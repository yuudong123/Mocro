"""에린 공방의 던전 탭: dungeon/macro.py를 돌리고 로그와 캐릭터별 재화를 보여 준다.

던전 매크로는 화면 인식·입력에 OpenCV·pywin32 등을 쓰고, 게임이 관리자 권한이라 관리자 권한으로 돌아야 한다.
- exe(모비 통합 매크로.exe): 패키지가 들어 있고 exe가 관리자 권한으로 뜨므로 이 프로세스 안에서 바로 돌린다.
- 소스(생활.bat): 공방과 따로 dungeon/.venv 가상환경을 쓰고, 탭을 처음 열 때 dungeon/requirements.txt를
  설치한다. 매크로는 관리자 권한으로 콘솔 없이 띄우고 로그 파일을 읽어 보여 준다. 중지는 stop.request 파일로 알린다.
"""
import ctypes
import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import queue
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
                               QCheckBox, QComboBox, QSpinBox, QPlainTextEdit)

from app import FROZEN, ROOT

DUNGEON = ROOT.parent / 'dungeon'            # exe면 exe 안의 dungeon(템플릿)
DUNGEON_DATA = Path(sys.executable).parent / 'data' / 'dungeon' if FROZEN else DUNGEON
VENV = DUNGEON / '.venv'
VENV_PYTHON = VENV / 'Scripts' / 'python.exe'
VENV_PYTHONW = VENV / 'Scripts' / 'pythonw.exe'
REQUIREMENTS = DUNGEON / 'requirements.txt'
STAMP = VENV / 'requirements.installed'      # 마지막으로 설치한 requirements.txt 사본(던전.bat과 같다)
STOP_FILE = DUNGEON_DATA / 'stop.request'
LOGS = DUNGEON_DATA / 'logs'
ROSTER = DUNGEON / 'roster.py'
COSTS = {'은동전': 10, '마족 공물': 1}           # 룬다 일반 1-1, 페카 고분 심층 2-1 입장 비용(macro.ROUTES)
STARTS = [('자동', None), ('룬다 일반 1-1', 'runda'), ('페카 고분 심층 2-1', 'peka')]
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def installed():
    """dungeon/.venv가 있고 requirements.txt가 마지막 설치 때와 같다(exe에는 패키지가 들어 있다)."""
    if FROZEN:
        return True
    try:
        return VENV_PYTHON.exists() and STAMP.read_bytes() == REQUIREMENTS.read_bytes()
    except OSError:
        return False


def base_python():
    """가상환경을 만들 Python. dungeon/requirements.txt의 numpy 1.26은 3.11·3.12용 휠만 있다."""
    candidates = [Path(sys.executable).with_name('python.exe')]
    for version in ('3.11', '3.12'):
        try:
            out = subprocess.run(['py', f'-{version}', '-c', 'import sys; print(sys.executable)'],
                                 capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW)
            if out.returncode == 0 and out.stdout.strip():
                candidates.append(Path(out.stdout.strip()))
        except (OSError, subprocess.TimeoutExpired):
            pass
    local = Path(os.environ.get('LOCALAPPDATA', ''))
    candidates += [local / 'Programs' / 'Python' / f'Python3{v}' / 'python.exe' for v in (11, 12)]
    for path in candidates:
        if not path.exists():
            continue
        try:
            out = subprocess.run([str(path), '-c', 'import sys; print(sys.version_info[:2])'],
                                 capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.stdout.strip() in ('(3, 11)', '(3, 12)'):
            return path
    return None


def install(say):
    """dungeon/.venv를 만들고 requirements.txt를 설치한다. say로 진행 상황을 알린다."""
    if not VENV_PYTHON.exists():
        python = base_python()
        if python is None:
            raise RuntimeError('Python 3.11 또는 3.12가 필요합니다. python.org에서 설치한 뒤 다시 열어 주세요.')
        say(f'가상환경 만드는 중: dungeon\\.venv ({python})')
        done = subprocess.run([str(python), '-m', 'venv', str(VENV)], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
        if done.returncode != 0:
            raise RuntimeError(f'가상환경을 만들지 못했습니다: {done.stderr.strip()[-300:]}')
    say('던전 매크로 패키지 설치 중(처음 한 번, 1~2분)…')
    proc = subprocess.Popen([str(VENV_PYTHON), '-m', 'pip', 'install', '--disable-pip-version-check',
                             '-r', str(REQUIREMENTS)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
    for line in proc.stdout:
        line = line.strip()
        if line.startswith(('Collecting', 'Installing', 'Successfully', 'ERROR')):
            say(line)
    if proc.wait() != 0:
        raise RuntimeError('패키지 설치에 실패했습니다. 위 메시지를 확인하세요.')
    shutil.copyfile(REQUIREMENTS, STAMP)
    say('설치 완료')


class ShellExecuteInfo(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('fMask', ctypes.c_ulong), ('hwnd', wintypes.HWND),
                ('lpVerb', wintypes.LPCWSTR), ('lpFile', wintypes.LPCWSTR), ('lpParameters', wintypes.LPCWSTR),
                ('lpDirectory', wintypes.LPCWSTR), ('nShow', ctypes.c_int), ('hInstApp', wintypes.HINSTANCE),
                ('lpIDList', ctypes.c_void_p), ('lpClass', wintypes.LPCWSTR), ('hkeyClass', wintypes.HKEY),
                ('dwHotKey', wintypes.DWORD), ('hIcon', wintypes.HANDLE), ('hProcess', wintypes.HANDLE)]


def launch_elevated(args):
    """관리자 권한(UAC 확인)으로 콘솔 없이 매크로를 띄운다. 프로세스 핸들, 취소하면 None."""
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40                      # SEE_MASK_NOCLOSEPROCESS: 끝났는지 보려고 핸들을 받는다
    info.lpVerb = 'runas'
    info.lpFile = str(VENV_PYTHONW)
    info.lpParameters = ' '.join(f'"{a}"' for a in ['macro.py', '--no-pause', *args])
    info.lpDirectory = str(DUNGEON)
    info.nShow = 0
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        return None
    return info.hProcess


def finished(handle):
    """Exit code if the process ended, else None (and closes the handle once it ended)."""
    if ctypes.windll.kernel32.WaitForSingleObject(handle, 0) != 0:
        return None
    code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(handle)
    return code.value


def roster_lines():
    """dungeon/characters.json의 카드별 재화(추정)를 한 줄씩."""
    try:
        if FROZEN:
            import roster as module
        else:  # 공방의 Python에는 dungeon 폴더가 경로에 없다
            spec = importlib.util.spec_from_file_location('dungeon_roster', ROSTER)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        roster = module.Roster()
    except Exception as error:  # noqa: BLE001 - 표시용이라 실패해도 탭은 쓴다
        return [f'캐릭터 기록을 읽지 못했습니다: {error}']
    lines = []
    for slot in sorted(int(k) for k in roster.cards):
        job = (roster.cards[str(slot)].get('id') or ['', ''])[1]
        lines.append(roster.describe(slot, COSTS).replace(f'{slot + 1}번', f'{slot + 1}번 {job}', 1))
    return lines or ['아직 기록 없음 · 매크로가 캐릭터를 돌면 채워집니다.']


class LogTail:
    """dungeon/logs/dungeon-날짜.log에서 시작 이후 새로 쓰인 줄만 읽는다."""

    def __init__(self):
        self.path = LOGS / f'dungeon-{datetime.now():%Y%m%d}.log'
        self.offset = self.path.stat().st_size if self.path.exists() else 0

    def read(self):
        lines = []
        today = LOGS / f'dungeon-{datetime.now():%Y%m%d}.log'
        for path in dict.fromkeys([self.path, today]):
            if path != self.path:          # 자정이 지나 새 파일
                self.path, self.offset = path, 0
            try:
                with path.open('rb') as f:
                    f.seek(self.offset)
                    data = f.read()
            except OSError:
                continue
            complete = data.rfind(b'\n') + 1
            self.offset += complete
            lines += data[:complete].decode('utf-8', errors='replace').splitlines()
        return lines


class DungeonPanel(QWidget):
    def __init__(self, state=None, changed=None):
        super().__init__()
        state = state or {}
        self.changed = changed or (lambda: None)
        self.events = queue.Queue()
        self.installing = False
        self.process = None
        self.tail = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        title = QLabel('던전 매크로')
        title.setObjectName('title')
        lay.addWidget(title)
        sub = QLabel('은동전으로 룬다 일반 1-1, 마족 공물로 페카 고분 심층 2-1을 반복합니다. '
                     '게임 화면 800×600 · 시작하면 관리자 권한 확인 창이 뜹니다 · F12로도 중지')
        sub.setObjectName('muted')
        sub.setWordWrap(True)
        lay.addWidget(sub)
        self.status = QLabel()
        lay.addWidget(self.status)
        options = QGridLayout()
        self.switch = QCheckBox('재화를 다 쓰면 다른 100레벨 캐릭터로 바꾸기')
        self.clean = QCheckBox('캐릭터를 바꾸기 전 가방 정리(상자 열기·장비·룬 분해)')
        self.wings = QCheckBox('다른 지역으로 갈 때 정령의 날개 사용')
        for box, key in ((self.switch, 'switch'), (self.clean, 'clean'), (self.wings, 'wings')):
            box.setChecked(state.get(key, True))
            box.toggled.connect(lambda *_: self.changed())
        options.addWidget(self.switch, 0, 0, 1, 2)
        options.addWidget(self.clean, 1, 0, 1, 2)
        options.addWidget(self.wings, 2, 0, 1, 2)
        options.addWidget(QLabel('던전 밖에서 시작할 던전'), 3, 0)
        self.start_route = QComboBox()
        self.start_route.addItems([name for name, _ in STARTS])
        self.start_route.setCurrentIndex(state.get('start', 0))
        self.start_route.currentIndexChanged.connect(lambda *_: self.changed())
        options.addWidget(self.start_route, 3, 1)
        options.addWidget(QLabel('클리어 횟수 제한 (0 = 재화를 다 쓸 때까지)'), 4, 0)
        self.runs = QSpinBox()
        self.runs.setRange(0, 9999)
        self.runs.setValue(state.get('runs', 0))
        self.runs.valueChanged.connect(lambda *_: self.changed())
        options.addWidget(self.runs, 4, 1)
        lay.addLayout(options)
        buttons = QHBoxLayout()
        self.run_button = QPushButton('▶ 시작')
        self.run_button.setObjectName('primary')
        self.run_button.clicked.connect(lambda: self.launch(check=False))
        self.check_button = QPushButton('상태 확인 (조작 없음)')
        self.check_button.clicked.connect(lambda: self.launch(check=True))
        self.stop_button = QPushButton('중지')
        self.stop_button.clicked.connect(self.request_stop)
        for button in (self.run_button, self.check_button, self.stop_button):
            buttons.addWidget(button)
        lay.addLayout(buttons)
        lay.addWidget(QLabel('캐릭터별 은동전·마족 공물 (마지막 기록 + 충전 추정)'))
        self.roster = QPlainTextEdit()
        self.roster.setReadOnly(True)
        self.roster.setMaximumHeight(120)
        lay.addWidget(self.roster)
        lay.addWidget(QLabel('실행 기록'))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        lay.addWidget(self.log, 1)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(500)
        self.update_buttons()
        self.show_roster()

    def state(self):
        return {'switch': self.switch.isChecked(), 'clean': self.clean.isChecked(),
                'wings': self.wings.isChecked(), 'start': self.start_route.currentIndex(),
                'runs': self.runs.value()}

    def arguments(self, check=False):
        if check:
            return ['--check']
        args = []
        if not self.switch.isChecked():
            args.append('--no-switch')
        if not self.clean.isChecked():
            args.append('--no-clean')
        if not self.wings.isChecked():
            args.append('--no-wings')
        start = STARTS[self.start_route.currentIndex()][1]
        if start:
            args += ['--start', start]
        if self.runs.value():
            args += ['--runs', str(self.runs.value())]
        return args

    def message(self, text):
        self.log.appendPlainText(text)

    def update_buttons(self):
        ready = installed() and not self.installing
        running = self.process is not None
        self.run_button.setEnabled(ready and not running)
        self.check_button.setEnabled(ready and not running)
        self.stop_button.setEnabled(running)
        if self.installing:
            self.status.setText('던전 매크로 준비 중…')
        elif running:
            self.status.setText('실행 중 · 중지 버튼이나 F12로 멈춥니다')
        elif ready:
            self.status.setText('준비됨')
        else:
            self.status.setText('던전 탭을 처음 열면 필요한 패키지를 설치합니다')

    def activate(self):
        """탭을 열 때마다 부른다. 처음이면(또는 requirements.txt가 바뀌었으면) 설치한다."""
        self.show_roster()
        if installed() or self.installing:
            return
        self.installing = True
        self.update_buttons()

        def task():
            try:
                install(lambda text: self.events.put(('log', text)))
            except Exception as error:  # noqa: BLE001 - 설치 실패는 탭에 보여 준다
                self.events.put(('log', f'설치 실패: {error}'))
            finally:
                self.events.put(('installed', None))
        threading.Thread(target=task, daemon=True).start()

    def launch(self, check=False):
        if self.process is not None or not installed():
            return
        args = self.arguments(check)
        self.message(('상태 확인' if check else '던전 매크로 시작') + (f' · {" ".join(args)}' if args else ''))
        if FROZEN:
            self.run_inside(args)
            return
        self.tail = LogTail()
        STOP_FILE.unlink(missing_ok=True)
        handle = launch_elevated(args)
        if handle is None:
            self.message('관리자 권한 실행이 취소되었습니다. 게임이 관리자 권한이라 매크로도 관리자 권한이 필요합니다.')
            return
        self.process = handle
        self.update_buttons()

    def run_inside(self, args):
        """exe: 이미 관리자 권한이고 패키지가 들어 있어 이 프로세스의 스레드로 돌린다."""
        import macro  # OpenCV 등은 공방을 열 때가 아니라 처음 시작할 때 읽는다
        self.stop_event = threading.Event()
        self.process = 'inside'
        self.update_buttons()

        def task():
            log, close = macro.open_log(lambda line: self.events.put(('log', line)))
            listener = macro.watch_stop(self.stop_event)
            try:
                macro.session(macro.parse([*args, '--no-pause']), self.stop_event, log)
            finally:
                self.stop_event.set()  # F12·중지 파일 감시도 끝낸다
                listener.stop()
                close()
                self.events.put(('done', None))
        threading.Thread(target=task, daemon=True).start()

    def request_stop(self):
        if self.process is None:
            return
        if self.process == 'inside':
            self.stop_event.set()
            self.message('중지 요청 · 진행 중인 동작을 마치면 멈춥니다')
            return
        try:
            STOP_FILE.write_text('stop', encoding='utf-8')
            self.message('중지 요청 · 진행 중인 동작을 마치면 멈춥니다')
        except OSError as error:
            self.message(f'중지 요청 실패: {error} · 게임 창에서 F12를 누르세요')

    def show_roster(self):
        self.roster.setPlainText('\n'.join(roster_lines()))

    def poll(self):
        while not self.events.empty():
            tag, value = self.events.get_nowait()
            if tag == 'log':
                self.message(value)
            elif tag == 'installed':
                self.installing = False
                self.update_buttons()
            elif tag == 'done':
                self.process = None
                self.message('매크로 종료')
                self.show_roster()
                self.update_buttons()
        if self.tail:
            for line in self.tail.read():
                self.message(line)
        if self.process is not None and self.process != 'inside':
            code = finished(self.process)
            if code is not None:
                self.process = None
                for line in self.tail.read() if self.tail else []:
                    self.message(line)
                self.tail = None
                self.message(f'매크로 종료 (코드 {code})')
                STOP_FILE.unlink(missing_ok=True)
                self.show_roster()
                self.update_buttons()
