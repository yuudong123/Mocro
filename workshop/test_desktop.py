import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
import threading
import tempfile
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from desktop import Window


class Presets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings_path = Path(self.temp.name) / 'settings.json'
        self.settings_patch = patch('desktop.SETTINGS', self.settings_path)
        self.settings_patch.start()
        self.patcher = patch.object(Window, 'refresh')
        self.patcher.start()
        self.window = Window()
        self.window.timer.stop()
        self.window.targets = {}
        self.window.presets = {}
        self.window.target_list.clear()

    def tearDown(self):
        self.window.running = False
        self.window.close()
        self.patcher.stop()
        self.settings_patch.stop()
        self.temp.cleanup()

    def test_save_and_one_click_start(self):
        w = self.window
        w.add_target('스노우 오브', 1)
        w.budget.setValue(55)
        w.recovery_mode.setCurrentIndex(1)
        w.include_inventory.setChecked(False)
        w.defaults = {'철괴': ['alter', '철괴(철 광석)']}
        w.preset_name.setText('오브')
        with patch.object(w, 'save', return_value=True):
            w.save_preset()
        w.add_target('목재', 300)
        w.budget.setValue(10)
        w.defaults = {}
        w.include_inventory.setChecked(True)
        with patch.object(w, 'launch') as launch:
            w.run_preset('오브')
            launch.assert_called_once()
        self.assertEqual(w.targets, {'스노우 오브': 1})
        self.assertEqual(w.budget.value(), 55)
        self.assertEqual(w.recovery_mode.currentIndex(), 1)
        self.assertFalse(w.include_inventory.isChecked())
        self.assertEqual(w.defaults['철괴'][1], '철괴(철 광석)')

    def test_running_rejects_preset(self):
        w = self.window
        w.running = True
        with patch.object(w, 'launch') as launch:
            w.run_preset('없음')
            launch.assert_not_called()

    def test_empty_not_saved(self):
        w = self.window
        with patch.object(w, 'save') as save:
            w.save_preset()
            save.assert_not_called()

    def test_dungeon_blocks_lifestyle_actions(self):
        self.window.targets = {'철괴': 1}
        self.window.dungeon.process = 'inside'
        try:
            self.window.launch()
            self.assertFalse(self.window.running)
            self.assertIn('던전 매크로를 중지', self.window.status.text())
        finally:
            self.window.dungeon.process = None

    def test_resume_keeps_frozen_goal_and_request_count(self):
        w = self.window
        w.add_target('강철괴', 40)
        w.include_inventory.setChecked(False)
        w.pending = {'targets': {'강철괴': 40}, 'include_inventory': False,
                     'resolved_targets': {'강철괴': 140}, 'calls': 3}
        with patch.object(w, 'save', return_value=True), patch('desktop.threading.Thread'):
            w.launch()
        self.assertEqual(w.pending['resolved_targets'], {'강철괴': 140})
        self.assertEqual(w.engine.calls, 3)
        self.assertFalse(w.include_inventory.isEnabled())

    def test_changed_mode_starts_new_baseline(self):
        w = self.window
        w.add_target('강철괴', 40)
        w.include_inventory.setChecked(False)
        w.pending = {'targets': {'강철괴': 40}, 'include_inventory': True,
                     'resolved_targets': {'강철괴': 40}, 'calls': 3}
        with patch.object(w, 'save', return_value=True), patch('desktop.threading.Thread'):
            w.launch()
        self.assertNotIn('resolved_targets', w.pending)
        self.assertEqual(w.engine.calls, 0)

    def test_baseline_acknowledged_only_after_save(self):
        w = self.window
        w.pending = {'targets': {'강철괴': 40}, 'include_inventory': False}
        ack, result = threading.Event(), {}
        w.events.put(('prepared', ({'강철괴': 140}, ack, result)))
        with patch.object(w, 'save', return_value=False):
            w.poll()
        self.assertTrue(ack.is_set())
        self.assertFalse(result['saved'])
        self.assertNotIn('resolved_targets', w.pending)

    def load_catalog(self, window):
        rows = {'철괴(광석)': [('alter', '철괴(광석)')],
                '철괴(철 광석)': [('alter', '철괴(철 광석)')],
                '실크(거미줄)': [('alter', '실크(거미줄)')],
                '실크(고급)': [('alter', '실크(고급)')]}
        window.events.put(('catalog', SimpleNamespace(gather={}, recipes=rows)))
        window.poll()

    def test_all_inputs_survive_restart_without_starting(self):
        w = self.window
        self.load_catalog(w)
        w.budget.setValue(755)
        w.include_inventory.setChecked(False)
        w.recovery_mode.setCurrentIndex(1)
        w.tabs.setCurrentIndex(1)
        w.facility.setCurrentText('금속 가공 시설')
        w.search.setText('철괴')
        w.text.setText('목재 88')
        w.preset_name.setText('작성 중인 프리셋')
        w.add_target('철괴(철 광석)', 40)
        w.remember_quantity('철괴(철 광석)', 37)
        w.group.setCurrentText('철괴')
        w.route.setCurrentIndex(w.route.findData(['alter', '철괴(철 광석)']))
        self.assertTrue(w.save())
        restored = Window()
        restored.timer.stop()
        try:
            self.load_catalog(restored)
            self.assertEqual(restored.budget.value(), 755)
            self.assertFalse(restored.include_inventory.isChecked())
            self.assertEqual(restored.recovery_mode.currentIndex(), 1)
            self.assertEqual(restored.tabs.currentIndex(), 1)
            self.assertEqual(restored.facility.currentText(), '금속 가공 시설')
            self.assertEqual(restored.search.text(), '철괴')
            self.assertEqual(restored.text.text(), '목재 88')
            self.assertEqual(restored.preset_name.text(), '작성 중인 프리셋')
            self.assertEqual(restored.targets, {'철괴(철 광석)': 40})
            self.assertEqual(restored.card_quantities['철괴(철 광석)'], 37)
            self.assertEqual(restored.group.currentText(), '철괴')
            self.assertEqual(restored.route.currentData(), ['alter', '철괴(철 광석)'])
            self.assertEqual(restored.defaults['철괴'], ['alter', '철괴(철 광석)'])
            self.assertFalse(restored.running)
        finally:
            restored.close()

    def test_autosave_signal_and_close_flush(self):
        w = self.window
        w.budget.setValue(345)
        self.assertTrue(w.autosave.isActive())
        w.autosave.timeout.emit()
        self.assertEqual(json.loads(self.settings_path.read_text(encoding='utf-8'))['ui']['budget'], 345)
        w.text.setText('철괴 100')
        w.close()
        self.assertEqual(json.loads(self.settings_path.read_text(encoding='utf-8'))['ui']['text'], '철괴 100')

    def test_empty_draft_does_not_resurrect_pending_targets(self):
        w = self.window
        w.pending = {'targets': {'목재': 100}, 'budget': 50,
                     'include_inventory': True, 'resolved_targets': {'목재': 100}}
        w.saved['pending'] = w.pending
        w.budget.setValue(500)
        w.include_inventory.setChecked(False)
        w.save()
        restored = Window()
        restored.timer.stop()
        try:
            self.assertEqual(restored.targets, {})
            self.assertEqual(restored.budget.value(), 500)
            self.assertFalse(restored.include_inventory.isChecked())
            self.assertEqual(restored.pending['resolved_targets'], {'목재': 100})
        finally:
            restored.close()


if __name__ == '__main__':
    unittest.main()
