"""CLI-only Mabinogi Mobile production assistant. No screen automation."""
import json
import logging
import math
import os
import queue
import re
import subprocess
import threading
import time
import traceback
import uuid
from pathlib import Path
from logging.handlers import RotatingFileHandler
import tkinter as tk
from tkinter import ttk, filedialog

import sys

# exe(모비 통합 매크로.exe)로 실행하면 읽기 전용 파일(레시피·아이콘)은 exe 안에서 읽고,
# 설정·로그는 exe 옆 data 폴더에 쓴다. 소스로 실행하면 둘 다 이 폴더다.
FROZEN = getattr(sys, 'frozen', False)
ROOT = Path(sys._MEIPASS) / 'workshop' if FROZEN else Path(__file__).resolve().parent
DATA = Path(sys.executable).parent / 'data' / 'workshop' if FROZEN else ROOT
DATA.mkdir(parents=True, exist_ok=True)
DEFAULT_CLI = r"C:\Nexon\MabinogiMobile\MabinogiMobile_CLI.exe"
SETTINGS = DATA / 'settings.json'
RECIPE_DATA = ROOT / 'recipes.json'
LOG_DIR = DATA / 'logs'
LOG_FILE = LOG_DIR / f'lifestyle-{os.getpid()}.log'


def _configure_logging():
    """Keep a durable, structured diagnostic trail outside the GUI log."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('erin_workshop')
    if not logger.handlers:
        handler = RotatingFileHandler(LOG_FILE, maxBytes=20 * 1024 * 1024,
                                      backupCount=5, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
    return logger


LOGGER = _configure_logging()


def log_event(level, event, **fields):
    """Write one complete JSON event; never let diagnostics break automation."""
    payload = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'event': event}
    payload.update(fields)
    try:
        line = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception as error:  # pragma: no cover - defensive logging path
        line = json.dumps({'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                           'event': 'logging_failure', 'error': repr(error),
                           'original_event': event}, ensure_ascii=False)
    LOGGER.log(getattr(logging, str(level).upper(), logging.INFO), line)


def log_exception(event, error, **fields):
    log_event('ERROR', event, error=repr(error), traceback=traceback.format_exc(), **fields)


def recipe_groups(recipes):
    groups = {}
    for name, options in recipes.items():
        groups.setdefault(Engine.output_name(name), []).extend(options)
    return {n: v for n, v in groups.items() if len(v) > 1}


class Halt(Exception):
    pass


class CLIError(Halt):
    def __init__(self, command, data):
        self.command, self.data = command, data
        super().__init__(f'{command}: {data}')


def parse_targets(text):
    parts = [p.strip() for p in text.split(',')]
    result = {}
    # Both 목재 30, 실크 10 and 목재,30,실크,10 are accepted.
    if len(parts) % 2 == 0 and all(re.fullmatch(r'\d+\s*개?', p) for p in parts[1::2]):
        parts = [f'{parts[i]} {parts[i+1]}' for i in range(0, len(parts), 2)]
    for part in parts:
        match = re.fullmatch(r'(.+?)[\s:=]+(\d+)\s*개?', part)
        if not match or not match[1].strip() or int(match[2]) <= 0:
            raise Halt('입력 예: 목재 30, 실크 10 (수량은 양의 정수)')
        name = match[1].strip()
        result[name] = result.get(name, 0) + int(match[2])
    return result


class CLI:
    def __init__(self, path):
        self.path = path
        self.run_id = None

    def call(self, command, body=None):
        args = [self.path, command]
        if body is not None:
            args.append(json.dumps(body, ensure_ascii=False) if isinstance(body, dict) else body)
        started = time.perf_counter()
        log_event('DEBUG', 'cli.request', run_id=self.run_id, command=command,
                  body=body, executable=self.path)
        try:
            p = subprocess.run(args, capture_output=True, encoding='utf-8-sig',
                               errors='replace', timeout=7200 if command.startswith('execute_') else 120,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired as error:
            log_exception('cli.timeout', error, run_id=self.run_id, command=command,
                          body=body, duration_ms=round((time.perf_counter() - started) * 1000))
            raise Halt('CLI 응답 시간 초과. 게임 상태 확인 후 다시 시작하세요. 자동 재시도하지 않습니다.')
        except OSError as e:
            log_exception('cli.os_error', e, run_id=self.run_id, command=command,
                          body=body, duration_ms=round((time.perf_counter() - started) * 1000))
            raise Halt(f'CLI 실행 실패: {e}')
        duration_ms = round((time.perf_counter() - started) * 1000)
        try:
            data = json.loads(p.stdout.strip())
        except ValueError as error:
            log_exception('cli.invalid_json', error, run_id=self.run_id, command=command,
                          body=body, returncode=p.returncode, duration_ms=duration_ms,
                          stdout=p.stdout, stderr=p.stderr)
            raise Halt(f'CLI 응답 해석 실패: {(p.stderr or p.stdout)[:500]}')
        raw_data = data
        if isinstance(data, dict) and 'body' in data:
            data = data['body']
        log_event('DEBUG', 'cli.response', run_id=self.run_id, command=command,
                  body=body, returncode=p.returncode, duration_ms=duration_ms,
                  stdout=p.stdout, stderr=p.stderr, parsed=raw_data, unwrapped=data)
        if p.returncode or isinstance(data, dict) and data.get('error'):
            log_event('ERROR', 'cli.error', run_id=self.run_id, command=command,
                      body=body, returncode=p.returncode, response=data)
            raise CLIError(command, data)
        return data


class Engine:
    def __init__(self, cli, emit, stop, budget=100, defaults=None, recovery=None, known_recipes=None):
        self.cli, self.emit, self.stop = cli, emit, stop
        self.run_id = uuid.uuid4().hex[:12]
        try:
            self.cli.run_id = self.run_id
        except Exception:
            pass
        self.budget, self.calls = budget, 0
        self.gather = {}
        self.recipes = {}
        self.catalog = {}
        self.defaults = defaults or {}
        self.recovery = recovery
        self.poll_seconds = 0.5
        self.reconnect_seconds = 15
        self.gather_probe_seconds = 15
        self.resin_empty_batches = 0
        # get_items intentionally omits equipment.  Keep successful equipment
        # crafts here so the scheduler can still finish the current run.
        self.untracked_produced = {}
        self.on_charge = None
        try:
            self.known_recipes = json.loads(RECIPE_DATA.read_text(encoding='utf-8'))['recipes']
        except (OSError, ValueError, KeyError):
            self.known_recipes = {}
        if known_recipes is not None:
            self.known_recipes = known_recipes
        log_event('INFO', 'engine.created', run_id=self.run_id, budget=budget,
                  defaults=self.defaults, cli=getattr(cli, 'path', repr(cli)))

    def gather_to(self, name, target, interrupt_when=None):
        """Monitor inventory while the blocking gathering command is running."""
        if self.weight_limited():
            return True
        route = name
        if (name == '나무 진액' and self.resin_empty_batches < 2
                and self.gather.get('부드러운 통나무', {}).get('ToolOk')):
            route = '부드러운 통나무'
        before = self.stock().get(name, 0) if route != name else None
        if route != name:
            self.emit(f'{name}: {route} 채집 경로 사용 · 진액 {target}개까지 감시')
        done = threading.Event()
        result = {}
        def gather():
            try:
                result['value'] = self.act('execute_gathering', route)
            except Exception as e:
                result['error'] = e
            finally:
                done.set()
        thread = threading.Thread(target=gather, daemon=True)
        thread.start()
        intentional = False
        yielded = False
        monitor_error = None
        fishing = False
        deadline = time.monotonic() + 1800
        next_queue_check = time.monotonic() + self.gather_probe_seconds
        try:
            while True:
                finished = done.wait(self.poll_seconds)
                if finished:
                    fishing = result.get('value', {}).get('result') == 'started'
                    if not fishing:
                        break
                self.check()
                weight_full = self.weight_limited()
                reached = self.stock().get(name, 0) >= target
                now = time.monotonic()
                facility_done = False
                if interrupt_when and now >= next_queue_check:
                    facility_done = interrupt_when()
                    next_queue_check = now + self.gather_probe_seconds
                if reached or facility_done or weight_full:
                    self.emit(f'{name}: ' + ('가방 무게 90% 이상 · 채집 중지' if weight_full else
                                            '가공·재료 우선순위 변경 · 채집 중지 후 재계획' if facility_done
                                             else f'목표 {target}개 도달 · 채집 중지 요청'))
                    try:
                        self.cli.call('stop_action')
                    except CLIError as error:
                        if not isinstance(error.data, dict) or error.data.get('error') != 'invalid_state':
                            raise
                        # Completion/travel can race stop_action. A completed
                        # call is safe to join; otherwise keep monitoring.
                        if not done.is_set():
                            continue
                    intentional, yielded = True, facility_done or weight_full
                    break
                if fishing:
                    if now >= deadline:
                        raise Halt('낚시 30분 제한에 도달했습니다.')
                    # The start command has returned; avoid polling a set
                    # event in a busy loop while auto fishing continues.
                    self.stop.wait(self.poll_seconds)
        except Exception as e:
            monitor_error = e
            try:
                self.cli.call('stop_action')
            except Exception:
                pass
        # Do not issue another action until the previous action has returned.
        while not done.wait(self.poll_seconds):
            pass
        if monitor_error:
            raise monitor_error
        self.check()
        error = result.get('error')
        if intentional:
            if error:
                canceled = isinstance(error, CLIError) and isinstance(error.data, dict) and error.data.get('error') == 'canceled'
                stopped = isinstance(error, Halt) and str(error).startswith('게임에서 작업이 중단되었습니다:')
                if not (canceled or stopped):
                    raise error
            return yielded
        if error:
            raise error
        if route != name:
            gained = self.stock().get(name, 0) - before
            self.resin_empty_batches = self.resin_empty_batches + 1 if gained <= 0 else 0
            log_event('INFO', 'gather.byproduct', run_id=self.run_id,
                      route=route, item=name, gained=gained, empty_batches=self.resin_empty_batches)
            if self.resin_empty_batches >= 2:
                self.emit('통나무 채집 2회 동안 진액 증가 없음 · 직접 진액 채집으로 전환')
        return False

    def check(self):
        if self.stop.is_set():
            raise Halt('중지됨. 등록된 가공 일감은 게임에서 계속 진행됩니다.')

    def weight_limited(self):
        data = self.read('get_inventory')
        try:
            current = float(data['CurrentInventoryWeight'])
            maximum = float(data['MaxInventoryWeight'])
            if not math.isfinite(current) or not math.isfinite(maximum) or current < 0 or maximum <= 0:
                raise ValueError('invalid weight')
        except (KeyError, TypeError, ValueError) as error:
            raise Halt(f'가방 무게를 확인할 수 없습니다: {data}') from error
        return current >= maximum * 0.90

    def read(self, cmd, body=None):
        while True:
            self.check()
            try:
                return self.cli.call(cmd, body)
            except CLIError as error:
                if not self.is_not_in_game(error):
                    raise
                # An execute/collect request may have made progress before
                # losing the session. Never replay it here; run() replans.
                if not (cmd.startswith('get_') or cmd == 'capabilities'):
                    raise
                items = self.wait_for_game(cmd)
                if cmd == 'get_items':
                    return items

    @staticmethod
    def is_not_in_game(error):
        return (isinstance(error, CLIError) and isinstance(error.data, dict)
                and error.data.get('error') == 'not_in_game')

    def wait_for_game(self, interrupted_command):
        self.emit('게임 접속 확인 대기 · 15초마다 자동 확인합니다. 중지 버튼으로 취소할 수 있습니다.')
        log_event('WARNING', 'game.connection_wait', run_id=self.run_id,
                  interrupted_command=interrupted_command,
                  retry_seconds=self.reconnect_seconds, calls=self.calls)
        attempts = 0
        while True:
            self.check()
            self.stop.wait(self.reconnect_seconds)
            self.check()
            attempts += 1
            try:
                items = self.cli.call('get_items')
            except CLIError as error:
                if not self.is_not_in_game(error):
                    raise
                log_event('WARNING', 'game.connection_retry', run_id=self.run_id,
                          interrupted_command=interrupted_command, attempt=attempts,
                          response=error.data)
                continue
            if not isinstance(items, list):
                raise Halt(f'접속 복구 후 재고 조회 실패: {items}')
            log_event('INFO', 'game.connection_restored', run_id=self.run_id,
                      interrupted_command=interrupted_command, attempts=attempts,
                      items_count=len(items), calls=self.calls)
            self.emit('게임 접속 복구 · 현재 재고와 가공 일감을 다시 확인합니다.')
            return items

    def settle_timed_out_action(self, error):
        """Prevent an uncertain old action from overlapping a fresh plan."""
        self.emit(f'{error.command}: 응답 시간 초과 · 이전 작업 중지 확인 후 이어합니다.')
        log_event('WARNING', 'game.action_timeout_recovery', run_id=self.run_id,
                  command=error.command, response=error.data, calls=self.calls)
        while True:
            self.wait_for_game(error.command)
            self.check()
            try:
                self.cli.call('stop_action')
            except CLIError as stop_error:
                if self.is_not_in_game(stop_error):
                    continue
                if not isinstance(stop_error.data, dict) or stop_error.data.get('error') != 'invalid_state':
                    raise
            log_event('INFO', 'game.action_settled', run_id=self.run_id,
                      command=error.command, calls=self.calls)
            return

    def stock(self):
        rows = self.read('get_items')
        if not isinstance(rows, list):
            raise Halt(f'보유 아이템 조회 실패: {rows}')
        counts = {}
        for row in rows:
            # Goals are inventory holdings; storage transfer eligibility is decided by recipe API.
            if row.get('Location') == 'inventory' and not row.get('IsLocked'):
                name = row['DisplayName']
                counts[name] = counts.get(name, 0) + int(row['Count'])
        return counts

    def load(self):
        log_event('DEBUG', 'engine.load.start', run_id=self.run_id)
        cap = self.read('capabilities')
        if cap.get('loading'):
            raise Halt('게임에 입장한 뒤 다시 시작하세요.')
        for cmd, kind in [('get_alterable_items', 'alter'), ('get_craftable_items', 'craft')]:
            for row in self.read(cmd).get('items', []):
                self.recipes.setdefault(row['DisplayName'], []).append((kind, row['DisplayName']))
                self.catalog[(kind, row['DisplayName'])] = row
        self.gather = {r['DisplayName']: r for r in self.read('get_gatherable_items').get('items', [])}
        log_event('INFO', 'engine.load.complete', run_id=self.run_id,
                  recipe_names=len(self.recipes), catalog_rows=len(self.catalog),
                  gatherable=len(self.gather))

    def resolve(self, name):
        options = self.recipes.get(name, [])
        if not options:
            options = [v for key, values in self.recipes.items()
                       if key.startswith(name + '(') for v in values]
        # The CLI can expose the same exact row more than once when a recipe
        # is unlocked through multiple categories. Identical rows have no
        # meaningful choice; only distinct exact routes need a default.
        options = list(dict.fromkeys(options))
        if len(options) > 1:
            log_event('WARNING', 'recipe.route_ambiguous', run_id=self.run_id,
                      requested=name, options=options, defaults=self.defaults)
            selected = tuple(self.defaults.get(self.output_name(name), []))
            if selected in options and options.count(selected) == 1:
                return selected
            raise Halt(f'{name}: 레시피를 지정하세요: ' + ', '.join(v[1] for v in options))
        return options[0] if options else None

    @staticmethod
    def output_name(name):
        return re.sub(r'\([^()]*\)$', '', name).strip()

    def act(self, cmd, name, craft_count=1):
        self.check()
        if cmd.startswith('execute_'):
            if (self.calls + 1) * 5 > self.budget:
                raise Halt('설정한 정령의 날개 사용 한도에 도달했습니다.')
            self.calls += 1
            if self.on_charge:
                self.on_charge(self.calls)
        self.emit(f'{cmd}: {name} · 실행 요청 비용 상한 {self.calls * 5}/{self.budget}')
        body = {'displayName': name}
        if cmd == 'execute_crafting':
            body['craftCount'] = craft_count
        log_event('INFO', 'engine.action', run_id=self.run_id, command=cmd,
                  name=name, craft_count=craft_count, calls=self.calls,
                  budget=self.budget)
        response = self.read(cmd, body)
        if response.get('result') in ('stopped', 'stopped_by_user'):
            raise Halt(f'게임에서 작업이 중단되었습니다: {response}')
        expected = {'execute_crafting': 'completed', 'execute_altering': 'started',
                    'execute_gathering': 'completed'}
        if cmd == 'execute_gathering' and response.get('result') == 'started':
            return response
        if cmd in expected and response.get('result') != expected[cmd]:
            raise Halt(f'작업 완료를 확인할 수 없습니다: {response}')
        return response

    def ensure(self, name, target, chain=()):
        """Compatibility entry point; production uses the shared scheduler."""
        if name in chain:
            raise Halt('재료 순환 참조: ' + ' → '.join(chain + (name,)))
        if not self.recipes and not self.gather:
            self.load()
        from production import Scheduler
        Scheduler(self, {name: target}).run()

    def plan_background_gathering(self, targets):
        from production import Scheduler
        scheduler = Scheduler(self, targets)
        scheduler.snapshot()
        return scheduler.make_plan().raw

    def run(self, targets):
        log_event('INFO', 'run.start', run_id=self.run_id, targets=targets,
                  calls=self.calls, budget=self.budget)
        while True:
            try:
                result = self.run_once(targets)
                log_event('INFO', 'run.complete', run_id=self.run_id, targets=targets,
                          calls=self.calls, budget=self.budget)
                return result
            except CLIError as e:
                if self.is_not_in_game(e):
                    self.wait_for_game(e.command)
                    self.check()
                    log_event('INFO', 'run.replan_after_connection', run_id=self.run_id,
                              command=e.command, calls=self.calls)
                    continue
                if (isinstance(e.data, dict) and e.data.get('error') == 'timeout'
                        and e.command in ('execute_gathering', 'execute_altering',
                                          'execute_crafting', 'complete_altering_work')):
                    self.settle_timed_out_action(e)
                    self.check()
                    continue
                if not isinstance(e.data, dict) or e.data.get('error') != 'blocked' or not self.recovery:
                    log_exception('run.error', e, run_id=self.run_id, targets=targets,
                                  calls=self.calls, command=e.command, response=e.data)
                    raise
                log_event('WARNING', 'run.blocked_recovery', run_id=self.run_id,
                          targets=targets, calls=self.calls, response=e.data)
                self.recovery(e.data, self.stop)
                self.check()
                self.emit('이어하기: 보유 재료·가공 일감을 다시 확인합니다. 사용 한도는 유지됩니다.')
            except Exception as e:
                log_exception('run.error', e, run_id=self.run_id, targets=targets,
                              calls=self.calls)
                raise

    def run_once(self, targets):
        from production import Scheduler
        log_event('DEBUG', 'run.catalog_reload', run_id=self.run_id, targets=targets)
        self.recipes = {}
        self.catalog = {}
        self.load()
        Scheduler(self, targets).run()


class App:
    def __init__(self, root):
        self.root = root
        root.title('마비노기 모바일 · 생활 도우미')
        root.geometry('1000x850')
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.worker = None
        self.cli = None
        self.defaults = {}
        try:
            saved = json.loads(SETTINGS.read_text(encoding='utf-8'))
            self.defaults = saved.get('defaults', {})
        except (OSError, ValueError):
            saved = {}
        self.catalog_loading = False
        self.catalog = {}
        self.groups = {}
        self.path = tk.StringVar(value=saved.get('cli', DEFAULT_CLI))
        self.targets = tk.StringVar(value='목재 30, 실크 10')
        self.budget = tk.StringVar(value='100')
        frame = ttk.Frame(root, padding=20)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='생활 도우미', font=('맑은 고딕', 20, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='목표는 가방의 최종 보유량 · 기존 보유분 포함 · CLI 전용').pack(anchor='w', pady=8)
        ttk.Entry(frame, textvariable=self.path).pack(fill='x')
        ttk.Button(frame, text='CLI 파일 선택', command=self.browse).pack(anchor='e')
        ttk.Label(frame, text='채집·가공·제작 혼합 입력: 최상급 거미줄 1000개, 목재 30').pack(anchor='w', pady=(12, 5))
        ttk.Entry(frame, textvariable=self.targets, font=('맑은 고딕', 12)).pack(fill='x')
        self.build_tabs(frame)
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=12)
        ttk.Label(row, text='정령의 날개 사용 한도').pack(side='left')
        ttk.Entry(row, textvariable=self.budget, width=8).pack(side='left', padx=8)
        self.start = ttk.Button(row, text='▶ 원클릭 시작 / 재개', command=self.launch)
        self.start.pack(side='left', padx=8)
        ttk.Button(row, text='중지', command=self.cancel).pack(side='left')
        ttk.Label(frame, text='채집은 최대 100개 단위 · 등록된 가공은 중지 후에도 진행 · 재료 구매/도구 수리는 수동').pack(anchor='w')
        self.log = tk.Text(frame, height=8, wrap='word', state='disabled', font=('맑은 고딕', 10))
        self.log.pack(fill='both', expand=True, pady=(10, 0))
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.poll()
        root.after(100, self.refresh_catalog)

    def build_tabs(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill='x', pady=6)
        self.refresh_button = ttk.Button(bar, text='게임 품목 새로고침', command=self.refresh_catalog)
        self.refresh_button.pack(side='left')
        self.summary = tk.StringVar(value='품목 조회 대기')
        ttk.Label(bar, textvariable=self.summary).pack(side='left', padx=10)
        self.tabs = ttk.Notebook(parent)
        self.tabs.pack(fill='both', expand=True)
        self.browsers = {}
        for kind, title in [('gather', '채집 재료'), ('alter', '가공 재료'), ('craft', '제작 아이템')]:
            page = ttk.Frame(self.tabs, padding=8)
            self.tabs.add(page, text=title)
            search = tk.StringVar()
            ttk.Entry(page, textvariable=search).pack(fill='x')
            box = tk.Listbox(page, height=9, exportselection=False)
            box.pack(fill='both', expand=True)
            search.trace_add('write', lambda *_, k=kind: self.filter_catalog(k))
            bottom = ttk.Frame(page)
            bottom.pack(fill='x', pady=6)
            ttk.Label(bottom, text='선택 품목 목표 수량').pack(side='left')
            qty = tk.StringVar(value='1')
            ttk.Spinbox(bottom, from_=1, to=1000000, textvariable=qty, width=10).pack(side='left', padx=8)
            ttk.Button(bottom, text='목표에 추가 / 수량 변경', command=lambda k=kind: self.add_target(k)).pack(side='left')
            self.browsers[kind] = (search, box, qty)
        settings = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(settings, text='설정 · 기본 레시피')
        ttk.Label(settings, text='품목 선택 → 기본 경로 선택 → 저장. 선택 가능한 경로만 표시합니다.').pack(anchor='w')
        self.group_list = tk.Listbox(settings, height=8, exportselection=False)
        self.group_list.pack(fill='both', expand=True)
        self.group_list.bind('<<ListboxSelect>>', self.select_group)
        self.route = ttk.Combobox(settings, state='readonly')
        self.route.pack(fill='x', pady=6)
        ttk.Button(settings, text='선택한 기본값 저장', command=self.save_route).pack(anchor='e')
        self.route_options = []

    def refresh_catalog(self):
        if self.catalog_loading or self.worker and self.worker.is_alive():
            return
        self.catalog_loading = True
        self.refresh_button.configure(state='disabled')
        path = self.path.get()
        def work():
            try:
                engine = Engine(CLI(path), self.events.put, threading.Event())
                engine.load()
                self.events.put(('catalog', engine))
            except Exception as e:
                self.events.put(f'품목 조회 실패: {e}')
            finally:
                self.events.put(('catalog_done', None))
        threading.Thread(target=work, daemon=True).start()

    def filter_catalog(self, kind):
        search, box, _ = self.browsers[kind]
        box.delete(0, 'end')
        for name in self.catalog.get(kind, []):
            if search.get().casefold() in name.casefold():
                box.insert('end', name)

    def add_target(self, kind):
        _, box, qty = self.browsers[kind]
        try:
            if not box.curselection():
                raise Halt('추가할 품목을 먼저 선택하세요.')
            count = int(qty.get())
            if count <= 0:
                raise Halt('수량은 1 이상이어야 합니다.')
            targets = parse_targets(self.targets.get()) if self.targets.get().strip() else {}
            targets[box.get(box.curselection()[0])] = count
            self.targets.set(', '.join(f'{n} {c}' for n, c in targets.items()))
        except (Halt, ValueError) as e:
            self.events.put(str(e))

    def select_group(self, event=None):
        if not self.group_list.curselection():
            return
        name = self.group_names[self.group_list.curselection()[0]]
        options = self.groups[name]
        self.route_options = [v for v in options if options.count(v) == 1]
        labels = [('가공' if k == 'alter' else '제작') + ' · ' + n for k, n in self.route_options]
        self.route.configure(values=['미지정'] + labels)
        chosen = tuple(self.defaults.get(name, []))
        self.route.current(self.route_options.index(chosen) + 1 if chosen in self.route_options else 0)

    def save_route(self):
        if not self.group_list.curselection():
            return
        name = self.group_names[self.group_list.curselection()[0]]
        index = self.route.current()
        if index > 0:
            self.defaults[name] = list(self.route_options[index - 1])
        else:
            self.defaults.pop(name, None)
        try:
            SETTINGS.write_text(json.dumps({'cli': self.path.get(), 'defaults': self.defaults}, ensure_ascii=False, indent=2), encoding='utf-8')
            self.events.put(f'{name}: 기본값 저장됨. 시작 / 재개 시 적용됩니다.')
        except OSError as e:
            self.events.put(f'설정 저장 실패: {e}')

    def browse(self):
        path = filedialog.askopenfilename(filetypes=[('CLI', '*.exe')])
        if path:
            self.path.set(path)

    def launch(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            targets = parse_targets(self.targets.get())
            budget = int(self.budget.get())
            if budget < 5:
                raise Halt('날개 사용 한도는 5 이상이어야 합니다.')
        except (Halt, ValueError) as e:
            self.events.put(str(e))
            return
        self.cli = CLI(self.path.get())
        self.stop.clear()
        self.start.configure(state='disabled')
        defaults = dict(self.defaults)
        def work():
            try:
                Engine(self.cli, self.events.put, self.stop, budget, defaults).run(targets)
            except Exception as e:
                self.events.put(f'중단: {e}')
            finally:
                self.events.put(None)
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def cancel(self):
        self.stop.set()
        if self.worker and self.worker.is_alive() and self.cli:
            def stop_game():
                try:
                    self.cli.call('stop_action')
                except Exception as e:
                    self.events.put(f'중지 요청: {e}')
            threading.Thread(target=stop_game, daemon=True).start()
            self.events.put('중지 요청됨. 진행 중인 CLI 응답을 기다립니다.')

    def close(self):
        if self.worker and self.worker.is_alive():
            self.cancel()
            self.events.put('작업이 끝나면 창을 닫아주세요.')
        else:
            self.root.destroy()

    def poll(self):
        while not self.events.empty():
            event = self.events.get_nowait()
            if isinstance(event, tuple):
                tag, engine = event
                if tag == 'catalog_done':
                    self.catalog_loading = False
                    self.refresh_button.configure(state='normal')
                elif tag == 'catalog':
                    self.catalog = {'gather': sorted(engine.gather)}
                    for kind in ('alter', 'craft'):
                        self.catalog[kind] = sorted(n for n, opts in engine.recipes.items() if any(k == kind for k, _ in opts))
                    for kind in self.browsers:
                        self.filter_catalog(kind)
                    self.groups = {n: opts for n, opts in recipe_groups(engine.recipes).items()
                                   if any(opts.count(v) == 1 for v in opts)}
                    self.group_names = sorted(self.groups)
                    self.group_list.delete(0, 'end')
                    for name in self.group_names:
                        opts = self.groups[name]
                        unique = sum(opts.count(v) == 1 for v in opts)
                        self.group_list.insert('end', f'{name} · {unique}경로')
                    self.summary.set(f'기본 레시피 설정 가능 {len(self.groups)}종')
                continue
            if event is None:
                self.start.configure(state='normal')
                continue
            line = f'[{time.strftime("%H:%M:%S")}] {event}\n'
            self.log.configure(state='normal')
            self.log.insert('end', line)
            self.log.see('end')
            self.log.configure(state='disabled')
        self.root.after(200, self.poll)


if __name__ == '__main__':
    root = tk.Tk()
    App(root)
    root.mainloop()
