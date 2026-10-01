"""게임 창·CLI·입력·화면 인식 공통 도구. 게임이 관리자 권한이라 호출 측도 관리자 권한이어야 한다."""
import ctypes
import json
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import win32api
import win32con
import win32gui
import win32process
import win32ui
from PIL import Image

ROOT = Path(__file__).resolve().parent
SETTINGS = ROOT.parent / 'workshop' / 'settings.json'
TEMPLATES = ROOT / 'templates'
DEFAULT_CLI = r"C:\Nexon\MabinogiMobile\MabinogiMobile_CLI.exe"
EXPECTED_SIZE = (800, 600)
# 반짝이는 버튼은 프레임마다 조금씩 달라 기준을 낮춘다(다른 화면 최고점 0.75 미만 확인).
THRESHOLDS = {'enter_runda': 0.78, 'enter_deep': 0.78}

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except (AttributeError, OSError):
    ctypes.windll.user32.SetProcessDPIAware()


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def relaunch_as_admin(script):
    """Re-run `script` elevated in its own console; True if the UAC prompt was accepted."""
    params = ' '.join(f'"{arg}"' for arg in [str(Path(script).resolve()), *sys.argv[1:]])
    return ctypes.windll.shell32.ShellExecuteW(None, 'runas', sys.executable, params, str(ROOT), 1) > 32


def cli_path():
    try:
        return json.loads(SETTINGS.read_text(encoding='utf-8')).get('cli') or DEFAULT_CLI
    except (OSError, ValueError):
        return DEFAULT_CLI


def cli(command, body=None):
    """Call the game CLI; errors come back as {'error': ...} instead of raising."""
    args = [cli_path(), command] + ([json.dumps(body, ensure_ascii=False)] if body else [])
    try:
        p = subprocess.run(args, capture_output=True, encoding='utf-8-sig', errors='replace',
                           timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        data = json.loads(p.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return {'error': repr(error)}
    return data.get('body', data) if isinstance(data, dict) else data


def currencies():
    data = cli('get_currencies')
    if not isinstance(data, list):
        return data
    return {c['DisplayName']: c['Amount'] for c in data
            if c.get('DisplayName') in ('은동전', '마족 공물', '정령의 날개')}


def summarize_activity(a):
    if not isinstance(a, dict) or 'error' in a:
        return a
    return {
        'dungeon': (a.get('Dungeon') or {}).get('State'),
        'boss': (a.get('Dungeon') or {}).get('IsBossBattleInProgress'),
        'auto': a.get('IsAutoPlaying'),
        'auto_target': a.get('AutoPlayTarget'),
        'travel': a.get('IsAutoTraveling'),
        'dead': a.get('IsDead'),
        'reviving': a.get('IsReviving'),
        'combat': a.get('IsInCombat'),
        'dialogue': a.get('IsDialoguePlaying'),
        'selecting': a.get('IsWaitingForSelection'),
        'target': (a.get('Interaction') or {}).get('TargetKind'),
        'interaction': (a.get('Interaction') or {}).get('AvailableInteractionType'),
        'last_interaction': (a.get('Interaction') or {}).get('LastRunningInteractionType'),
        'main_button': (a.get('Mode') or {}).get('MainButtonState'),
    }


def summarize_env(e):
    if not isinstance(e, dict) or 'error' in e:
        return e
    pos = e.get('WorldPosition') or {}
    return {'space': e.get('GameSpaceDisplayName'), 'channel': e.get('ChannelDisplayName'),
            'x': round(pos.get('X', 0), 1), 'y': round(pos.get('Y', 0), 1)}


def find_game():
    found = []

    def visit(hwnd, _):
        if (win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd) == 'UnityWndClass'
                and '마비노기' in win32gui.GetWindowText(hwnd)):
            found.append(hwnd)
    win32gui.EnumWindows(visit, None)
    return found[0] if found else None


def client_rect(hwnd):
    left, top = win32gui.ClientToScreen(hwnd, (0, 0))
    _, _, width, height = win32gui.GetClientRect(hwnd)
    return left, top, width, height


def print_window(hwnd, width, height):
    """Capture the client area even when other windows cover it; None if unsupported."""
    window_dc = win32gui.GetWindowDC(hwnd)
    source = win32ui.CreateDCFromHandle(window_dc)
    memory = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(source, width, height)
        memory.SelectObject(bitmap)
        # PW_CLIENTONLY | PW_RENDERFULLCONTENT
        if not ctypes.windll.user32.PrintWindow(hwnd, memory.GetSafeHdc(), 3):
            return None
        image = Image.frombuffer('RGB', (width, height), bitmap.GetBitmapBits(True), 'raw', 'BGRX', 0, 1)
        return image if image.getbbox() else None
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        memory.DeleteDC()
        source.DeleteDC()
        win32gui.ReleaseDC(hwnd, window_dc)


class Game:
    """Screen reading and input for the game window (client coordinates, 800x600)."""

    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.templates = {}

    def size(self):
        return client_rect(self.hwnd)[2:]

    def capture(self):
        _, _, width, height = client_rect(self.hwnd)
        image = print_window(self.hwnd, width, height)
        if image is None:
            raise RuntimeError('게임 화면을 캡처하지 못했습니다. 관리자 권한으로 실행했는지 확인하세요.')
        return image

    @staticmethod
    def has_template(name):
        return (TEMPLATES / f'{name}.png').exists()

    def template(self, name):
        if name not in self.templates:
            path = TEMPLATES / f'{name}.png'
            self.templates[name] = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
        return self.templates[name]

    def find(self, name, image=None, region=None, threshold=None):
        """Return the template's center (client coords) or None. region = (l, t, r, b)."""
        frame = cv2.cvtColor(np.asarray(image if image is not None else self.capture()), cv2.COLOR_RGB2BGR)
        left, top = 0, 0
        if region:
            left, top, right, bottom = region
            frame = frame[top:bottom, left:right]
        needle = self.template(name)
        if frame.shape[0] < needle.shape[0] or frame.shape[1] < needle.shape[1]:
            return None
        _, score, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(frame, needle, cv2.TM_CCOEFF_NORMED))
        if score < (threshold or THRESHOLDS.get(name, 0.88)):
            return None
        return left + x + needle.shape[1] // 2, top + y + needle.shape[0] // 2

    def focus(self):
        if win32gui.GetForegroundWindow() == self.hwnd:
            return
        if win32gui.IsIconic(self.hwnd):
            win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
        # Foreground changes are only allowed to the input owner; borrow its input queue.
        current = win32process.GetWindowThreadProcessId(win32gui.GetForegroundWindow())[0]
        mine = win32api.GetCurrentThreadId()
        attached = current and current != mine and ctypes.windll.user32.AttachThreadInput(mine, current, True)
        try:
            win32gui.BringWindowToTop(self.hwnd)
            win32gui.SetForegroundWindow(self.hwnd)
        finally:
            if attached:
                ctypes.windll.user32.AttachThreadInput(mine, current, False)
        time.sleep(0.3)
        if win32gui.GetForegroundWindow() != self.hwnd:
            raise RuntimeError('게임 창을 앞으로 가져오지 못했습니다.')

    def click(self, x, y):
        self.focus()
        left, top, _, _ = client_rect(self.hwnd)
        win32api.SetCursorPos((left + x, top + y))
        time.sleep(0.08)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
        time.sleep(0.06)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)
        time.sleep(0.25)

    def scroll(self, x, y, clicks):
        """Mouse wheel over (x, y): positive scrolls up, negative down."""
        self.focus()
        left, top, _, _ = client_rect(self.hwnd)
        win32api.SetCursorPos((left + x, top + y))
        time.sleep(0.08)
        win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, clicks * win32con.WHEEL_DELTA)
        time.sleep(0.25)

    def key(self, vk):
        self.hold([vk], 0.06)

    def hold(self, vks, seconds):
        """Hold keys together (e.g. W+D to move diagonally), then release."""
        self.focus()
        scans = [(vk, win32api.MapVirtualKey(vk, 0)) for vk in vks]
        for vk, scan in scans:
            win32api.keybd_event(vk, scan, 0, 0)
        try:
            time.sleep(seconds)
        finally:
            for vk, scan in scans:
                win32api.keybd_event(vk, scan, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.25)
