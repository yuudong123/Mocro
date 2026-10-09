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
        # CLI의 서버/직업/칭호는 고유 ID가 아니다. 같은 값을 가진 다른
        # 카드의 기록을 지우지 않는다(순회와 설정의 기준은 카드 번호).
        old = self.cards.get(str(slot), {})
        # 같은 캐릭터(서버·직업)면 마지막으로 던전을 돈·가방을 정리한 시각을 이어 간다.
        times = {k: old[k] for k in ('played', 'played_at', 'cleaned', 'cleaned_at')
                 if k in old and (old.get('id') or [])[:2] == list(ident)[:2]}
        self.cards[str(slot)] = {'id': list(ident), 'seen': now, 'seen_at': clock(now),
                                 **{name: int(money.get(name, 0)) for name in REGEN}, **times}
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.cards, ensure_ascii=False, indent=1), encoding='utf-8')

    def mark(self, slot, what):
        """카드에 지금 시각을 남긴다. what: 'played'(던전을 돌았다) 또는 'cleaned'(가방을 정리했다)."""
        card = self.cards.get(str(slot))
        if card is None:
            return
        now = self.now()
        card[what], card[f'{what}_at'] = now, clock(now)
        self.save()

    def needs_cleaning(self, slot):
        """마지막으로 가방을 정리한 뒤 던전을 돌았으면(또는 정리 기록이 없으면) True."""
        card = self.cards.get(str(slot))
        if not card or 'cleaned' not in card:
            return True
        return card.get('played', 0) > card['cleaned']

    def slot_of(self, ident):
        """서버·직업·칭호(ident)로 카드를 찾는다.

        칭호는 게임에서 바꿀 수 있어서, 똑같은 기록이 없으면 서버·직업이 같은 카드가 하나뿐일 때 그 카드로 본다.
        """
        ident = list(ident)
        exact = [int(k) for k, v in self.cards.items() if v.get('id') == ident]
        if exact:
            return exact[0] if len(exact) == 1 else None
        same = [int(k) for k, v in self.cards.items() if (v.get('id') or [])[:2] == ident[:2]]
        return same[0] if len(same) == 1 else None

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
