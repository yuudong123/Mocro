"""Character lifecycle regressions without game input or currency spending."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import macro
from macro import Exhausted, Macro, Stop
from roster import Roster


class FakeGame:
    def __init__(self):
        self.active = self.selected = 0
        self.selection = False
        self.logins = []
        self.money = {0: {'은동전': 42, '마족 공물': 2},
                      1: {'은동전': 69, '마족 공물': 2}}

    def capture(self):
        return object()

    def find(self, name, *args, **kwargs):
        return (1, 1) if name == 'select_title' and self.selection else None

    def scroll(self, *args):
        pass

    def click(self, x, y):
        if (x, y) == macro.GAME_START:
            self.active = self.selected
            self.selection = False
            self.logins.append(self.active)
        else:
            self.selected = x


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = FakeGame()
        # Same metadata deliberately: CLI does not expose a unique character ID.
        self.info = {'id': ('알리사', '댄서', '근면왕'), 'realm': '알리사',
                     'job': '댄서', 'title': '근면왕', 'level': 100}
        self.logs = []
        self.m = Macro(self.game, threading.Event(), self.logs.append,
                       config={'characters': {'5': {'include': False}}, 'clean': False,
                               'rune_grades': []})
        self.m.roster = Roster(Path(self.temp.name) / 'characters.json')
        self.m.wait = lambda seconds: self.m.check()
        self.m.me = lambda: dict(self.info)
        self.m.activity = lambda: {'dungeon': 'NotInDungeon'}
        self.m.cards = lambda image: [dict(slot=s, lv100=s < 2, selected=s == self.game.selected,
                                          center=(s, 100)) for s in range(6)]
        self.m.to_character_select = lambda: setattr(self.game, 'selection', True)
        self.m.screenshot = lambda tag: 'simulated.png'
        self.m.settled_character = lambda: (dict(self.info), dict(self.game.money[self.game.active]))
        self.m.until = self.until
        self.money_patch = patch.object(macro, 'currencies',
                                        side_effect=lambda: dict(self.game.money[self.game.active]))
        self.money_patch.start()
        self.addCleanup(self.money_patch.stop)
        cli_patch = patch.object(macro, 'cli', return_value={'Dungeon': {'State': 'NotInDungeon'}})
        cli_patch.start()
        self.addCleanup(cli_patch.stop)
        self.clean = Mock()
        self.m.clean_bag = self.clean

    def until(self, what, done, retry, **kwargs):
        if not done():
            retry()
        self.assertTrue(done(), what)

    def test_unknown_card_with_an_excluded_slot_is_identified_then_runs(self):
        visited = []

        def run():
            visited.append(self.m.slot)
            self.assertTrue(self.m.uses(macro.ROUTES[0]))
            self.assertTrue(self.m.uses(macro.ROUTES[1]))
            self.game.money[self.game.active] = {'은동전': 0, '마족 공물': 0}
            raise Exhausted('재화 부족')

        self.m.run = run
        with self.assertRaisesRegex(Stop, '모든.*완료'):
            self.m.run_all()
        self.assertEqual(visited, [0, 1])
        self.assertEqual(self.game.logins, [0, 1])
        self.assertEqual(self.m.completed_slots, {0, 1})
        self.assertEqual(self.clean.call_count, 2)
        self.assertEqual(set(self.m.roster.cards), {'0', '1'})

    def test_discovery_in_existing_dungeon_does_not_mark_current_as_finished(self):
        self.m.activity = lambda: {'dungeon': 'Cleared'}
        calls = []

        def run():
            calls.append(self.m.slot)
            if len(calls) == 1:
                raise Exhausted('카드 번호 모름')
            raise Stop('확인 종료')

        self.m.run = run
        with self.assertRaisesRegex(Stop, '확인 종료'):
            self.m.run_all()
        self.assertEqual(calls, [None, 0])
        self.assertEqual(self.m.completed_slots, set())
        self.clean.assert_not_called()

    def test_finished_card_with_remaining_currency_is_not_reselected(self):
        self.m.switch_character()
        self.assertEqual(self.game.logins, [1])
        self.assertEqual(self.m.completed_slots, {0})

    def test_disabled_card_is_never_selected(self):
        self.m.config['characters']['0'] = {'include': False}
        self.m.switch_character(finished=False)
        self.assertEqual(self.game.logins, [1])

    def test_discovery_prefers_current_allowed_card(self):
        self.game.active = self.game.selected = 1
        self.m.switch_character(finished=False)
        self.assertEqual(self.game.logins, [1])
        self.assertEqual(self.m.completed_slots, set())

    def test_wait_starts_new_round_after_recharge(self):
        self.m.config['wait'] = True
        self.m.completed_slots = {0, 1}
        clock = [100.0]
        self.m.roster.now = lambda: clock[0]
        self.m.wait = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        self.m.switch_character()
        self.assertEqual(self.game.logins, [0])
        self.assertEqual(self.m.completed_slots, set())
        self.assertGreaterEqual(clock[0], 160)

    def test_wait_can_be_stopped(self):
        self.m.config['wait'] = True
        self.m.completed_slots = {0, 1}

        def stop(seconds):
            self.m.stop.set()
            self.m.check()

        self.m.wait = stop
        with self.assertRaisesRegex(Stop, '중지 요청'):
            self.m.switch_character()
        self.assertEqual(self.game.logins, [])

    def test_ambiguous_selected_card_halts_without_writing_roster(self):
        self.m.cards = lambda image: [dict(slot=0, selected=True), dict(slot=1, selected=True)]
        with self.assertRaisesRegex(Stop, '하나로 확인'):
            self.m.switch_character()
        self.assertEqual(self.m.roster.cards, {})

    def test_no_switch_with_unknown_restricted_card_stops_before_running(self):
        self.m.run = Mock()
        with self.assertRaisesRegex(Stop, '카드 번호'):
            self.m.run_all(switch=False)
        self.m.run.assert_not_called()

    def test_level_mismatch_is_skipped_iteratively(self):
        def settled():
            return {**self.info, 'level': 99 if self.game.active == 0 else 100}, self.game.money[self.game.active]
        self.m.settled_character = settled
        self.m.switch_character(finished=False)
        self.assertEqual(self.game.logins, [0, 1])
        self.assertEqual(self.m.skip_slots, {0})

    def test_invalid_currency_does_not_overwrite_last_known_inventory(self):
        self.m.slot, self.m.ident = 0, self.info['id']
        self.m.remember({'은동전': 42, '마족 공물': 2})
        self.m.remember({'error': 'not_in_game'})
        self.m.remember({})
        self.assertEqual(self.m.roster.cards['0']['은동전'], 42)
        self.assertEqual(self.m.roster.cards['0']['마족 공물'], 2)


class Guards(unittest.TestCase):
    def test_login_waits_for_stable_character_currency_and_field(self):
        m = Macro(Mock(), threading.Event(), lambda line: None)
        clock = [0.0]
        info = {'id': ('알리사', '댄서', '근면왕'), 'level': 100, 'realm': '알리사', 'job': '댄서'}
        m.me = lambda: info
        m.wait = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        m.close_notice = lambda image: False
        m.skip_dialogue = lambda activity: False
        m.hud = lambda image: clock[0] >= 5

        def cli(command):
            if command == 'get_activity':
                return {'Dungeon': {'State': 'NotInDungeon'}}
            return {'GameSpaceDisplayName': '센마이 평원'}

        with patch.object(macro, 'cli', side_effect=cli), \
                patch.object(macro, 'currencies', side_effect=lambda: {'은동전': 42, '마족 공물': 2}), \
                patch.object(macro.time, 'monotonic', side_effect=lambda: clock[0]):
            got_info, money = m.settled_character()
        self.assertEqual(got_info, info)
        self.assertEqual(money['은동전'], 42)
        self.assertGreaterEqual(clock[0], 11)

    def test_roster_keeps_same_metadata_on_different_cards(self):
        with tempfile.TemporaryDirectory() as temp:
            roster = Roster(Path(temp) / 'characters.json')
            ident = ('알리사', '마법사', '근면왕')
            roster.record(0, ident, {'은동전': 20, '마족 공물': 1})
            roster.record(1, ident, {'은동전': 60, '마족 공물': 3})
            self.assertEqual(set(roster.cards), {'0', '1'})
            self.assertIsNone(roster.slot_of(ident))

    def test_overweight_respects_disabled_cleanup(self):
        m = Macro(Mock(), threading.Event(), lambda line: None,
                  config={'characters': {'0': {'items': False, 'equip': False, 'rune': False}}})
        m.slot = 0
        m.weight = lambda: (110, 100)
        with self.assertRaisesRegex(Stop, '가방 정리 후에도'):
            m.make_room()
        m.game.assert_not_called()
        self.assertEqual(m.game.mock_calls, [])

    def test_no_clean_disables_all_cleanup_actions(self):
        m = Macro(Mock(), threading.Event(), lambda line: None, clean=False)
        m.clean_bag(force=True)
        self.assertEqual(m.game.mock_calls, [])

    def test_unstable_currency_timeout_is_not_accepted_as_zero(self):
        m = Macro(Mock(), threading.Event(), lambda line: None)
        m.wait = lambda seconds: None
        with patch.object(macro, 'currencies', return_value={'error': 'not_in_game'}), \
                patch.object(macro.time, 'monotonic', side_effect=[0, 0, 41]):
            with self.assertRaisesRegex(Stop, '안정되지'):
                m.settled_currencies()


if __name__ == '__main__':
    unittest.main()
