import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtWidgets import QApplication

import dungeon_tab
from dungeon_tab import DungeonPanel, LogTail


class DungeonTab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.paths = {'VENV_PYTHON': root / 'venv' / 'python.exe', 'STAMP': root / 'venv' / 'stamp',
                      'REQUIREMENTS': root / 'requirements.txt', 'LOGS': root / 'logs',
                      'ROSTER': Path(dungeon_tab.ROSTER), 'STOP_FILE': root / 'stop.request'}
        self.patches = [patch.object(dungeon_tab, k, v) for k, v in self.paths.items()]
        for p in self.patches:
            p.start()
        self.paths['REQUIREMENTS'].write_text('numpy==1.26.4\n', encoding='utf-8')

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.temp.cleanup()

    def test_installed_needs_venv_and_same_requirements(self):
        self.assertFalse(dungeon_tab.installed())
        self.paths['VENV_PYTHON'].parent.mkdir()
        self.paths['VENV_PYTHON'].write_text('')
        self.paths['STAMP'].write_text('numpy==1.26.4\n', encoding='utf-8')
        self.assertTrue(dungeon_tab.installed())
        self.paths['REQUIREMENTS'].write_text('numpy==1.26.4\nmss==10.0.0\n', encoding='utf-8')
        self.assertFalse(dungeon_tab.installed())  # requirements가 바뀌면 다시 설치

    def test_options_become_macro_arguments(self):
        panel = DungeonPanel({'switch': False, 'clean': True, 'wings': False, 'start': 2, 'runs': 3})
        self.assertEqual(panel.arguments(), ['--no-switch', '--no-wings', '--start', 'peka', '--runs', '3'])
        self.assertEqual(panel.arguments(check=True), ['--check'])
        self.assertEqual(DungeonPanel({}).arguments(), [])
        state = panel.state()
        self.assertEqual({k: state[k] for k in ('switch', 'clean', 'wings', 'start', 'runs')},
                         {'switch': False, 'clean': True, 'wings': False, 'start': 2, 'runs': 3})

    def test_defaults_match_macro(self):
        state = DungeonPanel({}).state()
        self.assertEqual(state['silver_route'], 'runda')
        self.assertEqual(state['tribute_route'], 'peka')
        self.assertEqual((state['double'], state['double_min']), ('threshold', 20))
        self.assertEqual(state['rune_grades'], ['일반', '고급', '레어', '엘리트', '에픽'])
        self.assertFalse(state['wait'])
        self.assertEqual(len(state['characters']), 6)
        self.assertTrue(all(all(opts.values()) for opts in state['characters'].values()))

    def test_character_table_round_trip(self):
        saved = {'characters': {'2': {'include': False}, '3': {'silver': False, 'rune': False}},
                 'rune_grades': ['일반', '전설'], 'double': 'off', 'wait': True}
        state = DungeonPanel(saved).state()
        self.assertFalse(state['characters']['2']['include'])
        self.assertTrue(state['characters']['2']['silver'])
        self.assertEqual(state['characters']['3'],
                         {'include': True, 'silver': False, 'tribute': True, 'items': True, 'equip': True,
                          'rune': False})
        self.assertEqual(state['rune_grades'], ['일반', '전설'])
        self.assertEqual(state['double'], 'off')
        self.assertTrue(state['wait'])

    def test_old_global_clean_off_becomes_per_character(self):
        state = DungeonPanel({'clean': False, 'characters': {'1': {'items': True}}}).state()
        self.assertFalse(state['characters']['0']['items'])
        self.assertTrue(state['characters']['1']['items'])

    def test_progress_counts_log_lines(self):
        panel = DungeonPanel()
        for line in ['[10:00:00] 3번 캐릭터 접속 · 알리사 전격술사 100레벨 · 재화 {}',
                     '[10:01:00] 룬다 일반 1-1 입장 · 은동전 20개 사용',
                     '[10:02:00] 룬다 일반 1-1 1회 클리어 · 남은 은동전 80 · 가방 300/470',
                     '[10:03:00] 페카 고분 심층 2-1 입장 · 마족 공물 1개 사용',
                     '[10:04:00] 페카 고분 심층 2-1 2회 클리어 · 남은 마족 공물 0 · 가방 310/470',
                     '[10:05:00] 종료: 모든 100레벨 캐릭터의 은동전·마족 공물이 부족합니다.']:
            panel.message(line)
        p = panel.progress
        self.assertEqual((p.character, p.clears, p.silver, p.tribute), ('3번 전격술사', 2, 20, 1))
        self.assertIn('부족', p.reason)

    def test_checks_without_game(self):
        with patch.object(dungeon_tab, 'game_window', return_value=None):
            results = dict((name, ok) for name, ok, _ in dungeon_tab.run_checks(self.temp.name + '/없음.exe'))
        self.assertFalse(results['게임 창'])
        self.assertFalse(results['게임 CLI'])
        self.assertFalse(results['게임 접속'])

    def test_first_open_installs_once(self):
        panel = DungeonPanel()
        with patch.object(dungeon_tab, 'install') as install:
            panel.activate()
            panel.activate()  # 설치 중에 다시 열어도 한 번만
            for _ in range(50):
                panel.poll()
                if not panel.installing:
                    break
                self.app.processEvents()
                import time
                time.sleep(0.02)
        install.assert_called_once()
        self.assertFalse(panel.installing)

    def test_log_tail_reads_only_new_complete_lines(self):
        logs = self.paths['LOGS']
        logs.mkdir()
        tail_path = logs / f'dungeon-{dungeon_tab.datetime.now():%Y%m%d}.log'
        tail_path.write_text('[10:00:00] 이전 실행\n', encoding='utf-8')
        tail = LogTail()
        with tail_path.open('a', encoding='utf-8') as f:
            f.write('[10:01:00] 던전 매크로 시작\n[10:01:01] 쓰는 중')
        self.assertEqual(tail.read(), ['[10:01:00] 던전 매크로 시작'])
        with tail_path.open('a', encoding='utf-8') as f:
            f.write('\n')
        self.assertEqual(tail.read(), ['[10:01:01] 쓰는 중'])

    def test_stop_writes_request_file(self):
        panel = DungeonPanel()
        panel.process = object()
        panel.request_stop()
        self.assertTrue(self.paths['STOP_FILE'].exists())
        panel.process = None


class WindowHasDungeonTab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dungeon_tab_and_saved_options(self):
        from desktop import Window
        with tempfile.TemporaryDirectory() as temp:
            settings = Path(temp) / 'settings.json'
            settings.write_text(json.dumps({'dungeon': {'switch': False, 'runs': 5}}), encoding='utf-8')
            with patch('desktop.SETTINGS', settings), patch.object(Window, 'refresh'):
                window = Window()
                window.timer.stop()
                self.assertEqual([window.modes.tabText(i) for i in range(window.modes.count())], ['생활', '던전'])
                self.assertFalse(window.dungeon.switch.isChecked())
                self.assertEqual(window.dungeon.runs.value(), 5)
                window.dungeon.runs.setValue(7)
                self.assertTrue(window.save())
                window.close()
            self.assertEqual(json.loads(settings.read_text(encoding='utf-8'))['dungeon']['runs'], 7)


if __name__ == '__main__':
    unittest.main()
