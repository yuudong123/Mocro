import unittest

from target_modes import freeze_targets, matches_pending
from test_scheduler import Game, SimEngine, Clock


class QuantityModes(unittest.TestCase):
    def test_include_inventory_is_existing_final_target(self):
        self.assertEqual(freeze_targets({'강철괴': 40}, {'강철괴': 100}, True), {'강철괴': 40})

    def test_exclude_inventory_adds_starting_holdings(self):
        self.assertEqual(freeze_targets({'강철괴': 40}, {'강철괴': 100}, False), {'강철괴': 140})

    def test_route_alias_and_real_variants(self):
        self.assertEqual(freeze_targets({'철괴(철 광석)': 40, '염료(빨강)': 2},
                                       {'철괴': 100, '염료(파랑)': 50}, False),
                         {'철괴(철 광석)': 140, '염료(빨강)': 2})

    def test_legacy_pending_defaults_to_include(self):
        pending = {'targets': {'강철괴': 40}}
        self.assertTrue(matches_pending(pending, {'강철괴': 40}, True))
        self.assertFalse(matches_pending(pending, {'강철괴': 40}, False))

    def test_extra_mode_produces_on_top_of_existing_items(self):
        for include, expected in [(True, 100), (False, 140)]:
            game = Game({'시험품': 100, '거미줄': 80},
                        {'시험품': ('craft', 1, {'거미줄': 2}, '시험 제작대')})
            goals = freeze_targets({'시험품': 40}, game.bag, include)
            engine = SimEngine(game, lambda _: None, Clock(game), known_recipes=game.recipe_data())
            engine.run(goals)
            self.assertEqual(game.bag['시험품'], expected)


if __name__ == '__main__':
    unittest.main()
