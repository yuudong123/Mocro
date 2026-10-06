"""캐릭터 카드별로 마지막에 본 은동전·마족 공물을 기억한다(dungeon/characters.json).

매크로를 다시 켜도 재화가 없는 캐릭터에 일일이 접속해 보지 않도록, 마지막으로 본 양과
그 뒤 충전량으로 지금 던전을 돌 수 있는지 추정한다.
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

# exe로 실행하면 exe 옆 data 폴더에 쓴다(common.DATA와 같다. 이 모듈은 공방에서도 읽어서 common을 쓰지 않는다).
FROZEN = getattr(sys, 'frozen', False)
PATH = ((Path(sys.executable).parent / 'data' / 'dungeon') if FROZEN
        else Path(__file__).resolve().parent) / 'characters.json'
# 재화별 (1개 충전 간격(초), 자연 충전 최대치). 게임 화면에 은동전 n/100, 마족 공물 n/10으로 나온다.
REGEN = {'은동전': (30 * 60, 100), '마족 공물': (12 * 3600, 10)}


def clock(epoch):
    return datetime.fromtimestamp(epoch).strftime('%m-%d %H:%M')


class Roster:
    def __init__(self, path=PATH):
        self.path = path
        try:
            self.cards = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self.cards = {}

    @staticmethod
    def now():
        return time.time()

    def record(self, slot, ident, money):
        """Save what card `slot` (0-based) holds now; `ident` tells characters apart."""
        now = self.now()
        # 같은 캐릭터가 다른 칸에 남아 있으면(카드 순서가 바뀐 경우) 지운다.
        self.cards = {k: v for k, v in self.cards.items() if v.get('id') != list(ident) or k == str(slot)}
        self.cards[str(slot)] = {'id': list(ident), 'seen': now, 'seen_at': clock(now),
                                 **{name: int(money.get(name, 0)) for name in REGEN}}
        self.path.write_text(json.dumps(self.cards, ensure_ascii=False, indent=1), encoding='utf-8')

    def slot_of(self, ident):
        return next((int(k) for k, v in self.cards.items() if v.get('id') == list(ident)), None)

    def estimate(self, slot):
        """Currencies now assuming steady regen since last seen; None if the card was never seen."""
        card = self.cards.get(str(slot))
        if not card:
            return None
        elapsed = self.now() - card['seen']
        return {name: card[name] if card[name] >= cap else min(cap, card[name] + int(elapsed // every))
                for name, (every, cap) in REGEN.items()}

    def ready_at(self, slot, costs):
        """Epoch time from which the card can pay any of `costs` ({currency: cost}); 0 if never seen."""
        card = self.cards.get(str(slot))
        if not card:
            return 0
        times = []
        for name, cost in costs.items():
            every, cap = REGEN[name]
            if card[name] >= cost:
                return card['seen']
            if cost <= cap:
                times.append(card['seen'] + (cost - card[name]) * every)
        return min(times, default=float('inf'))

    def describe(self, slot, costs):
        card = self.cards.get(str(slot))
        if not card:
            return f'{slot + 1}번 기록 없음'
        guess = self.estimate(slot)
        ready = self.ready_at(slot, costs)
        when = '지금 가능' if ready <= self.now() else f'{clock(ready)}부터'
        return (f'{slot + 1}번 ' + '·'.join(f'{name} {guess[name]}' for name in REGEN)
                + f' (추정, {card["seen_at"]} 기록) {when}')
