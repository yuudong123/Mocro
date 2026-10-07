"""은동전(룬다 일반)·마족 공물(페카 고분 심층 2-1) 던전 반복 매크로.

게임 상태는 CLI로 확인하고, 입장·재도전·이동 조작만 화면 인식과 클릭으로 한다.
현재 캐릭터가 있는 던전부터 돌고, 그 재화가 떨어지면 다른 던전으로 이동한다.
두 재화가 모두 떨어지면 아직 안 한 100레벨 캐릭터로 바꿔 계속한다.
F12로 중지한다. 게임이 관리자 권한이라 이 스크립트도 관리자 권한으로 다시 실행된다.
"""
import argparse
import json
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

from common import (DATA, EXPECTED_SIZE, ROOT, SETTINGS, TEMPLATES, Game, cli, currencies, find_game,
                    is_admin, relaunch_as_admin, summarize_activity, summarize_env)
from roster import Roster, clock

LOGS = DATA / 'logs'
STOP_FILE = DATA / 'stop.request'  # 에린 공방 던전 탭의 중지 버튼이 만든다
VK_I, VK_M, VK_T, VK_ESC, VK_SPACE = 0x49, 0x4D, 0x54, win32con.VK_ESCAPE, win32con.VK_SPACE
VK_A, VK_E, VK_Q, VK_S, VK_W = 0x41, 0x45, 0x51, 0x53, 0x57
SKIP_REGION = (480, 0, 800, 70)  # 오른쪽 위 "장면 넘기기"·"대화 넘기기"·"이야기 넘기기"
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
# 필드 화면에만 있는 표시: 미니맵 옆 "ESC", 나침반 "Space", 채팅창 "Ctrl+Z".
# 지도·가방·메뉴처럼 화면을 덮는 창이 열리면 모두 가려진다. 알림이 뜨면 위쪽만 어두워지거나
# 접속 직후엔 일부만 보이므로 하나라도 보이면 필드로 본다. ("Home"은 알림 아이콘이 늘면 밀려서 쓰지 않는다.)
HUD_ANCHORS = {'hud_esc': (600, 0, 720, 60), 'hud_space': (700, 530, 800, 600), 'hud_chat': (100, 530, 260, 600)}
CRUMB_REGION = (0, 0, 160, 60)  # 지도 왼쪽 위 "울라 대륙". 지도가 열렸을 때만 있다
NOTICE_CLOSE_REGION = (320, 220, 480, 380)  # 레벨업 다이스 획득 같은 알림 가운데의 X
CHOICE_REGION = (100, 470, 700, 600)        # 대화 선택지(초록 둥근 버튼, 몰리 "지하 감옥/몬스터 소굴/…" 등)
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
# 자세히 가방 정리 화면(녹화 20261005-154846, 룬 분해 20261005-160805). 탭은 W/S, 칸은 Q/E로 옮긴다.
BAG_TABS = ['장비', '도구', '아이템', '패션', '탈것·펫']
BAG_TAB_X = [40, 85, 133, 181, 232]          # 선택된 탭은 초록 배경(y 52~66)
# 등급 칩: (왼쪽 끝 x, 가운데 y, 누를 곳 x). 선택되면 밝은 색 테두리가 생긴다. 아무것도 안 고르면 전 등급.
GRADE_CHIPS = {'일반': (55, 113, 78), '고급': (109, 113, 133), '레어': (163, 113, 186),
               '엘리트': (217, 113, 245), '에픽': (281, 113, 304), '전설': (56, 147, 79),
               '전설+': (109, 147, 136), '전설++': (170, 147, 200), '신화': (237, 147, 260), '유니크': (290, 147, 319)}
RUNE_GRADES = ['일반', '고급', '레어', '엘리트', '에픽']  # 룬은 전설 이상을 남긴다(녹화)
DETAIL_CLOSE = (770, 25)                     # 자세히 가방 정리·가방 오른쪽 위 X
BAG_METHODS = {'분해': ((88, 253), (70, 245, 106, 261)), '열기': ((224, 253), (200, 245, 250, 261))}
SELECT_ALL, SELECT_ALL_MARK = (695, 118), (689, 111, 701, 123)  # "전체 선택". 체크되면 초록
RUN_BUTTON = (600, 550, 740, 575)            # "N개 정리하기". 0개면 어두운 초록
BAG_LIST_REGION, SUBTAB_REGION = (440, 130, 790, 540), (440, 70, 760, 105)


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
ROUTE_BY_KEY = {route.key: route for route in ROUTES}
CURRENCY_KEY = {'은동전': 'silver', '마족 공물': 'tribute'}

# 에린 공방 던전 탭에서 고르는 설정(settings.json의 "dungeon"). 없는 값은 이 기본값을 쓴다.
DEFAULT_CONFIG = {
    'silver_route': 'runda',            # 은동전을 쓸 던전(ROUTES의 key). 새 던전은 녹화해 ROUTES에 추가한다
    'tribute_route': 'peka',            # 마족 공물을 쓸 던전
    'double': 'threshold',              # 더블 루팅: off(끔) / always(가능하면 항상) / threshold(double_min개 이상일 때)
    'double_min': 20,
    'rune_grades': RUNE_GRADES,         # 룬 분해 등급. 비우면 룬 분해를 하지 않는다
    'clean': True,                      # 상자 열기·소모품 분해 전체 스위치(--no-clean). 캐릭터별은 items
    'wait': False,                      # 모두 부족하면 충전될 때까지 기다렸다 이어서
    'characters': {},                   # 카드 번호(0부터, 문자열) → CHAR_DEFAULT의 항목
}
# 카드별: 매크로에 포함, 은동전·마족 공물 사용, 캐릭터를 바꾸기 전 상자 열기·소모품 분해, 장비·룬 분해
CHAR_DEFAULT = {'include': True, 'silver': True, 'tribute': True, 'items': True, 'equip': True, 'rune': True}


def load_config():
    """에린 공방 설정(settings.json)의 던전 설정. 파일이 없거나 깨졌으면 기본값."""
    try:
        saved = json.loads(SETTINGS.read_text(encoding='utf-8')).get('dungeon', {})
    except (OSError, ValueError, AttributeError):
        saved = {}
    config = {**DEFAULT_CONFIG, **{k: v for k, v in saved.items() if k in DEFAULT_CONFIG}}
    for key, currency in (('silver_route', '은동전'), ('tribute_route', '마족 공물')):
        route = ROUTE_BY_KEY.get(config[key])
        if route is None or route.currency != currency:
            config[key] = DEFAULT_CONFIG[key]
    return config


class Stop(Exception):
    pass


class Exhausted(Stop):
    """이 캐릭터의 은동전·마족 공물이 모두 부족하다."""


class Macro:
    def __init__(self, game, stop, log, max_runs=None, use_wings=True, start=None, clean=True, config=None):
        self.game, self.stop, self.log = game, stop, log
        self.max_runs, self.use_wings, self.start = max_runs, use_wings, start
        self.clean_allowed = clean      # --no-clean이면 공방 설정과 상관없이 상자·소모품 정리를 안 한다
        self.live = False               # True면 캐릭터를 바꿀 때마다 공방 설정을 다시 읽는다(session)
        self.apply_config(config or {})
        self.runs = 0
        self.roster = Roster()   # 카드별 마지막으로 본 재화(dungeon/characters.json)
        self.slot = None         # 지금 캐릭터의 카드 번호(0부터). 모르면 선택 화면에서 알아낸다
        self.ident = None
        self.done_ids = set()    # 이번 실행에서 끝낸 캐릭터(카드와 다르게 접속됐는지 확인)
        self.map_opened = False  # 이번 이동에서 M으로 지도를 열었다
        self.skip_slots = set()  # 100레벨 표시였지만 접속해 보니 아니었던 카드
        self.fresh = False       # 방금 접속해서 아직 한 판도 안 돌았다(재화를 잘못 읽었을 수 있다)
        self.pending_clean = False  # 카드 번호를 모르는 채로 가방을 정리했다(전환할 때 기록한다)

    # ---- 상태 확인 ----
    def check(self):
        if self.stop.is_set():
            raise Stop('중지 요청(F12 또는 에린 공방 중지 버튼)')

    def wait(self, seconds):
        if self.stop.wait(seconds):
            raise Stop('중지 요청(F12 또는 에린 공방 중지 버튼)')

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

    def apply_config(self, config):
        self.config = {**DEFAULT_CONFIG, **config}
        self.clean = self.clean_allowed and self.config['clean']
        # 은동전 던전, 마족 공물 던전 순서
        self.routes = [ROUTE_BY_KEY[self.config['silver_route']], ROUTE_BY_KEY[self.config['tribute_route']]]

    def reload_config(self):
        """도는 중에 공방에서 바꾼 설정을 가방 정리·캐릭터 전환 전에 다시 읽는다."""
        if self.live:
            self.apply_config(load_config())

    def char_opts(self, slot=None):
        """카드별 설정(공방 캐릭터 표). 지금 캐릭터가 몇 번 카드인지 모르면 기본값(전부 켬)."""
        slot = self.slot if slot is None else slot
        saved = self.config['characters'].get(str(slot), {}) if slot is not None else {}
        return {**CHAR_DEFAULT, **saved}

    def uses(self, route, slot=None):
        """이 캐릭터가 매크로에 포함됐고 이 던전의 재화를 쓰도록 설정됐다."""
        opts = self.char_opts(slot)
        return bool(opts['include'] and opts[CURRENCY_KEY[route.currency]])

    def costs_for(self, slot):
        """이 카드로 돌 던전의 {재화: 입장 비용}. 비었으면 돌릴 것이 없다."""
        return {r.currency: r.cost for r in self.routes if self.uses(r, slot)}

    def affordable(self, route):
        return self.uses(route) and self.balance(route) >= route.cost

    def weight(self):
        # 장면이 바뀌는 중(던전 퇴장·대화 직전)에는 0/3.4e38(float 최댓값) 같은 값이 온다. 말이 되는 값까지 다시 읽는다.
        for _ in range(90):
            data = cli('get_inventory')
            try:
                current = float(data['CurrentInventoryWeight'])
                maximum = float(data['MaxInventoryWeight'])
                if 0 < maximum < 1e6 and 0 <= current < 1e6:
                    return current, maximum
            except (KeyError, TypeError, ValueError):
                pass
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
        return next((r for r in self.routes if r.currency != route.currency), route)

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
                # 대화(심층 뒤 몰리 등)나 알림 창(레벨업 다이스 등)이 끼어들었으면 그것부터 넘긴다.
                # 그동안에는 다른 조작이 먹히지 않는다.
                if not self.skip_dialogue(summarize_activity(cli('get_activity'))) and not self.close_notice():
                    retry()
                last = time.monotonic()
            self.wait(0.7)

    def in_dialogue(self):
        a = summarize_activity(cli('get_activity'))
        return isinstance(a, dict) and bool(a.get('dialogue') or a.get('selecting'))

    def clear_dialogue(self):
        """끼어든 대화가 끝날 때까지 넘긴다(넘기기는 until의 재시도 차례에 한다)."""
        self.until('대화 넘기기', lambda: not self.in_dialogue(), lambda: None, every=3, first=True)

    def close_notice(self, image=None):
        """Close a notice window (e.g. level-up dice reward) with the X in the middle; True if it did.

        알림이 화면을 어둡게 덮으므로 밝기를 펴서 밝은 획만 비교한다(녹화의 다른 화면은 0.41 이하).
        """
        spot = self.find_text('notice_close', image, region=NOTICE_CLOSE_REGION, threshold=0.6, normalize=True)
        if not spot:
            return False
        self.log('알림 창 닫기')
        self.game.click(*spot)
        self.wait(1)
        return True

    def skip_dialogue(self, a):
        """If the CLI says a dialogue or choice is up, get past it; True if it did.

        "대화 넘기기"가 있으면 누르고, 없이 선택지(초록 버튼)만 있으면 첫 번째를 고르고(몰리 질문은 매번 다르지만
        어느 답이든 대화만 이어진다), 둘 다 없으면 Space로 대화를 넘긴다.
        """
        if not isinstance(a, dict) or not (a.get('dialogue') or a.get('selecting')):
            return False
        image = self.game.capture()
        choices = self.choice_buttons(image)
        if self.skip_scene(image):
            self.log('대화 넘기기')
        elif choices:
            self.log(f'대화 선택지 고르기(첫 번째, {len(choices)}개 중)')
            self.game.click(*choices[0])
        else:
            self.game.key(VK_SPACE)
        self.wait(1)
        return True

    @staticmethod
    def choice_buttons(image):
        """대화 선택지 버튼(초록 둥근 버튼) 가운데 좌표, 왼쪽부터."""
        left, top, right, bottom = CHOICE_REGION
        a = np.asarray(image.convert('RGB').crop((left, top, right, bottom))).astype(int)
        r, g, b = a[..., 0], a[..., 1], a[..., 2]
        green = ((g > 150) & (g - r > 80) & (g - b > 20)).astype(np.uint8)
        _, _, stats, centers = cv2.connectedComponentsWithStats(green)
        spots = [(left + int(cx), top + int(cy)) for (cx, cy), (_, _, w, h, area) in zip(centers[1:], stats[1:])
                 if w > 40 and 18 < h < 40 and area > 0.6 * w * h]  # 가로로 길고 속이 찬 버튼만
        return sorted(spots)

    def hud(self, image=None):
        """True while the field HUD shows, i.e. no full-screen window (map, bag, menu) is open.

        녹화 화면 기준 필드는 셋 중 최고 0.88 이상, 지도·메뉴·가방·선택 화면은 0.45 이하.
        """
        image = image if image is not None else self.game.capture()
        return any(self.find_text(name, image, region=region, threshold=0.65, normalize=True)
                   for name, region in HUD_ANCHORS.items())

    def map_open(self, image=None):
        """지도가 열려 있다.

        이번 이동에서 M으로 지도를 열었고 필드 표시(ESC·Space·Ctrl+Z)가 모두 가려져 있으면 열린 것으로 본다.
        지도 왼쪽 위 "울라 대륙"도 보지만 보조로만 쓴다: 흰 글자라 지도 배경이 밝은 곳(눈밭 등)에서는
        어떤 방식으로도 알아보기 어렵다.
        """
        image = image if image is not None else self.game.capture()
        if self.map_opened and not self.hud(image) and not self.in_dialogue():
            return True  # 대화 장면도 필드 표시를 가리므로 대화 중이 아닐 때만
        return self.crumb(image)

    def crumb(self, image):
        """지도 왼쪽 위 "울라 대륙". 밝은 픽셀 비교와 주변보다 밝은 획 비교 중 하나라도 맞으면."""
        left, top, right, bottom = CRUMB_REGION
        frame = cv2.cvtColor(np.asarray(image.crop((left, top, right, bottom)).convert('RGB')), cv2.COLOR_RGB2GRAY)
        needle = cv2.cvtColor(np.asarray(Image.open(TEMPLATES / 'map_crumb.png').convert('RGB')), cv2.COLOR_RGB2GRAY)
        frame, needle = (frame > 200).astype(np.float32), (needle > 200).astype(np.float32)
        if frame.max() > 0 and cv2.minMaxLoc(cv2.matchTemplate(frame, needle, cv2.TM_CCOEFF_NORMED))[1] >= 0.7:
            return True
        return bool(self.find_text('map_crumb', image, region=CRUMB_REGION, normalize=True))

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
            currency = ROUTE_BY_KEY[self.start].currency  # 고른 던전 중 같은 재화를 쓰는 던전
            route = next(r for r in self.routes if r.currency == currency)
            self.log(f'지정한 {route.name}부터 진행')
            return route
        route = self.route_in(env) or next((r for r in self.routes if self.near_entrance(r, env)), None)
        if route:
            self.log(f'현재 위치 {env["space"]} → {route.name}부터 진행')
            return route
        route = next((r for r in self.routes if self.affordable(r)), self.routes[0])
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
            a = self.activity()
            if a['dungeon'] == 'NotInDungeon' and self.skip_dialogue(a):
                continue  # 던전에서 나온 뒤 끼어든 대화(심층 뒤 몰리 등)
            if a['dungeon'] == 'NotInDungeon' and not a['travel'] and self.close_notice():
                continue  # 레벨업 다이스 같은 알림 창
            state = a['dungeon']
            env = self.environment()
            route = self.route_in(env) or route
            if state != 'NotInDungeon':
                loading_since = None
            if state in ('Entering', 'InProgress'):
                self.wait_clear(route)
            elif state == 'Cleared':
                self.to_reward_screen()
                self.runs += 1
                self.fresh = False
                current, maximum = self.weight()
                self.log(f'{route.name} {self.runs}회 클리어 · 남은 {route.currency} {self.balance(route)}'
                         f' · 가방 {current:.0f}/{maximum:.0f}')
                self.remember()
                if self.slot is not None:
                    self.roster.mark(self.slot, 'played')  # 이 뒤로는 가방을 다시 정리해야 한다
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
            self.remember()  # 칭호가 바뀌었으면 기록도 지금 값으로
        else:
            self.log('지금 캐릭터가 몇 번 카드인지 기록이 없어 캐릭터 표 대신 모두 켠 것으로 돕니다(한 번 바꾸면 알아봅니다)')
        while True:
            try:
                self.run()
                return
            except Exhausted as reason:
                if self.fresh and self.recheck():
                    continue  # 접속 직후 덜 읽힌 재화였다. 이 캐릭터로 계속 돈다
                self.reload_config()  # 도는 중에 공방에서 바꾼 캐릭터 표·정리 설정을 여기서부터 적용
                if not self.char_opts()['include']:
                    reason = f'{self.slot + 1}번 캐릭터는 매크로에서 빼 두었습니다.'
                self.log(f'{reason}' + (' 다음 캐릭터로 바꿉니다.' if switch else ' 종료합니다.'))
                self.clean_bag()
                if not switch:
                    raise
            self.start = None
            self.switch_character()

    # ---- 캐릭터를 바꾸기 전 가방 정리 ----
    def clean_bag(self):
        """녹화(20261005-154846, 20261005-160805)한 순서 그대로 가방을 정리한다.

        자세히 가방 정리 → 아이템 탭 소모품: 등급 전체, "열기"로 전체 선택(패션 티켓 조각 보물 상자는 뺌) 정리
        → "분해"로 전체 선택 정리 → 장비 탭: 등급 전체, 무기·방어구·장신구 차례로 전체 선택 분해
        → 보석·룬: 고른 등급(기본 일반~에픽)만 전체 선택 분해 → X로 닫기.
        상자·소모품은 공방 설정 clean, 장비·룬 분해는 캐릭터 표의 카드별 설정을 따른다.
        """
        opts = self.char_opts()
        if not opts['include']:
            return  # 매크로에서 뺀 캐릭터의 가방은 건드리지 않는다
        items = self.clean and opts['items']
        equip = opts['equip']
        runes = opts['rune'] and list(self.config['rune_grades'])
        if not (items or equip or runes):
            return
        if self.slot is not None and not self.roster.needs_cleaning(self.slot):
            # 재시작했을 때 이미 정리한 캐릭터를 또 정리하지 않는다(정리한 뒤로 던전을 안 돌았다).
            self.log(f'{self.slot + 1}번 캐릭터는 마지막으로 던전을 돈 뒤 이미 가방을 정리했습니다'
                     f'({self.roster.cards[str(self.slot)]["cleaned_at"]}) · 건너뜀')
            return
        before, maximum = self.weight()
        self.log(f'가방 정리 시작 · 무게 {before:.0f}/{maximum:.0f}')
        self.open_bag_detail()
        if items:
            self.bag_tab('아이템')
            self.bag_subtab('sub_consumable', back=True)
            self.all_grades()
            self.bag_method('열기')
            if self.select_everything(skip_fashion=True):
                self.run_cleanup('상자 열기')
            self.bag_method('분해')
            if self.select_everything():
                self.run_cleanup('소모품 분해')
        if equip or runes:
            self.bag_tab('장비')
            self.all_grades()
            self.bag_method('분해')
        if equip:
            for name, label in (('sub_weapon', '무기'), ('sub_armor', '방어구'), ('sub_accessory', '장신구')):
                self.bag_subtab(name, back=name == 'sub_weapon')
                if self.select_everything():
                    self.run_cleanup(f'{label} 분해')
        if runes:
            self.bag_subtab('sub_rune')
            self.set_grades(runes)
            if self.select_everything():
                self.run_cleanup('룬 분해(' + '·'.join(runes) + ')')
        # 자세히 정리 → 가방 → 필드. 필드에서 그 자리를 누르면 다른 것이 눌리므로 필드가 아닐 때만 누른다.
        self.until('가방 닫기', self.hud, lambda: self.hud() or self.game.click(*DETAIL_CLOSE), every=2, first=True)
        after, _ = self.weight()
        self.log(f'가방 정리 완료 · 무게 {before:.0f} → {after:.0f}')
        if self.slot is not None:
            self.roster.mark(self.slot, 'cleaned')
        else:
            self.pending_clean = True  # 몇 번 카드인지 캐릭터 선택 화면에서 알아낸 뒤 기록한다

    def open_bag_detail(self):
        """I(가방) → A(간단히 정리하기) → 자세히 정리하기."""
        def step():
            image = self.game.capture()
            button = self.game.find('detail_button', image)
            if button:
                self.game.click(*button)
            elif self.hud(image):
                self.game.key(VK_I)
            else:
                self.game.key(VK_A)  # 가방이 열려 있다. 필드에서 A는 이동이라 필드가 아닐 때만 누른다
        self.until('자세히 가방 정리', lambda: self.game.find('detail_title'), step, every=2, first=True)
        self.wait(0.8)

    def bag_tab(self, name):
        """탭(장비·아이템 등)을 녹화처럼 W/S로 옮긴다. 선택된 탭은 초록 배경."""
        target = BAG_TABS.index(name)

        def current():
            image = self.game.capture()
            for i, x in enumerate(BAG_TAB_X):
                r, g, _ = ImageStat.Stat(image.crop((x - 10, 52, x + 10, 66))).mean
                if g - r > 30:
                    return i
            return None

        def step():
            now = current()
            if now is not None:
                self.game.key(VK_S if now < target else VK_W)
        self.until(f'{name} 탭', lambda: current() == target, step, every=1.5, first=True)
        self.wait(0.6)

    def bag_subtab(self, name, back=False):
        """칸(소모품·무기·방어구·장신구)이 흰색으로 선택될 때까지 Q(앞으로) 또는 E(뒤로).

        칸 막대는 선택한 칸에 따라 옆으로 밀려서 위치 대신 선택된 칸의 글자로 확인한다.
        """
        self.until(f'{name} 칸', lambda: self.game.find(name, region=SUBTAB_REGION),
                   lambda: self.game.key(VK_Q if back else VK_E), every=1.5, first=True)
        self.wait(0.6)

    def all_grades(self):
        """선택된 등급 칩을 모두 풀어 전 등급을 본다(녹화에서는 '일반'만 선택돼 있어 그것을 풀었다).

        게임이 칩 선택을 기억해서 지난번 룬 분해 때 고른 일반~에픽이 남아 있을 수 있다.
        """
        self.set_grades([])

    @staticmethod
    def grade_selected(image, name):
        left, cy, _ = GRADE_CHIPS[name]
        gray = np.asarray(image.convert('L')).astype(float)
        edge = gray[cy - 3:cy + 4, left - 4:left + 7].mean(axis=0).max()
        fill = gray[cy - 3:cy + 4, left + 10:left + 16].mean()
        return edge - fill > 40  # 선택 72 이상, 아니면 2 이하

    def set_grades(self, names):
        """등급 칩을 names만 선택된 상태로 맞춘다(빈 목록이면 전부 해제 = 전 등급)."""
        def wrong():
            image = self.game.capture()
            return [n for n in GRADE_CHIPS if self.grade_selected(image, n) != (n in names)]

        def fix():
            for name in wrong():
                _, cy, x = GRADE_CHIPS[name]
                self.game.click(x, cy)
                self.wait(0.3)
        self.until('등급 ' + ('·'.join(names) or '전체'), lambda: not wrong(), fix, every=1.5, first=True)
        self.wait(0.4)

    def bag_method(self, name):
        """정리 방법(분해·열기). 선택된 쪽이 밝다."""
        spot, box = BAG_METHODS[name]
        self.until(f'정리 방법 {name}', lambda: sum(ImageStat.Stat(self.game.capture().crop(box)).mean) / 3 > 150,
                   lambda: self.game.click(*spot), every=1.5, first=True)
        self.wait(0.6)

    def select_everything(self, skip_fashion=False):
        """전체 선택. 정리할 것이 있으면 True(목록이 비었거나 0개면 False)."""
        if self.game.find('list_empty', region=BAG_LIST_REGION):
            return False

        def checked():
            r, g, _ = ImageStat.Stat(self.game.capture().crop(SELECT_ALL_MARK)).mean
            return g > 150 and g - r > 80
        self.until('전체 선택', checked, lambda: self.game.click(*SELECT_ALL), every=1.5, first=True)
        self.wait(0.6)
        if skip_fashion:
            self.unselect_fashion_box()
        _, g, _ = ImageStat.Stat(self.game.capture().crop(RUN_BUTTON)).mean
        return g > 150

    def unselect_fashion_box(self):
        """패션 티켓 조각 보물 상자는 열지 않는다(녹화에서 뺐다).

        캐릭터마다 아이템 순서가 달라 이름 글자로 찾고, 아이콘 테두리가 초록(선택)이면 눌러 뺀다.
        """
        for _ in range(4):
            label = self.game.find('fashion_box', region=BAG_LIST_REGION)
            if label:
                break
            self.game.scroll(615, 330, -3)  # 목록이 길면 아래로 내려 찾는다
            self.wait(0.6)
        else:
            return
        icon = (label[0], label[1] - 34)

        def chosen():
            edge = (icon[0] - 21, icon[1] - 15, icon[0] - 18, icon[1] + 15)
            r, g, _ = ImageStat.Stat(self.game.capture().crop(edge)).mean
            return g > 100 and g - r > 60
        self.until('패션 티켓 조각 보물 상자 빼기', lambda: not chosen(), lambda: self.game.click(*icon),
                   every=1.5, first=True)
        self.log('패션 티켓 조각 보물 상자는 빼고 정리')

    def run_cleanup(self, what):
        """Space(정리하기) → Space(확인 창) → 결과 화면 → Space(확인·받기)."""
        def result():
            return self.game.find('result_disassemble') or self.game.find('result_open')
        self.until(f'{what} 결과', result, lambda: self.game.key(VK_SPACE), every=3, first=True)
        self.wait(1)
        self.until(f'{what} 결과 닫기', lambda: not result() and self.game.find('detail_title'),
                   lambda: result() and self.game.key(VK_SPACE), every=2, first=True)
        self.log(f'{what} 완료')
        self.wait(0.6)

    def recheck(self, tries=3):
        """Re-read currencies a few times; True if this character can still run a dungeon."""
        for attempt in range(tries):
            self.wait(10)
            money = currencies()
            if not isinstance(money, dict) or 'error' in money:
                continue
            if any(self.uses(route) and money.get(route.currency, 0) >= route.cost for route in self.routes):
                self.log(f'재화를 다시 읽으니 {money} · 이 캐릭터로 계속')
                self.remember(money)
                self.fresh = False
                return True
        return False

    def settled_currencies(self, quiet=6, least=10, timeout=40):
        """접속 직후 CLI 재화는 한동안 덜 읽힌 값이 온다(마법사 은동전 100·공물 4가 5·0으로 읽힌 적 있다).

        접속 후 least초가 지나고 값이 quiet초 동안 그대로일 때 돌려준다.
        """
        started = time.monotonic()
        last, since, money = None, None, {}
        while True:
            reading = currencies()
            now = time.monotonic()
            if isinstance(reading, dict) and 'error' not in reading:
                money = reading
                key = tuple(reading.get(route.currency, 0) for route in ROUTES)
                if key != last:
                    last, since = key, now
                elif now - since >= quiet and now - started >= least:
                    return money
            if now - started > timeout:
                return money
            self.wait(2)

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
        """ESC 메뉴 → 게임 종료 → 캐릭터 선택 화면으로.

        메뉴와 창은 보이는 즉시 누른다. 같은 버튼은 1.5초, ESC는 4초 간격으로만 다시 누른다(열리는 중에
        또 누르면 닫히거나 두 번 눌린다). 로딩 중(검은 화면)에는 아무것도 누르지 않는다.
        """
        deadline = time.monotonic() + PATIENCE
        pressed = {}

        def due(what, gap):
            if time.monotonic() - pressed.get(what, -99) < gap:
                return False
            pressed[what] = time.monotonic()
            return True

        while True:
            self.check()
            image = self.game.capture()
            if self.game.find('select_title', image):
                break
            if time.monotonic() > deadline:
                raise Stop(f'캐릭터 선택 화면: {PATIENCE}초 동안 확인되지 않았습니다.')
            button = self.find_text('menu_quit', image, region=MENU_QUIT_REGION)
            if self.game.find('quit_title', image):
                if due('select', 1.5):
                    self.game.click(*TO_SELECT)
            elif button:
                if due('quit', 1.5):
                    self.game.click(*button)
            elif self.hud(image):
                if due('esc', 4) and not self.close_notice(image):
                    self.game.key(VK_ESC)
            elif due('dialogue', 4):
                self.skip_dialogue(summarize_activity(cli('get_activity')))
            self.wait(0.4)
        self.wait(1.5)  # 카드가 다 그려질 때까지

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
        if self.pending_clean and self.slot is not None:
            self.roster.mark(self.slot, 'cleaned')
        self.pending_clean = False
        # 캐릭터 표에서 뺐거나 은동전·공물을 둘 다 끈 카드는 고르지 않는다.
        candidates = [c for c in cards if c['lv100'] and c['slot'] not in self.skip_slots
                      and self.costs_for(c['slot'])]
        ready = {c['slot']: self.roster.ready_at(c['slot'], self.costs_for(c['slot'])) for c in candidates}
        self.log('캐릭터 선택 · ' + (' / '.join(self.roster.describe(c['slot'], self.costs_for(c['slot']))
                                               for c in candidates) or '돌릴 캐릭터 없음'))
        while True:
            todo = [c for c in candidates if ready[c['slot']] <= self.roster.now()]
            if todo:
                break
            soonest = min(candidates, key=lambda c: ready[c['slot']], default=None)
            if soonest is None or ready[soonest['slot']] == float('inf'):
                raise Stop('매크로에 포함한 100레벨 캐릭터가 없습니다. 종료합니다.')
            when = f'가장 빠른 것은 {soonest["slot"] + 1}번 캐릭터, {clock(ready[soonest["slot"]])}쯤부터입니다.'
            if not self.config['wait']:
                raise Stop(f'모든 100레벨 캐릭터의 은동전·마족 공물이 부족합니다. {when} 종료합니다.')
            self.log(f'모든 캐릭터의 재화가 부족합니다. {when} 그때까지 기다렸다 이어서 돕니다.')
            # 충전 간격을 기록 시각부터 세므로 1분 여유를 둔다. 기다리는 동안에도 중지할 수 있다.
            while self.roster.now() < ready[soonest['slot']] + 60:
                self.wait(min(60, ready[soonest['slot']] + 60 - self.roster.now()))
            self.until('캐릭터 선택 화면', lambda: self.game.find('select_title'), lambda: None, every=60)
        card = todo[0]
        self.until(f'{card["slot"] + 1}번 캐릭터 카드 선택',
                   lambda: self.cards(self.game.capture())[card['slot']]['selected'],
                   lambda: self.game.click(*card['center']), every=2, first=True)
        self.log(f'{card["slot"] + 1}번 카드 선택 · 화면 {self.screenshot("select")}')
        # 접속하면 CLI가 응답한다. 선택 화면이 그대로면 게임 시작을 다시 누른다.
        self.until('캐릭터 접속', self.in_game,
                   lambda: self.game.find('select_title') and self.game.click(*GAME_START), every=8, first=True)
        now = self.me()
        money = self.settled_currencies()
        self.log(f'{card["slot"] + 1}번 캐릭터 접속 · {now["realm"]} {now["job"]} {now["level"]}레벨 · '
                 f'재화 {money}')
        if now['id'] in self.done_ids:
            # 고른 카드와 다른 캐릭터다. 기록하지 않고 선택 화면에서 다시 고른다.
            self.log(f'이미 끝낸 캐릭터로 접속됨({now["realm"]} {now["job"]}) · 다음 캐릭터로 넘어갑니다.')
            return self.switch_character()
        self.slot, self.ident = card['slot'], now['id']
        self.remember(money)
        self.fresh = True
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
                if self.close_notice():
                    continue  # 알림 창이 자동 진행을 막고 있었다
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

    @staticmethod
    def text_mask(image, normalize=False):
        """주변보다 확실히 밝은 픽셀(글자)만 1로. 반투명 버튼 뒤 배경이 달라도 글자 모양은 같다.

        normalize: 영역의 밝기를 먼저 0~255로 편다. 알림 창이 화면을 어둡게 덮어도 글자를 찾는다.
        """
        gray = cv2.cvtColor(np.asarray(image.convert('RGB')), cv2.COLOR_RGB2GRAY).astype(np.float32)
        if normalize:
            low, high = np.percentile(gray, 1), np.percentile(gray, 99.5)
            gray = np.clip((gray - low) / max(high - low, 1) * 255, 0, 255)
        gray = gray.astype(np.int16)
        return ((gray - cv2.blur(gray, (15, 15))) > 35).astype(np.float32)

    def find_text(self, name, image=None, region=None, threshold=0.7, normalize=False):
        """Like Game.find but compares only the bright strokes (text, icons) of the template.

        반투명 창(ESC 메뉴 등)은 뒤 게임 화면이 비쳐 일반 비교로는 일치도가 들쭉날쭉하다.
        """
        if not hasattr(self, 'text_needles'):
            self.text_needles = {}
        if (name, normalize) not in self.text_needles:
            self.text_needles[name, normalize] = self.text_mask(Image.open(TEMPLATES / f'{name}.png'), normalize)
        needle = self.text_needles[name, normalize]
        image = image if image is not None else self.game.capture()
        left, top, right, bottom = region or (0, 0, *image.size)
        frame = self.text_mask(image.crop((left, top, right, bottom)), normalize)
        if frame.max() == 0 or frame.shape[0] < needle.shape[0] or frame.shape[1] < needle.shape[1]:
            return None
        _, score, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(frame, needle, cv2.TM_CCOEFF_NORMED))
        if score < threshold:
            return None
        return left + x + needle.shape[1] // 2, top + y + needle.shape[0] // 2

    def skip_scene(self, image=None):
        """Press any "… 넘기기" button (장면·대화·이야기) at the top right.

        버튼마다 앞 글자가 달라서 공통인 "넘기기" 글자 모양만 비교한다.
        """
        if not hasattr(self, 'skip_masks'):
            # 밝은·어두운 배경의 "장면 넘기기" 버튼에서 "넘기기" 부분만 쓴다.
            self.skip_masks = [self.text_mask(Image.open(TEMPLATES / f'{n}.png'))[4:22, 40:78]
                               for n in ('skip_scene', 'skip_scene_dark')]
        image = image if image is not None else self.game.capture()
        left, top, right, bottom = SKIP_REGION
        frame = self.text_mask(image.crop((left, top, right, bottom)))
        if frame.max() == 0:
            return False
        for needle in self.skip_masks:
            _, score, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(frame, needle, cv2.TM_CCOEFF_NORMED))
            if score >= 0.75:
                self.game.click(left + x + needle.shape[1] // 2, top + y + needle.shape[0] // 2)
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

        button = self.until('입장 화면', lambda: self.game.find(route.enter_template), nudge, every=2)
        if route.double_cost:
            # 더블 루팅: off 끔, always 비용만 되면, threshold 공방에서 정한 개수 이상일 때(기본 20)
            mode, balance = self.config['double'], self.balance(route)
            need = route.double_cost if mode == 'always' else max(route.double_cost, int(self.config['double_min']))
            want = mode != 'off' and balance >= need
            have = bool(self.game.find('double_on', region=DOUBLE_REGION))
            if want != have:
                self.log('더블 루팅 ' + ('켜기' if want else '끄기' if mode == 'off'
                                      else f'끄기 ({route.currency} {need}개 미만)'))
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
        self.map_opened = False
        # 알림 창에 막혀 지도가 닫히거나 클릭이 빠지면 지도 열기부터 다시 고른다.
        go = self.until('여기로 가기', lambda: self.game.find('map_go'),
                        lambda: self.pick_on_map(route, space), every=8, first=True)
        self.game.click(*go)
        if space != route.field_space and self.use_wings:
            self.wait(1.5)
            self.game.key(VK_T)
        return self.wait_arrival(route)

    def open_map(self):
        """M으로 지도를 연다. 지도가 열리면 필드 표시(ESC·Space·Ctrl+Z)가 모두 가려진다.

        알림 창에 M이 먹히지 않으면 필드 표시가 그대로 보이므로 다시 누른다.
        이미 연 지도는 M을 또 눌러 닫지 않는다.
        """
        self.clear_dialogue()  # 대화 중에는 M이 먹히지 않고, 대화 화면이 필드 표시를 가려 지도로 오인한다
        if self.map_open():
            return
        if not self.hud() and not getattr(self, 'hud_reported', False):
            # 필드인데 필드 표시를 못 알아보면 M이 먹혔는지 판단할 수 없다. 원인을 볼 수 있게 남긴다.
            self.hud_reported = True
            self.log(f'필드 표시(ESC·Space·Ctrl+Z)를 확인하지 못함 · 화면 {self.screenshot("hud")}')
        self.game.key(VK_M)
        self.wait(1.5)
        self.until('지도 열기', lambda: not self.hud() and not self.in_dialogue(),
                   lambda: self.hud() and self.game.key(VK_M), every=5)
        self.map_opened = True
        self.wait(1)
        if not self.crumb(self.game.capture()) and not getattr(self, 'crumb_reported', False):
            self.crumb_reported = True  # 진행에는 지장 없다. 템플릿을 실제 화면으로 고칠 수 있게 남긴다
            self.log(f'지도 왼쪽 위 "울라 대륙"은 못 알아봄(필드 표시가 가려져 지도로 보고 진행) · '
                     f'화면 {self.screenshot("map")}')

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
            if self.skip_dialogue(a):
                idle_since = None
                continue
            if a['travel'] or a['auto']:
                idle_since = None
            else:
                idle_since = idle_since or time.monotonic()
                if a['interaction'] == 'EnterDungeon' or time.monotonic() - idle_since > 1.5:
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


def parse(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=int, help='이 횟수만큼 클리어하면 종료')
    parser.add_argument('--start', choices=[r.key for r in ROUTES],
                        help='던전 밖에서 시작할 때 이 던전부터 진행 (runda/peka)')
    parser.add_argument('--check', action='store_true', help='조작 없이 현재 위치·재화·화면 인식 결과만 출력')
    parser.add_argument('--no-wings', action='store_true', help='던전 간 이동에 정령의 날개(T)를 쓰지 않음')
    parser.add_argument('--no-switch', action='store_true', help='재화가 떨어져도 다른 캐릭터로 바꾸지 않고 종료')
    parser.add_argument('--no-clean', action='store_true', help='재화를 다 쓴 뒤 가방 정리(장비 분해 등)를 하지 않음')
    parser.add_argument('--no-pause', action='store_true', help='끝나도 엔터를 기다리지 않음(에린 공방 던전 탭에서 실행)')
    return parser.parse_args(argv)


def open_log(echo=print):
    """dungeon/logs/dungeon-날짜.log에 쓰고 echo로도 보내는 log 함수와 닫기 함수."""
    LOGS.mkdir(parents=True, exist_ok=True)
    logfile = (LOGS / f'dungeon-{datetime.now():%Y%m%d}.log').open('a', encoding='utf-8')

    def log(message):
        line = f'[{datetime.now():%H:%M:%S}] {message}'
        echo(line)
        logfile.write(line + '\n')
        logfile.flush()
    return log, logfile.close


def watch_stop(stop):
    """F12, 또는 STOP_FILE(관리자 권한으로 따로 띄웠을 때 에린 공방 중지 버튼)이 생기면 stop을 켠다."""
    listener = keyboard.Listener(on_press=lambda k: stop.set() if k == keyboard.Key.f12 else None, daemon=True)
    listener.start()
    STOP_FILE.unlink(missing_ok=True)

    def watch_file():
        while not stop.wait(0.5):
            if STOP_FILE.exists():
                STOP_FILE.unlink(missing_ok=True)
                stop.set()
    threading.Thread(target=watch_file, daemon=True).start()
    return listener


def session(args, stop, log):
    """한 번 실행(--check면 확인만). 끝난 이유는 log로 남기고 예외는 밖으로 내보내지 않는다."""
    try:
        hwnd = find_game()
        if not hwnd:
            raise Stop('마비노기 모바일 창을 찾지 못했습니다.')
        log('던전 매크로 시작 · F12 중지')
        config = load_config()
        macro = Macro(Game(hwnd), stop, log, max_runs=args.runs, use_wings=not args.no_wings,
                      start=args.start, clean=not args.no_clean, config=config)
        macro.live = True
        log('설정 · 은동전 ' + macro.routes[0].name + ' · 마족 공물 ' + macro.routes[1].name
            + ' · 더블 루팅 ' + {'off': '끔', 'always': '항상'}.get(config['double'], f'{config["double_min"]}개 이상')
            + ' · 룬 분해 ' + ('·'.join(config['rune_grades']) or '안 함')
            + (' · 충전 대기' if config['wait'] else ''))
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
    except Exception as error:  # noqa: BLE001 - 게임 창을 찾기 전 오류도 남긴다
        log(f'종료: 예상치 못한 오류: {error!r}')


def main():
    args = parse()
    if sys.stdout:  # 콘솔 없이(pythonw) 띄우면 stdout이 없다. 로그는 파일로 본다
        sys.stdout.reconfigure(encoding='utf-8')
    if not is_admin():
        if not relaunch_as_admin(__file__):
            sys.exit('관리자 권한 실행이 취소되었습니다. 게임이 관리자 권한이라 매크로도 관리자 권한이 필요합니다.')
        return
    log, close = open_log(lambda line: print(line, flush=True))
    stop = threading.Event()
    watch_stop(stop)
    try:
        session(args, stop, log)
    finally:
        close()
        if not args.no_pause and sys.stdin:
            input('엔터를 누르면 창을 닫습니다.')


if __name__ == '__main__':
    main()
