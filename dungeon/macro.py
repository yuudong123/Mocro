"""은동전(룬다 일반)·마족 공물(페카 고분 심층 2-1) 던전 반복 매크로.

게임 상태는 CLI로 확인하고, 입장·재도전·이동 조작만 화면 인식과 클릭으로 한다.
현재 캐릭터가 있는 던전부터 돌고, 그 재화가 떨어지면 다른 던전으로 이동한다.
두 재화가 모두 떨어지면 아직 안 한 100레벨 캐릭터로 바꿔 계속한다.
F12로 중지한다. 게임이 관리자 권한이라 이 스크립트도 관리자 권한으로 다시 실행된다.
"""
import argparse
import math
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import win32con
from PIL import ImageStat
from pynput import keyboard

from common import (EXPECTED_SIZE, ROOT, TEMPLATES, Game, cli, currencies, find_game, is_admin,
                    relaunch_as_admin, summarize_activity, summarize_env)

LOGS = ROOT / 'logs'
VK_I, VK_M, VK_T, VK_ESC, VK_SPACE = 0x49, 0x4D, 0x54, win32con.VK_ESCAPE, win32con.VK_SPACE
SKIP_REGION = (600, 0, 800, 60)  # 오른쪽 위 "장면 넘기기"
CLEAR_TOUCH = (600, 300)        # 클리어 후 "화면을 터치해 주세요"
QUEST_TRACKER = (764, 138)      # 자동 진행이 안 켜질 때 누르는 퀘스트 추적 제목
DOUBLE_BUTTON = (294, 321)      # 룬다 입장 화면의 더블 루팅 선택 버튼
DOUBLE_REGION = (230, 290, 360, 350)
MAP_CONTINENT = (55, 24)        # 지도 왼쪽 위 "울라 대륙": 대륙 지도로 나간다
MAP_EAST, MAP_SOUTH = (46, 570), (111, 570)  # 이멘마하 지도 왼쪽 아래 동부/남부 탭
MAP_LIST_REGION = (0, 380, 160, 560)         # 지도 왼쪽 던전 목록
HUD_REGION = (500, 0, 640, 60)  # 필드 오른쪽 위 "Home". 지도처럼 화면을 덮는 창이 열리면 가려진다.
# 가방 무게가 한도를 넘으면 자동 진행이 멈춘다. 한 판 전리품만큼 여유를 두고 가방을 정리한다.
WEIGHT_LIMIT = 0.98
TIDY_BUTTON = (735, 509)        # 가방 오른쪽 아래 무게 옆 "정리"
# "간단히 정리하기" 창의 항목 체크 표시. 간단한 정리(장비 분해·재료 판매)만 켠다.
TIDY_OPTIONS = {'간단한 정리': (277, 313), '무거운 재료': (419, 313),
                '미스틱 다이스': (277, 392), '확실한 정리': (419, 392)}
TIDY_KEEP = '간단한 정리'
TIDY_START = (455, 539)         # "N개 정리하기"
TIDY_CONFIRM = (400, 563)       # "정리 대상"의 정리하기, "정리 완료"의 확인
BAG_CLOSE = (770, 18)
MENU_QUIT_REGION = (700, 500, 800, 600)  # ESC 메뉴 오른쪽 아래 "게임 종료"
TO_SELECT = (400, 426)          # 플레이 중단 창의 "캐릭터 선택 화면으로"
GAME_START = (400, 563)
# 캐릭터 선택 화면 카드: 4열 2줄. 2번째 줄도 위쪽(레벨 표시)은 스크롤 없이 보인다.
CARD_LEFTS, CARD_TOPS, CARD_WIDTH = (57, 231, 405, 579), (93, 374), 164


@dataclass
class Route:
    name: str
    currency: str
    cost: int
    dungeon_space: str                  # 던전 안 GameSpaceDisplayName 접두어
    field_space: str                    # 입구가 있는 지역
    entrance: tuple                     # 입구 근처 WorldPosition
    panel_template: str                 # 던전 선택 창이 이 던전인지 확인
    panel_clicks: list                  # 층·구역 선택 클릭
    enter_template: str                 # 입장 화면의 "입장하기"
    map_template: str                   # 지역 지도 왼쪽 목록의 던전 이름
    map_tab: tuple                      # 이멘마하 지도에서 누를 동부/남부 탭
    world_template: str                 # 대륙 지도의 던전 이름(목록에 없을 때)
    world_offset: tuple                 # 이름에서 던전 아이콘까지(이름 위쪽)
    double_cost: int = 0                # 더블 루팅 비용(0이면 없음)
    key: str = field(default='')


ROUTES = [
    Route(key='runda', name='룬다 일반 1-1', currency='은동전', cost=10, double_cost=20,
          dungeon_space='룬다 던전', field_space='이멘마하', entrance=(232, -69),
          panel_template='runda_title', panel_clicks=[(131, 254)],
          enter_template='enter_runda', map_template='map_runda', map_tab=MAP_SOUTH,
          world_template='world_runda', world_offset=(-5, -22)),
    Route(key='peka', name='페카 고분 심층 2-1', currency='마족 공물', cost=1,
          dungeon_space='페카 고분 심층', field_space='센마이 평원', entrance=(-270, 229),
          panel_template='peka_title', panel_clicks=[(104, 66), (146, 420)],
          enter_template='enter_deep', map_template='map_peka', map_tab=MAP_EAST,
          world_template='world_peka', world_offset=(0, -24)),
]


class Stop(Exception):
    pass


class Exhausted(Stop):
    """이 캐릭터의 은동전·마족 공물이 모두 부족하다."""


class Macro:
    def __init__(self, game, stop, log, max_runs=None, use_wings=True, start=None):
        self.game, self.stop, self.log = game, stop, log
        self.max_runs, self.use_wings, self.start = max_runs, use_wings, start
        self.runs = 0
        self.done_slots = set()  # 이번 실행에서 끝낸 캐릭터 카드 번호

    # ---- 상태 확인 ----
    def check(self):
        if self.stop.is_set():
            raise Stop('F12 중지 요청')

    def wait(self, seconds):
        if self.stop.wait(seconds):
            raise Stop('F12 중지 요청')

    def activity(self, patience=90):
        """get_activity, riding out not_in_game during loading screens."""
        deadline = time.monotonic() + patience
        while True:
            self.check()
            a = summarize_activity(cli('get_activity'))
            if isinstance(a, dict) and 'error' not in a:
                return a
            if time.monotonic() > deadline:
                raise Stop(f'게임 상태를 읽지 못했습니다: {a}')
            self.wait(1)

    def environment(self):
        for _ in range(60):
            env = summarize_env(cli('get_current_environment'))
            if isinstance(env, dict) and 'error' not in env:
                return env
            self.wait(1)
        raise Stop('현재 위치를 읽지 못했습니다.')

    def balance(self, route):
        # 로딩 중에는 not_in_game 오류가 온다. 0개로 오인하지 않도록 다시 읽는다.
        for _ in range(90):
            money = currencies()
            if isinstance(money, dict) and 'error' not in money:
                return money.get(route.currency, 0)
            self.wait(1)
        raise Stop(f'재화를 읽지 못했습니다: {money}')

    def affordable(self, route):
        return self.balance(route) >= route.cost

    def weight(self):
        for _ in range(90):
            data = cli('get_inventory')
            if isinstance(data, dict) and 'error' not in data:
                return float(data['CurrentInventoryWeight']), float(data['MaxInventoryWeight'])
            self.wait(1)
        raise Stop(f'가방 무게를 읽지 못했습니다: {data}')

    def overweight(self, limit=WEIGHT_LIMIT):
        """Stop reason when the bag is too heavy to run another dungeon, else None."""
        current, maximum = self.weight()
        if current < maximum * limit:
            return None
        return (f'가방 무게 {current:.0f}/{maximum:.0f} ({current / maximum:.1%}) · 여유 부족. '
                '보관함에 옮기거나 정리한 뒤 다시 실행하세요.')

    def make_room(self, limit=WEIGHT_LIMIT):
        """Tidy the bag when it is nearly full; Stop if that did not free enough weight."""
        if not self.overweight(limit):
            return
        self.tidy_bag()
        heavy = self.overweight(limit)
        if heavy:
            raise Stop(f'가방 정리 후에도 {heavy}')

    def route_in(self, env):
        for route in ROUTES:
            if (env.get('space') or '').startswith(route.dungeon_space):
                return route
        return None

    def near_entrance(self, route, env):
        if env.get('space') != route.field_space:
            return False
        return math.dist((env['x'], env['y']), route.entrance) < 80

    def other(self, route):
        return next(r for r in ROUTES if r is not route)

    def screenshot(self, tag):
        LOGS.mkdir(exist_ok=True)
        path = LOGS / f'{datetime.now():%Y%m%d-%H%M%S}-{tag}.png'
        try:
            self.game.capture().save(path)
            return path
        except Exception as error:  # noqa: BLE001 - best effort diagnostics
            self.log(f'스크린샷 실패: {error}')

    def tidy_checked(self, spot, image):
        # 켜진 항목은 주황 원(R-B 약 +120), 꺼진 항목은 회색(약 -20)이다.
        x, y = spot
        r, _, b = ImageStat.Stat(image.crop((x - 8, y - 8, x + 8, y + 8))).mean
        return r - b > 60

    def tidy_bag(self):
        """가방(I)의 "간단히 정리하기"로 장비를 분해하고 재료를 판다. 무거운 재료·미스틱 다이스는 끈다."""
        before, maximum = self.weight()
        self.log(f'가방 무게 {before:.0f}/{maximum:.0f} · 간단히 정리하기')
        self.game.key(VK_I)
        # 무게를 넘긴 상태에서는 가방을 열면 정리 창이 바로 뜬다. 안 뜨면 "정리"를 누른다.
        try:
            self.wait_for('정리 창', lambda: self.game.find('tidy_title'), 3)
        except Stop:
            self.check()
            self.game.click(*TIDY_BUTTON)
            self.wait_for('정리 창', lambda: self.game.find('tidy_title'), 5)
        for name, spot in TIDY_OPTIONS.items():
            want = name == TIDY_KEEP
            if self.tidy_checked(spot, self.game.capture()) != want:
                self.game.click(*spot)
                self.wait(0.6)
                if self.tidy_checked(spot, self.game.capture()) != want:
                    raise Stop(f'정리 항목 "{name}"을 {"켜지" if want else "끄지"} 못했습니다.')
        self.game.click(*TIDY_START)
        self.wait_for('정리 대상 화면', lambda: self.game.find('tidy_targets'), 10)
        self.wait(0.8)  # 화면이 서서히 나타나는 동안에는 버튼이 눌리지 않는다
        self.game.click(*TIDY_CONFIRM)
        self.wait_for('정리 완료 화면', lambda: self.game.find('tidy_done'), 30)
        self.wait(0.8)
        self.game.click(*TIDY_CONFIRM)
        self.wait(1.5)
        self.game.click(*BAG_CLOSE)
        self.wait(1.5)
        after, _ = self.weight()
        self.log(f'가방 정리 완료 · 무게 {before:.0f} → {after:.0f}')

    def wait_for(self, what, predicate, timeout, interval=0.7):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check()
            result = predicate()
            if result:
                return result
            self.wait(interval)
        raise Stop(f'{what}: {timeout}초 안에 확인되지 않았습니다.')

    def report(self):
        """--check: 조작 없이 판단 근거만 출력한다."""
        current, maximum = self.weight()
        self.log(f'게임 화면 {tuple(self.game.size())} · 재화 {currencies()} · 가방 {current:.0f}/{maximum:.0f}')
        self.log(f'상태 {self.activity()} · 위치 {self.environment()}')
        self.starting_route()
        image = self.game.capture()
        names = sorted(p.stem for p in TEMPLATES.glob('*.png'))
        seen = {name: self.game.find(name, image) for name in names}
        self.log('화면에서 찾은 버튼: ' + (', '.join(f'{k}{v}' for k, v in seen.items() if v) or '없음'))
        self.log(f'확인용 화면: {self.screenshot("check")}')

    # ---- 흐름 ----
    def starting_route(self):
        env = self.environment()
        if self.start and not self.route_in(env):
            route = next(r for r in ROUTES if r.key == self.start)
            self.log(f'지정한 {route.name}부터 진행')
            return route
        route = self.route_in(env) or next((r for r in ROUTES if self.near_entrance(r, env)), None)
        if route:
            self.log(f'현재 위치 {env["space"]} → {route.name}부터 진행')
            return route
        route = next((r for r in ROUTES if self.affordable(r)), ROUTES[0])
        self.log(f'현재 위치 {env["space"]}는 두 던전 입구가 아님 → {route.name}부터 진행')
        return route

    def run(self):
        size = tuple(self.game.size())
        if size != EXPECTED_SIZE:
            raise Stop(f'게임 화면이 {size[0]}x{size[1]}입니다. 800x600으로 맞춘 뒤 다시 실행하세요.')
        route = self.starting_route()
        loading_since = None
        while True:
            self.check()
            state = self.activity()['dungeon']
            env = self.environment()
            route = self.route_in(env) or route
            if state != 'NotInDungeon':
                loading_since = None
            if state in ('Entering', 'InProgress'):
                self.wait_clear(route)
            elif state == 'Cleared':
                self.to_reward_screen()
                self.runs += 1
                current, maximum = self.weight()
                self.log(f'{route.name} {self.runs}회 클리어 · 남은 {route.currency} {self.balance(route)}'
                         f' · 가방 {current:.0f}/{maximum:.0f}')
                if self.max_runs and self.runs >= self.max_runs:
                    self.leave_reward()
                    raise Stop(f'목표 {self.max_runs}회 완료')
                if self.overweight():
                    self.leave_reward()  # 밖에서 가방을 정리하고 다시 들어간다
                    continue
                if self.affordable(route):
                    self.game.click(*self.game.find('reward_retry'))
                    self.confirm_entry(route)
                    continue
                self.leave_reward()
                route = self.next_route(route)
            elif self.route_in(env):
                # 던전 공간인데 NotInDungeon: 입장·퇴장 로딩 중. 오래 가면 멈춘다.
                loading_since = loading_since or time.monotonic()
                if time.monotonic() - loading_since > 60:
                    raise Stop('던전 안에서 1분 넘게 진행 상태가 확인되지 않습니다.')
                self.wait(2)
                continue
            else:
                if not self.affordable(route):
                    route = self.next_route(route)
                self.make_room()
                self.enter_from_field(route)

    def run_all(self, switch=True):
        """run() each level-100 character in turn until every one is out of currency."""
        while True:
            try:
                self.run()
                return
            except Exhausted as reason:
                if not switch:
                    raise
                self.log(f'{reason} 다음 캐릭터로 바꿉니다.')
            self.start = None
            if not self.switch_character():
                raise Stop('모든 100레벨 캐릭터의 은동전·마족 공물이 부족합니다. 종료합니다.')

    # ---- 캐릭터 변경 ----
    def cards(self, image):
        """Character select cards: slot, level 100 or not, selected (yellow frame) or not."""
        found = []
        for row, top in enumerate(CARD_TOPS):
            for col, left in enumerate(CARD_LEFTS):
                r, _, b = ImageStat.Stat(image.crop((left, top + 20, left + 3, top + 150))).mean
                found.append({
                    'slot': row * len(CARD_LEFTS) + col,
                    'lv100': bool(self.game.find('lv100', image, region=(left, top, left + 110, top + 40))),
                    'selected': r - b > 40,  # 선택 카드 테두리 노랑(약 +80), 나머지 0 근처
                    'center': (left + CARD_WIDTH // 2, top + (120 if row == 0 else 90)),
                })
        return found

    def to_character_select(self):
        for _ in range(3):
            self.game.key(VK_ESC)
            try:
                button = self.wait_for('메뉴', lambda: self.game.find('menu_quit', region=MENU_QUIT_REGION), 3)
                break
            except Stop:
                self.check()  # 다른 창이 먼저 닫혔을 수 있어 다시 누른다
        else:
            raise Stop('ESC 메뉴를 열지 못했습니다.')
        self.game.click(*button)
        self.wait_for('플레이 중단 창', lambda: self.game.find('quit_title'), 5)
        self.game.click(*TO_SELECT)
        self.wait_for('캐릭터 선택 화면', lambda: self.game.find('select_title'), 90)
        self.wait(2)  # 카드가 다 그려질 때까지

    def switch_character(self):
        """Mark the current character done and log in to the next level-100 one; False if none is left."""
        self.to_character_select()
        self.game.scroll(400, 300, 10)  # 목록 맨 위로
        self.wait(1)
        cards = self.cards(self.game.capture())
        current = next((c['slot'] for c in cards if c['selected']), None)
        if current is not None:
            self.done_slots.add(current)
        todo = [c for c in cards if c['lv100'] and c['slot'] not in self.done_slots]
        self.log(f'캐릭터 선택 · 100레벨 {[c["slot"] + 1 for c in cards if c["lv100"]]}번 카드 · '
                 f'끝낸 카드 {sorted(s + 1 for s in self.done_slots)}')
        if not todo:
            return False
        card = todo[0]
        self.game.click(*card['center'])
        self.wait(1)
        if not self.cards(self.game.capture())[card['slot']]['selected']:
            raise Stop(f'{card["slot"] + 1}번 캐릭터 카드를 선택하지 못했습니다.')
        self.done_slots.add(card['slot'])
        self.game.click(*GAME_START)
        self.activity(patience=120)  # 접속하면 CLI가 응답한다
        self.wait(3)
        info = cli('get_my_info')
        level = info.get('Level') if isinstance(info, dict) else None
        level = level.get('Value') if isinstance(level, dict) else level
        job = info.get('EnabledCombatJobDisplayName') if isinstance(info, dict) else None
        job = job.get('Value') if isinstance(job, dict) else job
        self.log(f'{card["slot"] + 1}번 캐릭터 접속 · {job} {level}레벨 · 재화 {currencies()}')
        if level != 100:
            self.log('100레벨이 아니라 건너뜁니다.')
            return self.switch_character()
        return True

    def next_route(self, route):
        nxt = self.other(route)
        if not self.affordable(nxt):
            raise Exhausted(f'{route.currency}·{nxt.currency} 모두 부족합니다.')
        self.log(f'{route.currency} 부족 → {nxt.name}로 이동')
        return nxt

    def wait_clear(self, route):
        self.log(f'{route.name} 진행 중 · 클리어 대기')
        started = waiting = time.monotonic()
        nudged = False
        while True:
            a = self.activity()
            if a['dungeon'] == 'Cleared':
                self.log(f'클리어 ({time.monotonic() - started:.0f}초)')
                return
            if a['dungeon'] == 'NotInDungeon':
                return
            if a['dead']:
                self.log('사망 감지 · 30초 동안 부활 대기')
                self.wait_for('부활', lambda: not self.activity()['dead'], 30, interval=2)
            elapsed = time.monotonic() - waiting
            if a['dungeon'] == 'InProgress' and not a['auto'] and not a['boss']:
                if elapsed > 20 and self.overweight(limit=1):
                    self.log('가방 무게 초과로 자동 진행이 멈춤')
                    self.make_room(limit=1)
                    waiting, nudged = time.monotonic(), False  # 정리 후 다시 켜지는지 지켜본다
                    continue
                if elapsed > 20 and not nudged:
                    self.log('자동 진행이 꺼져 있음 · 퀘스트 추적을 눌러 시작')
                    self.game.click(*QUEST_TRACKER)
                    nudged = True
                elif elapsed > 60 and not a['combat']:
                    raise Stop('자동 진행이 시작되지 않습니다.')
            if time.monotonic() - started > 900:
                raise Stop('15분 동안 클리어되지 않았습니다.')
            if not a['combat'] and self.skip_scene():
                self.log('연출 장면 넘기기')
            self.wait(2)

    def skip_scene(self, image=None):
        """Press the translucent "장면 넘기기" button; light and dark backgrounds need separate images."""
        image = image if image is not None else self.game.capture()
        for name in ('skip_scene', 'skip_scene_dark'):
            spot = self.game.find(name, image, region=SKIP_REGION, threshold=0.8)
            if spot:
                self.game.click(*spot)
                return True
        return False

    def to_reward_screen(self):
        """Click through "화면을 터치해 주세요" and the chest scene up to the reward list."""
        deadline = time.monotonic() + 90
        last_click = time.monotonic()
        while time.monotonic() < deadline:
            self.check()
            image = self.game.capture()
            if self.game.find('reward_retry', image) and self.game.find('reward_exit', image):
                return
            if self.skip_scene(image):
                last_click = time.monotonic()
            elif self.game.find('touch_screen', image) or time.monotonic() - last_click > 6:
                self.game.click(*CLEAR_TOUCH)
                last_click = time.monotonic()
            self.wait(0.8)
        raise Stop('보상 화면(나가기/다시 하기)을 찾지 못했습니다.')

    def leave_reward(self):
        exit_button = self.game.find('reward_exit')
        if exit_button:
            self.game.click(*exit_button)
        else:
            self.game.key(VK_ESC)
        self.wait_for('던전 퇴장', lambda: self.activity()['dungeon'] == 'NotInDungeon', 60, interval=1.5)
        self.wait(2)

    def confirm_entry(self, route):
        """On the entry screen: set double looting, press 입장하기, verify the charge."""
        button = self.wait_for('입장 화면', lambda: self.game.find(route.enter_template), 15)
        if route.double_cost:
            want = self.balance(route) >= route.double_cost
            have = bool(self.game.find('double_on', region=DOUBLE_REGION))
            if want != have:
                self.log('더블 루팅 ' + ('켜기' if want else '끄기 (은동전 20개 미만)'))
                self.game.click(*DOUBLE_BUTTON)
                self.wait(0.8)
                if bool(self.game.find('double_on', region=DOUBLE_REGION)) != want:
                    raise Stop('더블 루팅 상태를 바꾸지 못했습니다.')
            button = self.game.find(route.enter_template) or button
        before = self.balance(route)
        self.game.click(*button)
        # 입장 중에는 Entering → NotInDungeon → InProgress 순서로 잠깐 밖으로 보인다.
        self.wait_for('던전 입장', lambda: self.activity()['dungeon'] == 'InProgress', 60, interval=1)
        spent = before - self.balance(route)
        self.log(f'{route.name} 입장 · {route.currency} {spent}개 사용')
        # 기다리면 자동 진행이 켜지지만 Space로 바로 시작하면 몇 초 아낀다.
        if not self.activity()['auto']:
            self.game.key(VK_SPACE)

    def enter_from_field(self, route):
        if self.game.find(route.enter_template):
            self.confirm_entry(route)  # 입장 화면이 이미 열려 있다
            return
        panel = self.game.find(route.panel_template)
        if not panel:
            if self.game.find('panel_close'):
                self.game.key(VK_ESC)  # 다른 창이 열려 있으면 닫는다
                self.wait(1)
            a, env = self.activity(), self.environment()
            if a['interaction'] == 'EnterDungeon' and self.near_entrance(route, env):
                self.game.key(VK_SPACE)
            else:
                for attempt in range(3):
                    if self.travel(route):
                        break
                    self.log(f'입구 도착을 확인하지 못함 · 다시 이동 ({attempt + 1}/3)')
                else:
                    raise Stop(f'{route.name} 입구로 이동하지 못했습니다.')
            self.wait_for(f'{route.name} 선택 창', lambda: self.game.find(route.panel_template), 20)
        for x, y in route.panel_clicks:
            self.game.click(x, y)
            self.wait(0.8)
        self.game.key(VK_SPACE)  # "n층 n구역 진입"
        self.confirm_entry(route)

    def travel(self, route):
        """지도(M)에서 던전을 골라 여기로 가기. 도착하면 선택 창이 자동으로 열린다.

        이멘마하: 동부/남부 탭 목록. 센마이 평원: 목록에 페카만 있어 룬다는 대륙 지도에서 고른다.
        """
        self.log(f'{route.name} 입구로 이동')
        space = self.environment().get('space')
        self.open_map()
        self.wait(1)
        item = self.game.find(route.map_template, region=MAP_LIST_REGION)
        if not item and space == '이멘마하':
            item = self.wait_for('지도 목록', lambda: self.map_list_item(route), 30, interval=2)
        if item:
            self.game.click(*item)
        else:
            # 지도가 열리는 중에는 "울라 대륙" 클릭이 먹히지 않을 수 있어 몇 번 다시 누른다.
            for _ in range(3):
                self.game.click(*MAP_CONTINENT)
                try:
                    label = self.wait_for('대륙 지도', lambda: self.game.find(route.world_template, threshold=0.9), 4)
                    break
                except Stop:
                    self.check()
            else:
                raise Stop('대륙 지도로 나가지 못했습니다.')
            self.game.click(label[0] + route.world_offset[0], label[1] + route.world_offset[1])
        go = self.wait_for('여기로 가기', lambda: self.game.find('map_go'), 10)
        self.game.click(*go)
        if space != route.field_space and self.use_wings:
            self.wait(1.5)
            self.game.key(VK_T)
        return self.wait_arrival(route)

    def open_map(self, timeout=60):
        """Press M until the map covers the field HUD.

        레벨업 직후 미스틱 다이스 창 같은 것이 떠 있으면 M이 먹히지 않는다. 창이 닫힐 때까지 다시 누른다.
        """
        deadline = time.monotonic() + timeout
        while True:
            self.game.key(VK_M)
            try:
                self.wait_for('지도', lambda: not self.game.find('hud_home', region=HUD_REGION), 5)
                return
            except Stop:
                self.check()
            if time.monotonic() > deadline:
                raise Stop(f'지도를 {timeout}초 동안 열지 못했습니다.')
            self.log('지도가 열리지 않음 · 다른 창이 닫히길 기다렸다가 다시 열기')

    def map_list_item(self, route):
        """이멘마하 지도 목록에서 던전을 찾는다. 지도가 닫혀 있으면 다시 열고, 아니면 탭을 다시 누른다."""
        item = self.game.find(route.map_template, region=MAP_LIST_REGION)
        if item:
            return item
        if self.game.find('hud_home', region=HUD_REGION):
            self.open_map()
            self.wait(1)
        self.game.click(*route.map_tab)
        return None

    def wait_arrival(self, route):
        """Watch the auto travel through the CLI only.

        Capturing the screen (PrintWindow) makes the game hitch, and repeated hitches
        stopped the auto travel mid-way, so the screen is checked only after it ends.
        """
        deadline = time.monotonic() + 300
        idle_since = None
        while time.monotonic() < deadline:
            a = self.activity()
            if a['travel'] or a['auto']:
                idle_since = None
            else:
                idle_since = idle_since or time.monotonic()
                if a['interaction'] == 'EnterDungeon' or time.monotonic() - idle_since > 4:
                    break
            self.wait(1.5)
        for _ in range(4):
            if self.game.find(route.panel_template):
                return True
            if self.activity()['interaction'] == 'EnterDungeon':
                self.game.key(VK_SPACE)
            self.wait(1.5)
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=int, help='이 횟수만큼 클리어하면 종료')
    parser.add_argument('--start', choices=[r.key for r in ROUTES],
                        help='던전 밖에서 시작할 때 이 던전부터 진행 (runda/peka)')
    parser.add_argument('--check', action='store_true', help='조작 없이 현재 위치·재화·화면 인식 결과만 출력')
    parser.add_argument('--no-wings', action='store_true', help='던전 간 이동에 정령의 날개(T)를 쓰지 않음')
    parser.add_argument('--no-switch', action='store_true', help='재화가 떨어져도 다른 캐릭터로 바꾸지 않고 종료')
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    if not is_admin():
        if not relaunch_as_admin(__file__):
            sys.exit('관리자 권한 실행이 취소되었습니다. 게임이 관리자 권한이라 매크로도 관리자 권한이 필요합니다.')
        return
    LOGS.mkdir(exist_ok=True)
    logfile = (LOGS / f'dungeon-{datetime.now():%Y%m%d}.log').open('a', encoding='utf-8')

    def log(message):
        line = f'[{datetime.now():%H:%M:%S}] {message}'
        print(line, flush=True)
        logfile.write(line + '\n')
        logfile.flush()

    stop = threading.Event()
    keyboard.Listener(on_press=lambda k: stop.set() if k == keyboard.Key.f12 else None, daemon=True).start()
    try:
        hwnd = find_game()
        if not hwnd:
            raise Stop('마비노기 모바일 창을 찾지 못했습니다.')
        log('던전 매크로 시작 · F12 중지')
        macro = Macro(Game(hwnd), stop, log, max_runs=args.runs, use_wings=not args.no_wings,
                      start=args.start)
        if args.check:
            macro.report()
            return
        try:
            macro.run_all(switch=not args.no_switch)
        except Stop as reason:
            if not stop.is_set() and '완료' not in str(reason) and '부족' not in str(reason):
                log(f'중단 당시 화면: {macro.screenshot("stop")}')
            raise
        except Exception as error:
            shot = macro.screenshot('error')
            raise Stop(f'예상치 못한 오류: {error!r} (화면: {shot})') from error
    except Stop as reason:
        log(f'종료: {reason}')
    finally:
        logfile.close()
        input('엔터를 누르면 창을 닫습니다.')


if __name__ == '__main__':
    main()
