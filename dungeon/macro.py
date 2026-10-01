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

import cv2
import numpy as np
import win32con
from PIL import Image, ImageStat
from pynput import keyboard

from common import (EXPECTED_SIZE, ROOT, TEMPLATES, Game, cli, currencies, find_game, is_admin,
                    relaunch_as_admin, summarize_activity, summarize_env)
from roster import Roster, clock

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
# 대륙 지도를 왼쪽 위 끝으로 옮기는 드래그(지도 내용을 오른쪽 아래로 끈다). 끝에서는 룬다·페카가 다 보인다.
# 가장자리의 버튼을 잘못 누르지 않게 화면 중앙에서 짧게 여러 번 끈다.
MAP_PAN = ((400, 300), (560, 420))
# 필드 오른쪽 위 미니맵 옆 "ESC". 지도·가방·메뉴처럼 화면을 덮는 창이 열리면 가려진다.
# ("Home"은 알림 아이콘이 늘면 왼쪽으로 밀려서 쓰지 않는다.)
HUD_REGION = (600, 0, 720, 60)
CRUMB_REGION = (0, 0, 160, 60)  # 지도 왼쪽 위 "울라 대륙". 지도가 열렸을 때만 있다
# 레벨업·시즌 스킬·스킬 획득 알림 창이 떠 있으면 조작이 먹히지 않는다. 화면 조작은 이만큼 다시 시도한다.
PATIENCE = 180
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
        self.roster = Roster()   # 카드별 마지막으로 본 재화(dungeon/characters.json)
        self.slot = None         # 지금 캐릭터의 카드 번호(0부터). 모르면 선택 화면에서 알아낸다
        self.ident = None
        self.done_ids = set()    # 이번 실행에서 끝낸 캐릭터(카드와 다르게 접속됐는지 확인)
        self.skip_slots = set()  # 100레벨 표시였지만 접속해 보니 아니었던 카드

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
            if self.ready(a):
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
        def open_tidy():
            # 무게를 넘긴 상태에서는 가방을 열면 정리 창이 바로 뜬다. 가방만 열렸으면 "정리"를 누른다.
            if self.hud():
                self.game.key(VK_I)
            else:
                self.game.click(*TIDY_BUTTON)

        def click_if(name, spot):
            return lambda: self.game.find(name) and self.game.click(*spot)

        self.until('정리 창', lambda: self.game.find('tidy_title'), open_tidy, first=True)
        for name, spot in TIDY_OPTIONS.items():
            want = name == TIDY_KEEP
            self.until(f'정리 항목 "{name}" {"켜기" if want else "끄기"}',
                       lambda: self.tidy_checked(spot, self.game.capture()) == want,
                       lambda: self.game.click(*spot), every=2, first=True)
        self.until('정리 대상 화면', lambda: self.game.find('tidy_targets'),
                   click_if('tidy_title', TIDY_START), first=True)
        self.wait(0.8)  # 화면이 서서히 나타나는 동안에는 버튼이 눌리지 않는다
        self.until('정리 완료 화면', lambda: self.game.find('tidy_done'),
                   click_if('tidy_targets', TIDY_CONFIRM), every=6, first=True)
        self.wait(0.8)
        self.until('정리 완료 닫기', lambda: not self.game.find('tidy_done'),
                   lambda: self.game.click(*TIDY_CONFIRM), first=True)
        self.wait(1)
        self.until('가방 닫기', self.hud, lambda: self.game.click(*BAG_CLOSE), first=True)
        after, _ = self.weight()
        self.log(f'가방 정리 완료 · 무게 {before:.0f} → {after:.0f}')

    def until(self, what, done, retry, timeout=PATIENCE, every=4, first=False):
        """Wait for done(), calling retry() every `every` seconds meanwhile (right away if `first`).

        레벨업·시즌 스킬·스킬 획득 같은 알림 창이 떠 있는 동안에는 클릭과 키가 먹히지 않는다.
        창이 사라질 때까지 같은 조작을 다시 시도한다. retry는 현재 화면을 보고 필요한 것만 해야 한다.
        """
        deadline = time.monotonic() + timeout
        last = None if first else time.monotonic()
        while True:
            self.check()
            result = done()
            if result:
                return result
            now = time.monotonic()
            if now > deadline:
                raise Stop(f'{what}: {timeout}초 동안 확인되지 않았습니다.')
            if last is None or now - last >= every:
                retry()
                last = time.monotonic()
            self.wait(0.7)

    def hud(self, image=None):
        """True while the field HUD shows, i.e. no full-screen window (map, bag, menu) is open."""
        return bool(self.game.find('hud_esc', image, region=HUD_REGION))

    def map_open(self, image=None):
        """지도 왼쪽 위 "울라 대륙"이 보이면 지도가 열린 것이다.

        뒤 배경이 지도 무늬라 지도를 옮기면 달라지므로 밝은 글자 픽셀만 비교한다.
        """
        image = image if image is not None else self.game.capture()
        left, top, right, bottom = CRUMB_REGION

        def bright(a):
            return (cv2.cvtColor(np.asarray(a), cv2.COLOR_RGB2GRAY) > 200).astype(np.float32)
        frame = bright(image.crop((left, top, right, bottom)))
        needle = bright(Image.open(TEMPLATES / 'map_crumb.png').convert('RGB'))
        if frame.max() == 0:
            return False
        return cv2.minMaxLoc(cv2.matchTemplate(frame, needle, cv2.TM_CCOEFF_NORMED))[1] >= 0.7

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
                self.remember()
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

    def remember(self, money=None):
        if self.slot is not None and self.ident:
            self.roster.record(self.slot, self.ident, money or currencies())

    def run_all(self, switch=True):
        """run() each level-100 character in turn until every one is out of currency."""
        self.ident = self.me()['id']
        self.slot = self.roster.slot_of(self.ident)
        if self.slot is not None:
            self.log(f'지금 캐릭터는 기록상 {self.slot + 1}번 카드')
        while True:
            try:
                self.run()
                return
            except Exhausted as reason:
                if not switch:
                    raise
                self.log(f'{reason} 다음 캐릭터로 바꿉니다.')
            self.start = None
            self.switch_character()

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
        """ESC 메뉴 → 게임 종료 → 캐릭터 선택 화면으로. 로딩 중(검은 화면)에는 아무것도 누르지 않는다."""
        def step():
            image = self.game.capture()
            button = self.game.find('menu_quit', image, region=MENU_QUIT_REGION)
            if self.game.find('quit_title', image):
                self.game.click(*TO_SELECT)
            elif button:
                self.game.click(*button)
            elif self.hud(image):
                self.game.key(VK_ESC)

        self.until('캐릭터 선택 화면', lambda: self.game.find('select_title'), step, first=True)
        self.wait(2)  # 카드가 다 그려질 때까지

    def in_game(self):
        return self.ready(summarize_activity(cli('get_activity')))

    @staticmethod
    def ready(a):
        # 접속·이동 로딩 중에는 not_in_game 오류나, 던전 상태 같은 항목이 비어 있는 응답이 온다.
        return isinstance(a, dict) and 'error' not in a and a.get('dungeon') is not None

    def switch_character(self):
        """Save the current character's currencies and log in to a level-100 one that can run a dungeon.

        재화는 카드별로 파일에 남기고 충전 속도로 지금 양을 추정해, 돌 수 없는 캐릭터는 접속하지 않는다.
        """
        before, money = self.me(), currencies()
        self.done_ids.add(before['id'])
        self.to_character_select()
        self.game.scroll(400, 300, 10)  # 목록 맨 위로
        self.wait(1)
        cards = self.cards(self.game.capture())
        current = next((c['slot'] for c in cards if c['selected']), None)
        self.slot, self.ident = (current if current is not None else self.slot), before['id']
        if isinstance(money, dict) and 'error' not in money:
            self.remember(money)
        costs = {route.currency: route.cost for route in ROUTES}
        candidates = [c for c in cards if c['lv100'] and c['slot'] not in self.skip_slots]
        self.log('캐릭터 선택 · ' + ' / '.join(self.roster.describe(c['slot'], costs) for c in candidates))
        todo = [c for c in candidates if self.roster.ready_at(c['slot'], costs) <= self.roster.now()]
        if not todo:
            soonest = min(candidates, key=lambda c: self.roster.ready_at(c['slot'], costs), default=None)
            when = (f' 가장 빠른 것은 {soonest["slot"] + 1}번 캐릭터, '
                    f'{clock(self.roster.ready_at(soonest["slot"], costs))}쯤부터입니다.') if soonest else ''
            raise Stop(f'모든 100레벨 캐릭터의 은동전·마족 공물이 부족합니다.{when} 종료합니다.')
        card = todo[0]
        self.until(f'{card["slot"] + 1}번 캐릭터 카드 선택',
                   lambda: self.cards(self.game.capture())[card['slot']]['selected'],
                   lambda: self.game.click(*card['center']), every=2, first=True)
        self.log(f'{card["slot"] + 1}번 카드 선택 · 화면 {self.screenshot("select")}')
        # 접속하면 CLI가 응답한다. 선택 화면이 그대로면 게임 시작을 다시 누른다.
        self.until('캐릭터 접속', self.in_game,
                   lambda: self.game.find('select_title') and self.game.click(*GAME_START), every=8, first=True)
        self.wait(3)
        now = self.me()
        self.log(f'{card["slot"] + 1}번 캐릭터 접속 · {now["realm"]} {now["job"]} {now["level"]}레벨 · '
                 f'재화 {currencies()}')
        if now['id'] in self.done_ids:
            # 고른 카드와 다른 캐릭터다. 기록하지 않고 선택 화면에서 다시 고른다.
            self.log(f'이미 끝낸 캐릭터로 접속됨({now["realm"]} {now["job"]}) · 다음 캐릭터로 넘어갑니다.')
            return self.switch_character()
        self.slot, self.ident = card['slot'], now['id']
        self.remember()
        if now['level'] != 100:
            self.log('100레벨이 아니라 건너뜁니다.')
            self.skip_slots.add(card['slot'])
            return self.switch_character()

    def me(self):
        """Who is logged in: level, job, realm and an id to tell characters apart (CLI has no name)."""
        def read():
            i = cli('get_my_info')
            return i if isinstance(i, dict) and 'error' not in i and i.get('Level') is not None else None
        info = self.wait_for('캐릭터 정보', read, 60, interval=1)

        def value(key):
            v = info.get(key)
            return v.get('Value') if isinstance(v, dict) else v
        out = {'level': value('Level'), 'job': value('EnabledCombatJobDisplayName'),
               'realm': value('RealmName'), 'title': value('Title')}
        out['id'] = (out['realm'], out['job'], out['title'])
        return out

    def next_route(self, route):
        nxt = self.other(route)
        if not self.affordable(nxt):
            raise Exhausted(f'{route.currency}·{nxt.currency} 모두 부족합니다.')
        self.log(f'{route.currency} 부족 → {nxt.name}로 이동')
        return nxt

    def wait_clear(self, route):
        self.log(f'{route.name} 진행 중 · 클리어 대기')
        started = waiting = time.monotonic()
        nudged = 0
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
                    waiting, nudged = time.monotonic(), 0  # 정리 후 다시 켜지는지 지켜본다
                    continue
                # 알림 창에 클릭이 막힐 수 있어 20초마다 다시 누른다.
                if elapsed > PATIENCE and not a['combat']:
                    raise Stop(f'자동 진행이 {PATIENCE}초 동안 시작되지 않습니다.')
                if elapsed > 20 and time.monotonic() - nudged > 20:
                    self.log('자동 진행이 꺼져 있음 · 퀘스트 추적을 눌러 시작')
                    self.game.click(*QUEST_TRACKER)
                    nudged = time.monotonic()
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
        deadline = time.monotonic() + PATIENCE
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
        def press_exit():
            exit_button = self.game.find('reward_exit')
            if exit_button:
                self.game.click(*exit_button)
            elif self.hud():
                self.game.key(VK_ESC)

        self.until('던전 퇴장', lambda: self.activity()['dungeon'] == 'NotInDungeon', press_exit,
                   every=8, first=True)
        self.wait(2)

    def confirm_entry(self, route):
        """On the entry screen: set double looting, press 입장하기, verify the charge."""
        def nudge():
            # 선택 창의 Space("n층 n구역 진입")나 보상 화면의 다시 하기가 먹히지 않았으면 다시 누른다.
            image = self.game.capture()
            retry = self.game.find('reward_retry', image)
            if self.game.find(route.panel_template, image):
                self.game.key(VK_SPACE)
            elif retry:
                self.game.click(*retry)

        button = self.until('입장 화면', lambda: self.game.find(route.enter_template), nudge, every=5)
        if route.double_cost:
            want = self.balance(route) >= route.double_cost
            have = bool(self.game.find('double_on', region=DOUBLE_REGION))
            if want != have:
                self.log('더블 루팅 ' + ('켜기' if want else '끄기 (은동전 20개 미만)'))
                self.until('더블 루팅 ' + ('켜기' if want else '끄기'),
                           lambda: bool(self.game.find('double_on', region=DOUBLE_REGION)) == want,
                           lambda: self.game.click(*DOUBLE_BUTTON), every=2, first=True)
            button = self.game.find(route.enter_template) or button
        before = self.balance(route)
        self.game.click(*button)
        # 입장 중에는 Entering → NotInDungeon → InProgress 순서로 잠깐 밖으로 보인다.
        # 입장 화면이 그대로 남아 있으면 입장하기를 다시 누른다.
        self.until('던전 입장', lambda: self.activity()['dungeon'] == 'InProgress',
                   lambda: (spot := self.game.find(route.enter_template)) and self.game.click(*spot), every=8)
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
            self.until(f'{route.name} 선택 창', lambda: self.game.find(route.panel_template),
                       lambda: self.activity()['interaction'] == 'EnterDungeon' and self.game.key(VK_SPACE))
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
        # 알림 창에 막혀 지도가 닫히거나 클릭이 빠지면 지도 열기부터 다시 고른다.
        go = self.until('여기로 가기', lambda: self.game.find('map_go'),
                        lambda: self.pick_on_map(route, space), every=8, first=True)
        self.game.click(*go)
        if space != route.field_space and self.use_wings:
            self.wait(1.5)
            self.game.key(VK_T)
        return self.wait_arrival(route)

    def open_map(self):
        """M으로 지도를 연다. 알림 창에 M이 먹히지 않으면 다시 누르고, 다른 창이 열려 있으면 ESC로 닫는다."""
        def press():
            image = self.game.capture()
            if self.map_open(image):
                return
            if self.hud(image):
                self.game.key(VK_M)
            elif np.asarray(image).mean() > 10:  # 로딩(검은 화면)이 아니면 캐릭터 창 같은 다른 창이다
                self.game.key(VK_ESC)

        self.until('지도 열기', self.map_open, press, first=True)
        self.wait(1)

    def pick_on_map(self, route, space):
        """지도에서 던전을 고른다. 이멘마하는 동부/남부 탭 목록, 그 밖은 목록에 없으면 대륙 지도에서 고른다."""
        # 지도 위의 자리를 누르는 것이라 지도가 열려 있을 때만 누른다. 필드에서 누르면
        # 왼쪽 위 "울라 대륙" 자리는 캐릭터 초상화라 캐릭터 창이 열린다.
        self.open_map()
        item = self.game.find(route.map_template, region=MAP_LIST_REGION)
        if not item and space == '이멘마하':
            self.game.click(*route.map_tab)
            self.wait(1.5)
            item = self.game.find(route.map_template, region=MAP_LIST_REGION)
        if item:
            self.game.click(*item)
            return
        label = self.game.find(route.world_template, threshold=0.9)
        if not label:
            if not self.map_open():
                return  # 그사이 지도가 닫혔다. 다음 시도에서 다시 연다
            self.game.click(*MAP_CONTINENT)  # "울라 대륙"
            self.wait(2)
            label = self.game.find(route.world_template, threshold=0.9)
        if not label:
            # 던전이 화면 밖이면 지도를 왼쪽 위 끝까지 옮긴다. 거기서는 룬다와 페카가 다 보인다.
            self.pan_map_top_left()
            label = self.game.find(route.world_template, threshold=0.9)
            if not label:
                self.log(f'대륙 지도에서 {route.name}을 찾지 못함 · 화면 {self.screenshot("map")}')
        if label:
            self.game.click(label[0] + route.world_offset[0], label[1] + route.world_offset[1])

    def pan_map_top_left(self, limit=20):
        """Drag the map until it stops moving (its top-left edge)."""
        before = np.asarray(self.game.capture(), dtype=np.int16)
        for _ in range(limit):
            # 지도가 열려 있을 때만 끈다. 다른 창 위에서 끌면 엉뚱한 것을 누른다.
            if not self.map_open():
                self.log('지도가 열려 있지 않아 지도 옮기기를 멈춤')
                return
            self.game.drag(*MAP_PAN)
            self.wait(0.5)
            after = np.asarray(self.game.capture(), dtype=np.int16)
            if np.abs(after - before).mean() < 2:  # 그대로면 끝에 닿았다
                return
            before = after

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
        try:
            self.until(f'{route.name} 선택 창', lambda: self.game.find(route.panel_template),
                       lambda: self.activity()['interaction'] == 'EnterDungeon' and self.game.key(VK_SPACE),
                       timeout=60, first=True)
            return True
        except Stop:
            self.check()
            return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=int, help='이 횟수만큼 클리어하면 종료')
    parser.add_argument('--start', choices=[r.key for r in ROUTES],
                        help='던전 밖에서 시작할 때 이 던전부터 진행 (runda/peka)')
    parser.add_argument('--check', action='store_true', help='조작 없이 현재 위치·재화·화면 인식 결과만 출력')
    parser.add_argument('--no-wings', action='store_true', help='던전 간 이동에 정령의 날개(T)를 쓰지 않음')
    parser.add_argument('--no-switch', action='store_true', help='재화가 떨어져도 다른 캐릭터로 바꾸지 않고 종료')
    parser.add_argument('--story', type=int, nargs='?', const=98, metavar='LEVEL',
                        help='던전 대신 스토리 퀘스트를 넘기며 이 레벨(기본 98)까지 키움')
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
        log(('스토리' if args.story else '던전') + ' 매크로 시작 · F12 중지')
        macro = Macro(Game(hwnd), stop, log, max_runs=args.runs, use_wings=not args.no_wings,
                      start=args.start)
        if args.check:
            macro.report()
            return
        try:
            if args.story:
                from story import Story
                Story(macro, args.story).run()
            else:
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
