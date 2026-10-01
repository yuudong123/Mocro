"""부캐 육성: 스토리 퀘스트 자동 진행이 멈추지 않게 이어 주며 목표 레벨까지 둔다.

대화·선택지는 Space, 연출은 장면 넘기기, "화면을 터치해 주세요"는 클릭으로 넘긴다.
아무것도 진행되지 않으면 Space(나침반)로 퀘스트 자동 진행을 다시 켠다.
화면 캡처는 게임을 잠깐 멈추게 해 자동 이동을 끊을 수 있어서, 진행이 멈췄을 때만 화면을 본다.
"""
import time

from macro import CLEAR_TOUCH, VK_SPACE, Stop

IDLE_SPACE = 8       # 이만큼 아무 진행이 없으면 Space를 누른다
STUCK_REPORT = 600   # 이만큼 진행이 없으면 화면을 남긴다
LEVEL_EVERY = 60
BAG_EVERY = 300


class Story:
    def __init__(self, macro, target=98):
        self.m, self.target = macro, target

    def run(self):
        m = self.m
        level, last_level = None, 0
        last_bag = time.monotonic()
        idle_since, last_space, reported = None, 0, False
        m.log(f'스토리 진행 시작 · {self.target}레벨이 되면 멈춤 · F12 중지')
        while True:
            m.check()
            now = time.monotonic()
            if now - last_level > LEVEL_EVERY:
                me = m.me()
                if me['level'] != level:
                    level = me['level']
                    m.log(f'{me["job"]} {level}레벨')
                if level >= self.target:
                    raise Stop(f'{self.target}레벨 도달 · 완료')
                last_level = now
            a = m.activity()
            if a['dialogue'] or a['selecting']:
                # 대화 넘기기와 선택지(첫 번째·기본 선택)는 Space로 된다.
                if a['dialogue_next'] or a['selecting']:
                    m.game.key(VK_SPACE)
                idle_since, reported = None, False
                m.wait(0.8)
                continue
            busy = a['auto'] or a['travel'] or a['combat'] or a['dead'] or a['reviving']
            if busy and not a['sequence']:
                idle_since, reported = None, False
                if now - last_bag > BAG_EVERY and not a['combat']:
                    m.make_room()
                    last_bag = now
                m.wait(2)
                continue
            image = m.game.capture()
            if m.skip_scene(image):
                m.log('연출 장면 넘기기')
                idle_since = None
            elif a['dungeon'] == 'Cleared' or m.game.find('touch_screen', image):
                m.game.click(*CLEAR_TOUCH)
                idle_since = None
            elif not a['sequence']:
                idle_since = idle_since or now
                if now - idle_since > IDLE_SPACE and now - last_space > IDLE_SPACE:
                    # 상호작용(대화·조사)이 있으면 그것을, 없으면 나침반으로 퀘스트 자동 진행을 켠다.
                    m.game.key(VK_SPACE)
                    last_space = now
                if now - idle_since > STUCK_REPORT and not reported:
                    m.log(f'{STUCK_REPORT // 60}분 동안 진행이 없음 · 화면 {m.screenshot("story")} · 계속 시도')
                    reported = True
            m.wait(2)
