"""던전 매크로 제작용 기록기.

사용자가 직접 던전을 도는 동안 게임 창 안의 클릭·키 입력, 그 순간의 게임 화면,
CLI 상태 변화를 dungeon/recordings/<시각>/ 에 저장한다. F9 수동 캡처, F12 종료.
"""
import json
import queue
import sys
import threading
import time
from datetime import datetime

import mss
import win32con
import win32gui
from PIL import Image
from pynput import keyboard, mouse

from common import (EXPECTED_SIZE, ROOT, cli, client_rect, currencies, find_game, is_admin,
                    print_window, relaunch_as_admin, summarize_activity, summarize_env)


class Recorder:
    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.out = ROOT / 'recordings' / datetime.now().strftime('%Y%m%d-%H%M%S')
        self.out.mkdir(parents=True)
        self.events = (self.out / 'events.jsonl').open('a', encoding='utf-8')
        self.lock = threading.Lock()
        self.saves = queue.Queue()
        self.stop = threading.Event()
        self.count = 0
        self.env = None
        self.activity = None
        self.started = time.monotonic()

    def log(self, kind, **fields):
        with self.lock:
            fields = {'t': round(time.monotonic() - self.started, 2), 'type': kind, **fields}
            self.events.write(json.dumps(fields, ensure_ascii=False) + '\n')
            self.events.flush()
        print(json.dumps(fields, ensure_ascii=False))

    def shot(self, tag):
        """Grab now (cheap) and let the worker thread encode the PNG."""
        left, top, width, height = client_rect(self.hwnd)
        image = print_window(self.hwnd, width, height)
        if image is None:
            # Screen grab fallback: whatever covers the game gets captured too.
            with mss.mss() as screen:
                raw = screen.grab({'left': left, 'top': top, 'width': width, 'height': height})
            image = Image.frombytes('RGB', raw.size, raw.rgb)
            tag += '_screen'
        with self.lock:
            self.count += 1
            name = f'{self.count:04d}_{tag}.png'
        self.saves.put((name, image))
        return name

    def shot_later(self, tag, delay=0.8):
        threading.Timer(delay, lambda: self.stop.is_set() or self.shot(tag)).start()

    def save_worker(self):
        while True:
            item = self.saves.get()
            if item is None:
                return
            name, image = item
            image.save(self.out / name, compress_level=1)

    def poll_worker(self):
        last_env_poll = 0
        while not self.stop.is_set():
            activity = summarize_activity(cli('get_activity'))
            if activity != self.activity:
                changed_dungeon = (isinstance(self.activity, dict) and isinstance(activity, dict)
                                   and self.activity.get('dungeon') != activity.get('dungeon'))
                self.activity = activity
                self.log('state', activity=activity, shot=self.shot('state'),
                         **({'currencies': currencies()} if changed_dungeon else {}))
            if time.monotonic() - last_env_poll >= 3:
                last_env_poll = time.monotonic()
                env = summarize_env(cli('get_current_environment'))
                if not isinstance(env, dict) or not isinstance(self.env, dict) or env.get('space') != self.env.get('space'):
                    self.log('location', env=env)
                self.env = env
            self.stop.wait(1)

    def inside(self, x, y):
        left, top, _, _ = client_rect(self.hwnd)
        # Clicks on a window lying over the game area are not game clicks.
        hit = win32gui.GetAncestor(win32gui.WindowFromPoint((x, y)), win32con.GA_ROOT)
        return hit == self.hwnd, (x - left, y - top)

    def on_click(self, x, y, button, pressed):
        if not pressed or self.stop.is_set():
            return
        inside, (rx, ry) = self.inside(x, y)
        if not inside:
            return
        self.log('click', button=button.name, x=rx, y=ry, env=self.env, shot=self.shot('click'))
        self.shot_later('after')

    def on_key(self, key):
        if key == keyboard.Key.f12:
            self.stop.set()
            return False
        if key == keyboard.Key.f9:
            self.log('manual', shot=self.shot('manual'))
            return
        if win32gui.GetForegroundWindow() != self.hwnd:
            return
        name = getattr(key, 'char', None) or getattr(key, 'name', None) or str(key)
        self.log('key', key=name, env=self.env, shot=self.shot('key'))
        self.shot_later('after')

    def run(self):
        _, _, width, height = client_rect(self.hwnd)
        self.log('start', size=[width, height], currencies=currencies(),
                 env=summarize_env(cli('get_current_environment')), shot=self.shot('start'))
        workers = [threading.Thread(target=self.save_worker, daemon=True),
                   threading.Thread(target=self.poll_worker, daemon=True)]
        for worker in workers:
            worker.start()
        mouse_listener = mouse.Listener(on_click=self.on_click)
        mouse_listener.start()
        with keyboard.Listener(on_press=self.on_key) as key_listener:
            key_listener.join()
        mouse_listener.stop()
        time.sleep(1)  # let pending "after" shots fire
        self.log('end', currencies=currencies(), env=summarize_env(cli('get_current_environment')))
        self.saves.put(None)
        workers[0].join()
        self.events.close()
        print(f'저장 위치: {self.out}')


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    if not is_admin():
        if not relaunch_as_admin(__file__):
            sys.exit('관리자 권한 실행이 취소되었습니다. 게임이 관리자 권한이라 기록기도 관리자 권한이 필요합니다.')
        print('관리자 권한 창에서 기록기를 실행했습니다.')
        return
    hwnd = find_game()
    if not hwnd:
        sys.exit('마비노기 모바일 창을 찾지 못했습니다.')
    _, _, width, height = client_rect(hwnd)
    if (width, height) != EXPECTED_SIZE:
        print(f'경고: 게임 화면이 {width}x{height}입니다. {EXPECTED_SIZE[0]}x{EXPECTED_SIZE[1]}로 맞춰주세요.')
    print('기록 시작 · F9 수동 캡처 · F12 종료')
    try:
        Recorder(hwnd).run()
    finally:
        input('엔터를 누르면 창을 닫습니다.')


if __name__ == '__main__':
    main()
