"""에린 공방의 던전 탭: dungeon/macro.py를 돌리고 로그와 캐릭터별 재화를 보여 준다.

던전 매크로는 화면 인식·입력에 OpenCV·pywin32 등을 쓰고, 게임이 관리자 권한이라 관리자 권한으로 돌아야 한다.
- exe(모비 통합 매크로.exe): 패키지가 들어 있고 exe가 관리자 권한으로 뜨므로 이 프로세스 안에서 바로 돌린다.
- 소스(생활.bat): 공방과 따로 dungeon/.venv 가상환경을 쓰고, 탭을 처음 열 때 dungeon/requirements.txt를
  설치한다. 매크로는 관리자 권한으로 콘솔 없이 띄우고 로그 파일을 읽어 보여 준다. 중지는 stop.request 파일로 알린다.
"""
import ctypes
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import queue
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QFrame,
                               QCheckBox, QComboBox, QSpinBox, QPlainTextEdit, QScrollArea, QTableWidget,
                               QTableWidgetItem, QHeaderView, QSystemTrayIcon)

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
# 공방의 Python에는 OpenCV가 없어 macro를 읽지 못하므로 선택지는 여기 적는다(macro.ROUTES·DEFAULT_CONFIG와 같게).
SILVER_ROUTES = [('runda', '룬다 일반 1-1')]
TRIBUTE_ROUTES = [('peka', '페카 고분 심층 2-1')]
DOUBLE_MODES = [('정한 개수 이상일 때 켜기', 'threshold'), ('가능하면 항상 켜기', 'always'), ('끄기', 'off')]
GRADES = ['일반', '고급', '레어', '엘리트', '에픽', '전설', '전설+', '전설++', '신화', '유니크']
DEFAULT_RUNE_GRADES = GRADES[:5]
SLOTS = 6                                     # 캐릭터 선택 화면 카드 수
CHAR_DEFAULT = {'include': True, 'silver': True, 'tribute': True, 'items': True, 'equip': True, 'rune': True}
COLUMNS = [('카드', None), ('캐릭터', None), ('포함', 'include'), ('은동전', 'silver'), ('공물', 'tribute'),
           ('상자·소모품', 'items'), ('장비 분해', 'equip'), ('룬 분해', 'rune'), ('지금 재화(추정)', None)]
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


def load_roster():
    """dungeon/roster.py의 Roster(카드별 마지막 재화 기록). 읽지 못하면 None."""
    try:
        if FROZEN:
            import roster as module
        else:  # 공방의 Python에는 dungeon 폴더가 경로에 없다
            spec = importlib.util.spec_from_file_location('dungeon_roster', ROSTER)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        return module.Roster()
    except Exception:  # noqa: BLE001 - 표시용이라 실패해도 탭은 쓴다
        return None


def roster_rows(costs=COSTS):
    """카드 번호(0부터) → (캐릭터, 재화 추정 글). 기록이 없는 카드는 빠진다."""
    roster = load_roster()
    rows = {}
    if roster is None:
        return rows
    for key, card in roster.cards.items():
        slot = int(key)
        ident = card.get('id') or ['', '']
        guess = roster.estimate(slot)
        ready = roster.ready_at(slot, costs)
        when = '지금 가능' if ready <= roster.now() else f'{datetime.fromtimestamp(ready):%m-%d %H:%M}부터'
        cleaned = f' · 가방 정리됨 {card["cleaned_at"]}' if not roster.needs_cleaning(slot) else ''
        rows[slot] = (f'{ident[1]} · {ident[0]}',
                      f'은동전 {guess["은동전"]} · 공물 {guess["마족 공물"]} · {when} (기록 {card["seen_at"]}){cleaned}')
    return rows


# ---- 게임 창 (공방의 Python에는 pywin32가 없어 ctypes로) ----
user32 = ctypes.windll.user32


def game_window():
    """마비노기 모바일 창 핸들(Unity 창 중 제목에 '마비노기'). 없으면 None."""
    found = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        name, title = ctypes.create_unicode_buffer(64), ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, name, 64)
        user32.GetWindowTextW(hwnd, title, 256)
        if user32.IsWindowVisible(hwnd) and name.value == 'UnityWndClass' and '마비노기' in title.value:
            found.append(hwnd)
        return True
    user32.EnumWindows(proc(visit), 0)
    return found[0] if found else None


def client_size(hwnd):
    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    return rect.right - rect.left, rect.bottom - rect.top


def fit_window(hwnd, width=800, height=600):
    """게임 화면(클라이언트 영역)이 width×height가 되게 창 크기를 바꾼다. 성공하면 True.

    게임이 관리자 권한이라 관리자 권한(exe)이 아니면 Windows가 막는다.
    """
    style = user32.GetWindowLongW(hwnd, -16)        # GWL_STYLE
    ex_style = user32.GetWindowLongW(hwnd, -20)     # GWL_EXSTYLE
    rect = wintypes.RECT(0, 0, width, height)
    user32.AdjustWindowRectEx(ctypes.byref(rect), style, False, ex_style)
    if user32.IsZoomed(hwnd):
        user32.ShowWindow(hwnd, 9)                  # SW_RESTORE: 최대화면 크기를 바꿀 수 없다
    flags = 0x0002 | 0x0004 | 0x0010                # SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE
    user32.SetWindowPos(hwnd, None, 0, 0, rect.right - rect.left, rect.bottom - rect.top, flags)
    return client_size(hwnd) == (width, height)


def run_checks(cli_path):
    """시작 전 점검: [(항목, 통과, 설명)]. CLI를 부르므로 백그라운드에서 부른다."""
    results = []
    hwnd = game_window()
    results.append(('게임 창', bool(hwnd), '찾음' if hwnd else '마비노기 모바일 창이 없습니다. 게임을 켜 주세요.'))
    size = client_size(hwnd) if hwnd else None
    results.append(('게임 화면 800×600', size == (800, 600),
                    f'{size[0]}×{size[1]}' + ('' if size == (800, 600) else ' → 「창 크기 맞추기」를 누르세요')
                    if size else '게임 창이 없습니다'))
    cli = Path(cli_path or '')
    results.append(('게임 CLI', cli.is_file(), str(cli) if cli.is_file()
                    else f'{cli} 없음 → 「생활」 탭 설정에서 CLI 경로를 고쳐 주세요'))
    ingame, note = False, 'CLI가 없습니다'
    if cli.is_file():
        try:
            out = subprocess.run([str(cli), 'get_activity'], capture_output=True, timeout=20,
                                 encoding='utf-8-sig', errors='replace', creationflags=NO_WINDOW)
            data = json.loads(out.stdout.strip())
            body = data.get('body', data) if isinstance(data, dict) else data
            ingame = isinstance(body, dict) and 'error' not in body
            note = '접속 중' if ingame else '캐릭터로 접속해 주세요(캐릭터 선택 화면이면 접속 전으로 보입니다)'
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            note = f'CLI 응답 없음: {error}'
    results.append(('게임 접속', ingame, note))
    if FROZEN:
        admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        results.append(('관리자 권한', admin, '관리자 권한으로 실행 중' if admin
                        else 'exe를 관리자 권한으로 다시 실행해 주세요'))
    else:
        results.append(('관리자 권한', True, '시작할 때 관리자 권한 확인 창이 뜹니다'))
    return results


class Progress:
    """매크로 로그 줄에서 지금 캐릭터·클리어 수·쓴 재화를 모은다(공방 안·별도 프로세스 모두 로그로 본다)."""
    LOGIN = re.compile(r'(\d+)번 캐릭터 접속 · \S+ (\S+) (\d+)레벨')
    KNOWN = re.compile(r'지금 캐릭터는 기록상 (\d+)번 카드')
    SPENT = re.compile(r'입장 · (은동전|마족 공물) (\d+)개 사용')
    END = re.compile(r'종료: (.+)')

    def __init__(self):
        self.character, self.clears, self.silver, self.tribute, self.reason = '-', 0, 0, 0, ''

    def feed(self, line):
        if m := self.LOGIN.search(line):
            self.character = f'{m[1]}번 {m[2]}'
        elif m := self.KNOWN.search(line):
            self.character = f'{m[1]}번'
        elif '회 클리어' in line:
            self.clears += 1
        elif m := self.SPENT.search(line):
            if m[1] == '은동전':
                self.silver += int(m[2])
            else:
                self.tribute += int(m[2])
        elif m := self.END.search(line):
            self.reason = m[1]

    def text(self):
        return (f'지금 캐릭터 {self.character} · 클리어 {self.clears}회 · '
                f'쓴 은동전 {self.silver}개 · 쓴 마족 공물 {self.tribute}개')


def box(title):
    frame = QFrame()
    frame.setObjectName('card')
    lay = QVBoxLayout(frame)
    label = QLabel(title)
    label.setObjectName('accent')  # 색은 공방 테마를 따른다
    lay.addWidget(label)
    return frame, lay


class DungeonPanel(QWidget):
    def __init__(self, state=None, changed=None, save=None, cli_path=None):
        super().__init__()
        state = state or {}
        self.changed = changed or (lambda: None)
        self.save_now = save or (lambda: True)
        self.cli_path = cli_path or (lambda: '')
        self.events = queue.Queue()
        self.installing = False
        self.checking = False
        self.process = None
        self.tail = None
        self.progress = Progress()
        self.tray = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        host = QWidget()
        scroll.setWidget(host)
        lay = QVBoxLayout(host)
        lay.setContentsMargins(24, 20, 24, 20)
        title = QLabel('던전 매크로')
        title.setObjectName('title')
        lay.addWidget(title)
        sub = QLabel('은동전·마족 공물로 던전을 반복하고, 다 쓰면 가방을 정리한 뒤 다른 100레벨 캐릭터로 바꿉니다. '
                     '클릭하는 동안 게임 창이 앞으로 나오고 마우스가 움직입니다 · F12로도 중지')
        sub.setObjectName('muted')
        sub.setWordWrap(True)
        lay.addWidget(sub)
        self.status = QLabel()
        lay.addWidget(self.status)

        # 1. 시작 전 점검
        frame, inner = box('시작 전 점검')
        self.check_labels = QLabel('점검 전')
        self.check_labels.setWordWrap(True)
        inner.addWidget(self.check_labels)
        row = QHBoxLayout()
        self.recheck_button = QPushButton('다시 점검')
        self.recheck_button.clicked.connect(self.run_checks)
        self.fit_button = QPushButton('창 크기 맞추기 (800×600)')
        self.fit_button.clicked.connect(self.fit)
        row.addWidget(self.recheck_button)
        row.addWidget(self.fit_button)
        inner.addLayout(row)
        lay.addWidget(frame)

        # 2. 캐릭터 표
        frame, inner = box('캐릭터 (캐릭터 선택 화면의 카드 순서)')
        self.table = QTableWidget(SLOTS, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([c for c, _ in COLUMNS])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        saved_chars = state.get('characters', {})
        for slot in range(SLOTS):
            # 예전 버전의 전체 설정 '상자 열기·소모품 분해'를 껐었다면 캐릭터별 기본값도 끈다
            opts = {**CHAR_DEFAULT, 'items': state.get('clean', True), **saved_chars.get(str(slot), {})}
            self.table.setItem(slot, 0, QTableWidgetItem(f'{slot + 1}번'))
            self.table.setItem(slot, 1, QTableWidgetItem('미확인'))
            for col, (_, key) in enumerate(COLUMNS):
                if key in CHAR_DEFAULT:
                    item = QTableWidgetItem()
                    item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                    item.setCheckState(Qt.CheckState.Checked if opts[key] else Qt.CheckState.Unchecked)
                    self.table.setItem(slot, col, item)
            self.table.setItem(slot, len(COLUMNS) - 1, QTableWidgetItem(''))
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.table.setMinimumHeight(self.table.verticalHeader().length() + header.height() + 6)
        self.table.itemChanged.connect(lambda *_: self.changed())
        inner.addWidget(self.table)
        note = QLabel('포함을 끄면 그 캐릭터는 고르지 않습니다. 은동전·공물을 끄면 그 재화로 던전을 돌지 않습니다. '
                      '상자·소모품(상자 열기, 소모품 분해, 패션 티켓 조각 보물 상자는 제외)과 장비·룬 분해는 그 '
                      '캐릭터를 바꾸기 전 가방 정리에서 합니다. 셋 다 끄면 가방을 열지 않습니다. 도는 중에 바꾼 설정은 다음 가방 '
                      '정리·캐릭터 전환부터 적용됩니다. 기록이 없는 캐릭터로 시작하면 한 번 바꾸기 전까지 몇 번 '
                      '카드인지 몰라서 모두 켠 것으로 돕니다.')
        note.setObjectName('muted')
        note.setWordWrap(True)
        inner.addWidget(note)
        lay.addWidget(frame)

        # 3. 던전 설정
        frame, inner = box('던전 설정')
        grid = QGridLayout()
        grid.addWidget(QLabel('은동전으로 돌 던전'), 0, 0)
        self.silver_route = self.route_combo(SILVER_ROUTES, state.get('silver_route', 'runda'))
        grid.addWidget(self.silver_route, 0, 1)
        grid.addWidget(QLabel('마족 공물로 돌 던전'), 1, 0)
        self.tribute_route = self.route_combo(TRIBUTE_ROUTES, state.get('tribute_route', 'peka'))
        grid.addWidget(self.tribute_route, 1, 1)
        grid.addWidget(QLabel('룬다 더블 루팅'), 2, 0)
        double_row = QHBoxLayout()
        self.double = QComboBox()
        for label, key in DOUBLE_MODES:
            self.double.addItem(label, key)
        self.double.setCurrentIndex(max(0, self.double.findData(state.get('double', 'threshold'))))
        self.double_min = QSpinBox()
        self.double_min.setRange(20, 100)
        self.double_min.setSuffix('개 이상일 때')
        self.double_min.setValue(state.get('double_min', 20))
        double_row.addWidget(self.double, 1)
        double_row.addWidget(self.double_min)
        grid.addLayout(double_row, 2, 1)
        self.double.currentIndexChanged.connect(self.double_mode_changed)
        self.double_min.valueChanged.connect(lambda *_: self.changed())
        self.double_mode_changed()
        grid.addWidget(QLabel('룬 분해 등급'), 3, 0)
        grades = QGridLayout()
        chosen = state.get('rune_grades', DEFAULT_RUNE_GRADES)
        self.grades = {}
        for i, name in enumerate(GRADES):
            check = QCheckBox(name)
            check.setChecked(name in chosen)
            check.toggled.connect(lambda *_: self.changed())
            grades.addWidget(check, i // 5, i % 5)
            self.grades[name] = check
        grid.addLayout(grades, 3, 1)
        grid.setColumnStretch(1, 1)
        inner.addLayout(grid)
        note = QLabel('다른 던전은 직접 플레이를 녹화해 추가할 수 있습니다. 룬 분해 등급을 모두 끄면 룬 분해를 하지 않습니다.')
        note.setObjectName('muted')
        note.setWordWrap(True)
        inner.addWidget(note)
        lay.addWidget(frame)

        # 4. 실행 옵션
        frame, inner = box('실행 옵션')
        self.switch = QCheckBox('재화를 다 쓰면 다른 100레벨 캐릭터로 바꾸기')
        self.wings = QCheckBox('다른 지역으로 갈 때 정령의 날개 사용')
        self.wait = QCheckBox('모든 캐릭터의 재화가 부족하면 충전될 때까지 기다렸다 이어서 돌기')
        self.notify = QCheckBox('끝나거나 멈추면 Windows 알림')
        for check, key, default in ((self.switch, 'switch', True),
                                    (self.wings, 'wings', True), (self.wait, 'wait', False),
                                    (self.notify, 'notify', True)):
            check.setChecked(state.get(key, default))
            check.toggled.connect(lambda *_: self.changed())
            inner.addWidget(check)
        grid = QGridLayout()
        grid.addWidget(QLabel('던전 밖에서 시작할 던전'), 0, 0)
        self.start_route = QComboBox()
        self.start_route.addItems([name for name, _ in STARTS])
        self.start_route.setCurrentIndex(state.get('start', 0))
        self.start_route.currentIndexChanged.connect(lambda *_: self.changed())
        grid.addWidget(self.start_route, 0, 1)
        grid.addWidget(QLabel('클리어 횟수 제한 (0 = 재화를 다 쓸 때까지)'), 1, 0)
        self.runs = QSpinBox()
        self.runs.setRange(0, 9999)
        self.runs.setValue(state.get('runs', 0))
        self.runs.valueChanged.connect(lambda *_: self.changed())
        grid.addWidget(self.runs, 1, 1)
        grid.setColumnStretch(1, 1)
        inner.addLayout(grid)
        lay.addWidget(frame)

        # 5. 실행
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
        self.progress_label = QLabel(self.progress.text())
        lay.addWidget(self.progress_label)
        lay.addWidget(QLabel('실행 기록'))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(220)
        lay.addWidget(self.log)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(500)
        self.update_buttons()
        self.show_roster()

    @staticmethod
    def route_combo(choices, current):
        combo = QComboBox()
        for key, name in choices:
            combo.addItem(name, key)
        combo.setCurrentIndex(max(0, combo.findData(current)))
        return combo

    def double_mode_changed(self, *_):
        self.double_min.setEnabled(self.double.currentData() == 'threshold')
        self.changed()

    def characters(self):
        chars = {}
        for slot in range(SLOTS):
            chars[str(slot)] = {key: self.table.item(slot, col).checkState() == Qt.CheckState.Checked
                                for col, (_, key) in enumerate(COLUMNS) if key in CHAR_DEFAULT}
        return chars

    def state(self):
        return {'switch': self.switch.isChecked(), 'clean': True,
                'wings': self.wings.isChecked(), 'wait': self.wait.isChecked(),
                'notify': self.notify.isChecked(), 'start': self.start_route.currentIndex(),
                'runs': self.runs.value(), 'silver_route': self.silver_route.currentData(),
                'tribute_route': self.tribute_route.currentData(), 'double': self.double.currentData(),
                'double_min': self.double_min.value(),
                'rune_grades': [name for name, check in self.grades.items() if check.isChecked()],
                'characters': self.characters()}

    def arguments(self, check=False):
        if check:
            return ['--check']
        args = []
        if not self.switch.isChecked():
            args.append('--no-switch')
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
        self.progress.feed(text)
        self.progress_label.setText(self.progress.text())

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
        """탭을 열 때마다 부른다. 점검을 다시 하고, 처음이면(또는 requirements.txt가 바뀌었으면) 설치한다."""
        self.show_roster()
        self.run_checks()
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

    def run_checks(self):
        if self.checking:
            return
        self.checking = True
        self.check_labels.setText('점검 중…')
        path = self.cli_path()
        threading.Thread(target=lambda: self.events.put(('checks', run_checks(path))), daemon=True).start()

    def show_checks(self, results):
        self.checking = False
        self.check_labels.setText('\n'.join(f'{"✓" if ok else "✗"} {name} · {note}' for name, ok, note in results))

    def fit(self):
        hwnd = game_window()
        if not hwnd:
            self.message('게임 창을 찾지 못했습니다. 게임을 켠 뒤 다시 누르세요.')
        elif fit_window(hwnd):
            self.message('게임 화면을 800×600으로 맞췄습니다.')
        else:
            size = client_size(hwnd)
            self.message(f'창 크기를 바꾸지 못했습니다(지금 {size[0]}×{size[1]}). '
                         + ('게임 설정에서 창 모드인지 확인하세요.' if FROZEN
                            else '관리자 권한으로 실행한 exe에서만 바꿀 수 있습니다. 게임 창을 직접 맞춰 주세요.'))
        self.run_checks()

    def launch(self, check=False):
        if self.process is not None or not installed():
            return
        if not self.save_now():  # 매크로는 시작할 때 settings.json의 던전 설정을 읽는다
            self.message('설정을 저장하지 못해 시작하지 않았습니다.')
            return
        args = self.arguments(check)
        self.progress = Progress()
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
        rows = roster_rows()
        self.table.blockSignals(True)
        for slot in range(SLOTS):
            who, money = rows.get(slot, ('미확인', '기록 없음 · 한 번 돌면 채워집니다'))
            self.table.item(slot, 1).setText(who)
            self.table.item(slot, len(COLUMNS) - 1).setText(money)
        self.table.blockSignals(False)

    def finished_run(self):
        self.process = None
        self.tail = None
        self.message('매크로 종료')
        self.show_roster()
        self.update_buttons()
        self.notify_end()

    def notify_end(self):
        if not self.notify.isChecked() or not QSystemTrayIcon.isSystemTrayAvailable():
            return
        if self.tray is None:
            self.tray = QSystemTrayIcon(QIcon(str(ROOT / 'assets' / 'workshop.ico')), self)
        self.tray.show()
        self.tray.showMessage('던전 매크로 종료', self.progress.reason or self.progress.text(),
                              QSystemTrayIcon.MessageIcon.Information, 10000)

    def poll(self):
        while not self.events.empty():
            tag, value = self.events.get_nowait()
            if tag == 'log':
                self.message(value)
            elif tag == 'installed':
                self.installing = False
                self.update_buttons()
            elif tag == 'checks':
                self.show_checks(value)
            elif tag == 'done':
                self.finished_run()
        if self.tail:
            for line in self.tail.read():
                self.message(line)
        if self.process is not None and self.process != 'inside':
            code = finished(self.process)
            if code is not None:
                for line in self.tail.read() if self.tail else []:
                    self.message(line)
                STOP_FILE.unlink(missing_ok=True)
                self.finished_run()
