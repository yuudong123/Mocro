"""Resource-consuming actions run against a deterministic simulated game."""
import threading
import json
import unittest

from app import Engine, CLIError, Halt, LOG_FILE, RECIPE_DATA, log_event
from production import Scheduler, Recipe, build_plan, product_name


IRON = '철괴(철 광석)'
METAL = '금속 가공 시설'
WOOD = '목재 가공 시설'


class Game:
    def __init__(self, bag=None, definitions=None, storage=None):
        self.bag = dict(bag or {})
        self.storage = dict(storage or {})
        self.definitions = definitions or {
            IRON: ('alter', 3, {'철 광석': 10}, METAL),
            '강철괴': ('alter', 3, {'철괴': 3, '석탄': 4}, METAL),
            '목재': ('alter', 3, {'통나무': 10}, WOOD),
        }
        self.gatherable = {'철 광석', '석탄', '통나무', '거미줄'}
        self.works = []
        self.actions = []
        self.gather_amount = 100
        self.craft_limit = 100
        self.block_after_queue = False

    def recipe_data(self):
        return {f'{kind}:{name}': {'yield': produced, 'ingredients': dict(ingredients)}
                for name, (kind, produced, ingredients, facility) in self.definitions.items()}

    def tick(self, seconds):
        for facility in {w['FacilityName'] for w in self.works}:
            remaining = seconds
            for work in self.works:
                if work['FacilityName'] != facility or work['IsCompleted']:
                    continue
                work['State'] = 'InProgress'
                elapsed = min(remaining, work['RemainingSeconds'])
                work['RemainingSeconds'] -= elapsed
                remaining -= elapsed
                if work['RemainingSeconds'] <= 0:
                    work.update(IsCompleted=True, State='Completed')
                else:
                    break

    def enqueue(self, name):
        facility = self.definitions[name][3]
        active = any(w['FacilityName'] == facility and not w['IsCompleted'] for w in self.works)
        self.works.append(dict(DisplayName=name, FacilityName=facility, IsCompleted=False,
                               State='NotStarted' if active else 'InProgress', RemainingSeconds=10))

    def consume(self, ingredients, count):
        for name, units in ingredients.items():
            required = units * count
            from_bag = min(required, self.bag.get(name, 0))
            from_storage = required - from_bag
            assert from_storage <= self.storage.get(name, 0)
            self.bag[name] = self.bag.get(name, 0) - from_bag
            self.storage[name] = self.storage.get(name, 0) - from_storage

    def call(self, command, body=None):
        if command == 'get_inventory':
            return {'CurrentInventoryWeight': 0, 'MaxInventoryWeight': 100}
        if command == 'capabilities':
            return {}
        if command == 'get_items':
            return [dict(DisplayName=n, Count=c, Location=location) for source, location in
                    [(self.bag, 'inventory'), (self.storage, 'account_storage')]
                    for n, c in source.items() if c]
        if command == 'get_gatherable_items':
            return {'items': [dict(DisplayName=n, ToolOk=True) for n in sorted(self.gatherable)]}
        if command in ('get_alterable_items', 'get_craftable_items'):
            kind = 'alter' if command == 'get_alterable_items' else 'craft'
            rows = []
            for name, (category, produced, ingredients, facility) in self.definitions.items():
                if kind != category or body and body not in name and not any(body in n for n in ingredients):
                    continue
                missing = [dict(DisplayName=n, Required=c, Owned=self.bag.get(n, 0) + self.storage.get(n, 0))
                           for n, c in ingredients.items() if self.bag.get(n, 0) + self.storage.get(n, 0) < c]
                row = dict(DisplayName=name)
                row['Alterable' if kind == 'alter' else 'Craftable'] = not missing
                row['ProducedPerWork' if kind == 'alter' else 'ProducedPerCraft'] = produced
                if missing:
                    row.update(Reason='not_enough_ingredient', MissingIngredients=missing)
                rows.append(row)
            return {'items': rows}
        if command == 'get_altering_works':
            return {'works': [dict(w) for w in self.works]}
        name = body['displayName'] if body else None
        if command == 'execute_gathering':
            self.actions.append(('gather', name, self.gather_amount))
            self.bag[name] = self.bag.get(name, 0) + self.gather_amount
            self.tick(40)
            return {'result': 'completed'}
        if command == 'execute_altering':
            kind, produced, ingredients, facility = self.definitions[name]
            assert sum(w['FacilityName'] == facility for w in self.works) < 7
            self.consume(ingredients, 1)
            self.enqueue(name)
            self.actions.append(('queue', name))
            if self.block_after_queue:
                self.block_after_queue = False
                raise CLIError(command, {'error': 'blocked', 'kind': 'unknown_modal'})
            return {'result': 'started'}
        if command == 'complete_altering_work':
            facility = self.definitions[name][3]
            completed = [w for w in self.works if w['FacilityName'] == facility and w['IsCompleted']]
            assert completed
            for work in completed:
                output = product_name(work['DisplayName'])
                self.bag[output] = self.bag.get(output, 0) + self.definitions[work['DisplayName']][1]
                self.works.remove(work)
            self.actions.append(('collect', facility, len(completed)))
            return {'collected': len(completed)}
        if command == 'execute_crafting':
            count = body['craftCount']
            if count > self.craft_limit:
                self.actions.append(('limit', name, count))
                raise CLIError(command, {'error': 'invalid_count', 'maxCount': self.craft_limit})
            kind, produced, ingredients, facility = self.definitions[name]
            self.consume(ingredients, count)
            self.bag[name] = self.bag.get(name, 0) + produced * count
            self.actions.append(('craft', name, count))
            self.tick(count)
            return {'result': 'completed'}
        raise AssertionError(command)


class Clock:
    def __init__(self, game):
        self.game = game

    def is_set(self):
        return False

    def wait(self, seconds):
        self.game.tick(seconds)
        return False


class SimEngine(Engine):
    def gather_to(self, name, target, interrupt_when=None):
        self.cli.gather_amount = min(100, target - self.cli.bag.get(name, 0))
        return super().gather_to(name, target, interrupt_when)


class Scheduling(unittest.TestCase):
    def test_s_potion_uses_material_limited_crafting_batches(self):
        name = '뛰어난 자동회복 물약S'
        inputs = {'뛰어난 회복 물약S': 5, '분해된 장비 부품': 180}
        game = Game({'뛰어난 회복 물약S': 350, '분해된 장비 부품': 12600},
                    {name: ('craft', 5, inputs, '약품 제작대')})
        game.craft_limit = 12
        recipes = json.loads(RECIPE_DATA.read_text(encoding='utf-8'))['recipes']
        self.engine(game, known_recipes=recipes).run({name: 350})
        self.assertEqual([a[2] for a in game.actions if a[0] == 'craft'],
                         [12, 12, 12, 12, 12, 10])
        self.assertEqual(game.bag[name], 350)

    def test_high_weight_still_allows_crafting(self):
        class HeavyGame(Game):
            def call(self, command, body=None):
                if command == 'get_inventory':
                    return {'CurrentInventoryWeight': 95, 'MaxInventoryWeight': 100}
                return super().call(command, body)
        game = HeavyGame({'거미줄': 10},
                         {'시험품': ('craft', 1, {'거미줄': 2}, '시험 제작대')})
        self.engine(game).run({'시험품': 5})
        self.assertEqual(game.actions, [('craft', '시험품', 5)])
        self.assertEqual(game.bag['시험품'], 5)

    def test_equipment_uses_acknowledged_crafts_when_inventory_omits_it(self):
        name = '비늘 갑옷 상의'

        class EquipmentGame(Game):
            def call(self, command, body=None):
                result = super().call(command, body)
                if command == 'get_items':
                    return [row for row in result if row['DisplayName'] != name]
                return result

        game = EquipmentGame({'철괴': 6},
                             {name: ('craft', 1, {'철괴': 3}, '방어구 제작대')})
        messages = []
        engine = SimEngine(game, messages.append, Clock(game), budget=10000,
                           known_recipes=game.recipe_data())
        engine.run({name: 2})

        self.assertEqual(game.actions, [('craft', name, 2)])
        self.assertEqual(game.bag[name], 2)
        self.assertEqual(engine.untracked_produced[name], 2)
        self.assertTrue(any('신규 제작 수량' in message for message in messages))

    def test_partial_queue_gathers_missing_logs_before_bulk_resin(self):
        game = Game({'최상급 통나무': 7, '나무 진액': 319},
                    {'목재': ('alter', 3, {'최상급 통나무': 20, '나무 진액': 16}, WOOD)})
        game.gatherable |= {'최상급 통나무', '나무 진액'}
        game.enqueue('목재')
        game.works[0]['RemainingSeconds'] = 6000
        engine = self.engine(game)
        engine.load()
        scheduler = Scheduler(engine, {'목재': 180})
        scheduler.snapshot()
        plan = scheduler.make_plan()
        self.assertGreater(plan.raw['나무 진액'], 0)
        self.assertEqual(scheduler.next_gather(plan), ('최상급 통나무', 20))
        self.assertTrue(scheduler.production_probe('나무 진액', 868))
        self.assertEqual(game.actions, [])

    def test_upper_recipe_does_not_steal_gathering_from_reachable_work(self):
        game = Game({'철 광석': 7}, {
            '목재': ('alter', 3, {'강철괴': 5, '거미줄': 20}, WOOD),
            '강철괴': ('alter', 3, {'철 광석': 10}, METAL)})
        engine = self.engine(game)
        engine.load()
        scheduler = Scheduler(engine, {'목재': 30})
        scheduler.snapshot()
        self.assertEqual(scheduler.next_gather(scheduler.make_plan()), ('철 광석', 10))

    def test_probe_detects_newly_affordable_work_without_executing(self):
        game = Game({'철 광석': 0})
        engine = self.engine(game)
        engine.load()
        scheduler = Scheduler(engine, {'철괴': 21})
        self.assertFalse(scheduler.production_probe())
        game.bag['철 광석'] = 10
        self.assertTrue(scheduler.production_probe())
        self.assertEqual(game.actions, [])

    def test_probe_batches_refills_for_long_running_queue(self):
        game = Game({'철 광석': 10})
        game.enqueue(IRON)
        game.works[0]['RemainingSeconds'] = 1200
        engine = self.engine(game)
        engine.load()
        scheduler = Scheduler(engine, {'철괴': 30})
        self.assertFalse(scheduler.production_probe())
        game.bag['철 광석'] = 30
        self.assertTrue(scheduler.production_probe())

    def engine(self, game, **kwargs):
        return SimEngine(game, lambda _: None, Clock(game), budget=10000,
                         defaults={'철괴': ['alter', IRON]},
                         known_recipes=kwargs.pop('known_recipes', game.recipe_data()), **kwargs)

    def test_80_ore_starts_seven_works_before_gathering(self):
        game = Game({'철 광석': 80})
        self.engine(game).run({'철괴': 100})
        self.assertEqual(game.actions[:7], [('queue', IRON)] * 7)
        self.assertEqual(game.actions[7][0], 'gather')
        self.assertEqual(sum(a[2] for a in game.actions if a[0] == 'gather'), 260)
        self.assertEqual(sum(a[0] == 'queue' for a in game.actions), 34)
        self.assertEqual(game.bag['철괴'], 102)

    def test_shared_material_and_final_reserves_in_both_orders(self):
        for goals in ({'철괴': 100, '강철괴': 100}, {'강철괴': 100, '철괴': 100}):
            with self.subTest(goals=goals):
                game = Game({'철 광석': 80})
                self.engine(game).run(goals)
                self.assertEqual(game.bag['철괴'], 102)
                self.assertEqual(game.bag['강철괴'], 102)
                self.assertEqual(game.actions.count(('queue', IRON)), 68)
                self.assertEqual(game.actions.count(('queue', '강철괴')), 34)
                self.assertEqual(sum(a[2] for a in game.actions if a[:2] == ('gather', '철 광석')), 600)
                self.assertEqual(sum(a[2] for a in game.actions if a[:2] == ('gather', '석탄')), 136)

    def test_second_facility_runs_before_first_finishes(self):
        game = Game({'철 광석': 70, '통나무': 70})
        self.engine(game).run({'철괴': 21, '목재': 21})
        first_collection = next(i for i, a in enumerate(game.actions) if a[0] == 'collect')
        self.assertEqual(first_collection, 14)
        self.assertEqual(game.actions[:2], [('queue', IRON), ('queue', '목재')])
        self.assertEqual(game.actions[:14].count(('queue', IRON)), 7)
        self.assertEqual(game.actions[:14].count(('queue', '목재')), 7)

    def test_collection_refills_same_facility_before_another_trip(self):
        game = Game({'철 광석': 140, '통나무': 140})
        self.engine(game).run({'철괴': 42, '목재': 42})
        first_collection = next(i for i, a in enumerate(game.actions) if a[0] == 'collect')
        facility = game.actions[first_collection][1]
        expected = IRON if facility == METAL else '목재'
        self.assertEqual(game.actions[first_collection+1], ('queue', expected))
        self.assertGreaterEqual(game.bag['철괴'], 42)
        self.assertGreaterEqual(game.bag['목재'], 42)

    def test_pending_jobs_are_not_produced_or_gathered_again(self):
        game = Game({'철 광석': 10})
        for _ in range(7):
            game.enqueue(IRON)
        self.engine(game).run({'철괴': 100})
        self.assertEqual(game.actions.count(('queue', IRON)), 27)
        self.assertEqual(sum(a[2] for a in game.actions if a[0] == 'gather'), 260)
        self.assertEqual(game.bag['철괴'], 102)

    def test_already_satisfied_output_does_not_cause_unneeded_collection(self):
        game = Game({'철괴': 3, '철 광석': 10})
        game.enqueue(IRON)
        game.tick(10)
        self.engine(game).run({'철괴': 3, '철 광석': 20})
        self.assertEqual(game.actions, [('gather', '철 광석', 10)])
        self.assertEqual(len(game.works), 1)

    def test_partial_success_then_modal_replans_registered_work(self):
        game = Game({'철 광석': 80})
        game.block_after_queue = True
        recovered = []
        engine = self.engine(game, recovery=lambda *args: recovered.append(True))
        engine.run({'철괴': 24})
        self.assertEqual(recovered, [True])
        self.assertEqual(game.actions.count(('queue', IRON)), 8)
        self.assertEqual(game.bag['철괴'], 24)
        self.assertEqual(engine.calls, 8)

    def test_storage_covers_ingredients_but_not_final_bag_goals(self):
        game = Game(storage={'철 광석': 80})
        self.engine(game).run({'철괴': 24, '철 광석': 10})
        self.assertEqual(game.actions[:7], [('queue', IRON)] * 7)
        self.assertEqual(game.bag['철괴'], 24)
        self.assertEqual(game.bag['철 광석'], 10)
        self.assertEqual(sum(a[2] for a in game.actions if a[0] == 'gather'), 10)

    def test_other_recipes_occupy_slots_and_are_collected_together(self):
        game = Game({'철 광석': 100})
        for _ in range(3):
            game.enqueue('강철괴')
        self.engine(game).run({'철괴': 30})
        first_collection = next(i for i, a in enumerate(game.actions) if a[0] == 'collect')
        self.assertEqual(first_collection, 4)
        self.assertEqual(game.actions[first_collection], ('collect', METAL, 7))

    def test_full_facility_of_other_work_is_waited_for(self):
        game = Game({'철 광석': 10})
        for _ in range(7):
            game.enqueue('강철괴')
        self.engine(game).run({'철괴': 3})
        self.assertEqual(game.actions[0], ('collect', METAL, 7))
        self.assertEqual(game.actions[1], ('queue', IRON))

    def test_live_facility_name_alias_counts_other_recipes(self):
        game = Game({'철 광석': 10})
        game.definitions = {n: (k, p, items, '금속 가공' if facility == METAL else facility)
                            for n, (k, p, items, facility) in game.definitions.items()}
        for _ in range(7):
            game.enqueue('강철괴')
        self.engine(game).run({'철괴': 3})
        self.assertEqual(game.actions[0], ('collect', '금속 가공', 7))

    def test_first_gather_bootstraps_one_work(self):
        game = Game()
        self.engine(game).run({'철괴': 100})
        self.assertEqual(game.actions[0], ('gather', '철 광석', 10))
        self.assertEqual(game.actions[1], ('queue', IRON))

    def test_all_ingredients_for_first_batch_before_bulk_surplus(self):
        game = Game(definitions={'목재': ('alter', 3, {'철 광석': 10, '통나무': 10}, WOOD)})
        self.engine(game).run({'목재': 100})
        self.assertEqual(game.actions[:2], [('gather', '철 광석', 10), ('gather', '통나무', 10)])
        self.assertEqual(game.actions[2], ('queue', '목재'))

    def test_crafting_batches_and_learns_facility_limit(self):
        game = Game({'거미줄': 40}, {'시험품': ('craft', 2, {'거미줄': 2}, '시험 제작대')})
        game.craft_limit = 7
        self.engine(game).run({'시험품': 40})
        self.assertEqual([a[2] for a in game.actions if a[0] == 'craft'], [7, 7, 6])
        self.assertEqual(sum(a[0] == 'limit' for a in game.actions), 1)
        self.assertEqual(game.bag['시험품'], 40)

    def test_cli_changed_recipe_overrides_public_quantity(self):
        game = Game(definitions={IRON: ('alter', 3, {'철 광석': 15}, METAL)})
        data = game.recipe_data()
        data[f'alter:{IRON}']['ingredients']['철 광석'] = 10
        self.engine(game, known_recipes=data).run({'철괴': 6})
        self.assertEqual(game.bag['철괴'], 6)
        self.assertEqual(sum(a[2] for a in game.actions if a[0] == 'gather'), 30)

    def test_missing_recipe_still_starts_with_owned_material(self):
        game = Game({'철 광석': 80})
        self.engine(game, known_recipes={}).run({'철괴': 30})
        self.assertEqual(game.actions[:7], [('queue', IRON)] * 7)
        self.assertEqual(game.bag['철괴'], 30)

    def test_real_parenthesis_variants_keep_separate_balances(self):
        game = Game({'염료(빨강)': 5, '염료(파랑)': 0})
        game.gatherable |= {'염료(빨강)', '염료(파랑)'}
        self.engine(game).run({'염료(빨강)': 5, '염료(파랑)': 2})
        self.assertEqual(game.actions, [('gather', '염료(파랑)', 2)])


class MaterialPlans(unittest.TestCase):
    def test_duplicate_live_recipe_rows_merge_dynamic_availability(self):
        rows = [
            {'DisplayName': '은합금괴', 'Alterable': False, 'ProducedPerWork': 3,
             'Reason': 'not_enough_ingredient',
             'MissingIngredients': [
                 {'DisplayName': '특수강괴', 'Required': 5, 'Owned': 0},
                 {'DisplayName': '은 광석', 'Required': 20, 'Owned': 0},
                 {'DisplayName': '석탄', 'Required': 16, 'Owned': 0}]},
            {'DisplayName': '은합금괴', 'Alterable': False, 'ProducedPerWork': 3,
             'Reason': 'not_enough_ingredient',
             'MissingIngredients': [
                 {'DisplayName': '은 광석', 'Required': 20, 'Owned': 0},
                 {'DisplayName': '석탄', 'Required': 16, 'Owned': 0}]},
        ]
        merged = Scheduler.merge_recipe_rows(rows, 'alter', '은합금괴')
        self.assertEqual(merged['ProducedPerWork'], 3)
        self.assertEqual({item['DisplayName']: item['Required']
                          for item in merged['MissingIngredients']},
                         {'특수강괴': 5, '은 광석': 20, '석탄': 16})

    def test_different_live_recipe_yields_remain_ambiguous(self):
        rows = [
            {'DisplayName': '품목', 'Alterable': True, 'ProducedPerWork': 3},
            {'DisplayName': '품목', 'Alterable': True, 'ProducedPerWork': 5},
        ]
        with self.assertRaisesRegex(ValueError, '모호'):
            Scheduler.merge_recipe_rows(rows, 'alter', '품목')

    def test_diagnostic_log_is_persistent_json(self):
        log_event('INFO', 'test.diagnostic', run_id='test-run', details={'ok': True})
        self.assertTrue(LOG_FILE.exists())
        self.assertIn('test.diagnostic', LOG_FILE.read_text(encoding='utf-8'))

    def test_live_missing_replaces_self_and_plus_typo(self):
        corrected = Scheduler.reconcile_ingredients(
            '최상급 목재+',
            {'최상급 목재+': 5, '최상급 통나무+': 20, '나무 진액': 20},
            [{'DisplayName': '최상급 목재', 'Required': 5, 'Owned': 3},
             {'DisplayName': '최상급 통나무', 'Required': 20, 'Owned': 0}])
        self.assertEqual(corrected['최상급 목재'], 5)
        self.assertEqual(corrected['최상급 통나무'], 20)
        self.assertNotIn('최상급 목재+', corrected)
        self.assertNotIn('최상급 통나무+', corrected)

    def test_live_missing_replaces_public_spelling_typo(self):
        corrected = Scheduler.reconcile_ingredients(
            '상급 화염 마법 유탄',
            {'불꽃의 결정': 9, '상금 마법 유탄 부품': 3},
            [{'DisplayName': '상급 마법 유탄 부품', 'Required': 3, 'Owned': 0}])
        self.assertEqual(corrected['상급 마법 유탄 부품'], 3)
        self.assertNotIn('상금 마법 유탄 부품', corrected)

    def test_diamond_uses_shared_rounding_and_queued_output(self):
        recipes = {
            'A': Recipe('craft', 'A', 'A', 1, {'C': 2}, True, 'A시설', False),
            'B': Recipe('craft', 'B', 'B', 1, {'C': 2}, True, 'B시설', False),
            'C': Recipe('alter', 'C', 'C', 3, {'원료': 10}, True, 'C시설', False),
        }
        plan = build_plan({'A': 1, 'B': 1, 'C': 1}, {}, {}, {'C': 3}, {'원료'}, recipes.get)
        self.assertEqual(plan.jobs, {'A': 1, 'B': 1, 'C': 1})
        self.assertEqual(plan.raw, {'원료': 10})

    def test_cycle_fails_before_action(self):
        recipes = {
            'A': Recipe('alter', 'A', 'A', 1, {'B': 1}, True, '시설', False),
            'B': Recipe('alter', 'B', 'B', 1, {'A': 1}, True, '시설', False),
        }
        with self.assertRaisesRegex(ValueError, '순환'):
            build_plan({'A': 1}, {}, {}, {}, set(), recipes.get)


class GatheringMonitor(unittest.TestCase):
    def test_weight_reaching_90_stops_gather_before_quantity_target(self):
        class HeavyCLI(self.RunningCLI):
            def call(self, cmd, body=None):
                if cmd == 'get_inventory':
                    return {'CurrentInventoryWeight': 90 if self.ready else 89,
                            'MaxInventoryWeight': 100}
                return super().call(cmd, body)
        cli = HeavyCLI()
        self.assertTrue(self.engine(cli).gather_to('철 광석', 100))
        self.assertEqual(cli.stop_calls, 1)
        self.assertEqual(cli.amount, 2)

    def test_resin_route_monitors_resin_not_logs(self):
        class ResinCLI:
            def __init__(self):
                self.resin = 0
                self.stopped = threading.Event()
                self.requested = None

            def call(self, cmd, body=None):
                if cmd == 'get_inventory':
                    return {'CurrentInventoryWeight': 0, 'MaxInventoryWeight': 100}
                if cmd == 'get_items':
                    return [dict(DisplayName='나무 진액', Count=self.resin, Location='inventory')]
                if cmd == 'execute_gathering':
                    self.requested = body['displayName']
                    self.resin = 5
                    if not self.stopped.wait(2):
                        raise AssertionError('resin target did not stop gathering')
                    return {'result': 'stopped_by_user'}
                if cmd == 'stop_action':
                    self.stopped.set()
                    return {}
                raise AssertionError(cmd)

        cli = ResinCLI()
        engine = self.engine(cli)
        engine.gather = {'부드러운 통나무': {'ToolOk': True}}
        engine.gather_to('나무 진액', 5)
        self.assertEqual(cli.requested, '부드러운 통나무')
        self.assertTrue(cli.stopped.is_set())

    class RunningCLI:
        def __init__(self, fishing=False, final_error=None):
            self.stopped = threading.Event()
            self.amount = 0
            self.ready = False
            self.fishing = fishing
            self.final_error = final_error
            self.stop_calls = 0

        def call(self, cmd, body=None):
            if cmd == 'get_inventory':
                return {'CurrentInventoryWeight': 0, 'MaxInventoryWeight': 100}
            if cmd == 'get_items':
                return [dict(DisplayName='철 광석', Count=self.amount, Location='inventory')]
            if cmd == 'execute_gathering':
                self.amount = 2
                self.ready = True
                if self.fishing:
                    return {'result': 'started'}
                if not self.stopped.wait(2):
                    raise AssertionError('채집 중지 요청이 오지 않았습니다.')
                if self.final_error:
                    raise CLIError(cmd, {'error': self.final_error})
                return {'result': 'stopped_by_user'}
            if cmd == 'stop_action':
                self.stop_calls += 1
                self.stopped.set()
                return {}
            raise AssertionError(cmd)

    def engine(self, cli):
        engine = Engine(cli, lambda _: None, threading.Event(), known_recipes={})
        engine.poll_seconds = 0.001
        engine.gather_probe_seconds = 0.001
        return engine

    def test_queue_completion_interrupts_before_gather_target(self):
        cli = self.RunningCLI()
        yielded = self.engine(cli).gather_to('철 광석', 100, interrupt_when=lambda: cli.ready)
        self.assertTrue(yielded)
        self.assertEqual(cli.amount, 2)
        self.assertEqual(cli.stop_calls, 1)

    def test_real_error_is_not_swallowed_by_intentional_stop(self):
        cli = self.RunningCLI(final_error='blocked')
        with self.assertRaises(CLIError):
            self.engine(cli).gather_to('철 광석', 100, interrupt_when=lambda: cli.ready)

    def test_fishing_start_is_monitored_until_target(self):
        cli = self.RunningCLI(fishing=True)
        self.assertFalse(self.engine(cli).gather_to('철 광석', 2))
        self.assertEqual(cli.stop_calls, 1)

    def test_fishing_yields_when_processing_finishes(self):
        cli = self.RunningCLI(fishing=True)
        self.assertTrue(self.engine(cli).gather_to('철 광석', 100, interrupt_when=lambda: cli.ready))
        self.assertEqual(cli.stop_calls, 1)

    def test_fishing_already_stopped_race_does_not_spin(self):
        class AlreadyStopped(self.RunningCLI):
            def call(self, cmd, body=None):
                if cmd == 'stop_action':
                    self.stop_calls += 1
                    raise CLIError(cmd, {'error': 'invalid_state'})
                return super().call(cmd, body)
        cli = AlreadyStopped(fishing=True)
        self.assertFalse(self.engine(cli).gather_to('철 광석', 2))
        self.assertEqual(cli.stop_calls, 1)


if __name__ == '__main__':
    unittest.main()
