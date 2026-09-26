import threading
import unittest
from app import Engine, Halt, CLIError, parse_targets


class FakeCLI:
    def __init__(self):
        self.items = {}
        self.works = []
        self.actions = []

    def call(self, cmd, body=None):
        if cmd == 'get_inventory':
            return {'CurrentInventoryWeight': 0, 'MaxInventoryWeight': 100}
        if cmd == 'capabilities':
            return {}
        if cmd == 'get_items':
            return [dict(DisplayName=n, Count=c, Location='inventory', IsLocked=False)
                    for n, c in self.items.items()]
        if cmd == 'get_gatherable_items':
            return {'items': [dict(DisplayName=n, ToolOk=True) for n in ('통나무', '최상급 거미줄')]}
        if cmd == 'get_craftable_items':
            return {'items': []}
        if cmd == 'get_alterable_items':
            count = self.items.get('통나무', 0)
            return {'items': [dict(DisplayName='목재', Alterable=count >= 10,
                                   ProducedPerWork=3, Reason='not_enough_ingredient',
                                   MissingIngredients=[dict(DisplayName='통나무', Required=10, Owned=count)])]}
        if cmd == 'get_altering_works':
            return {'works': self.works}
        self.actions.append(cmd)
        if cmd == 'execute_gathering':
            name = body['displayName']
            self.items[name] = self.items.get(name, 0) + 100
            return {'result': 'completed'}
        if cmd == 'execute_altering':
            self.items['통나무'] -= 10
            self.works.append(dict(DisplayName='목재', FacilityName='목재 가공', IsCompleted=True))
            return {'result': 'started'}
        if cmd == 'complete_altering_work':
            count = len(self.works)
            self.works = []
            self.items['목재'] = self.items.get('목재', 0) + 3 * count
            return {'collected': count}
        raise AssertionError(cmd)


class Tests(unittest.TestCase):
    def test_high_weight_queues_and_collects_then_waits(self):
        class HeavyCLI(FakeCLI):
            def call(self, cmd, body=None):
                if cmd == 'get_inventory':
                    return {'CurrentInventoryWeight': 90, 'MaxInventoryWeight': 100}
                return super().call(cmd, body)

        class CancelOnWait(threading.Event):
            def wait(self, timeout=None):
                self.set()
                return True

        cli = HeavyCLI()
        cli.items['통나무'] = 10
        engine = Engine(cli, lambda _: None, CancelOnWait(), known_recipes={})
        with self.assertRaisesRegex(Halt, '중지됨'):
            engine.run({'목재': 6})
        self.assertEqual(cli.actions, ['execute_altering', 'complete_altering_work'])
        self.assertEqual(cli.items['목재'], 3)
        self.assertEqual(len(cli.works), 0)

    def test_weight_recovery_resumes_after_wait(self):
        class HeavyCLI(FakeCLI):
            weight = 90
            def call(self, cmd, body=None):
                if cmd == 'get_inventory':
                    return {'CurrentInventoryWeight': self.weight, 'MaxInventoryWeight': 100}
                return super().call(cmd, body)

        cli = HeavyCLI()
        class ReleaseOnWait(threading.Event):
            def wait(self, timeout=None):
                cli.weight = 89
                return False

        engine = Engine(cli, lambda _: None, ReleaseOnWait(), known_recipes={})
        engine.run({'통나무': 10})
        self.assertEqual(cli.actions, ['execute_gathering'])

    def test_not_in_game_read_waits_until_reconnected(self):
        class ReconnectingCLI(FakeCLI):
            def __init__(self):
                super().__init__()
                self.misses = 2

            def call(self, cmd, body=None):
                if cmd == 'get_items' and self.misses:
                    self.misses -= 1
                    raise CLIError(cmd, {'error': 'not_in_game'})
                return super().call(cmd, body)

        cli = ReconnectingCLI()
        engine = self.engine(cli)
        engine.reconnect_seconds = 0.001
        engine.run({'통나무': 10})
        self.assertEqual(cli.misses, 0)
        self.assertEqual(cli.actions, ['execute_gathering'])

    def test_not_in_game_after_partial_action_replans_without_replaying(self):
        class PartialCLI(FakeCLI):
            def call(self, cmd, body=None):
                if cmd == 'execute_gathering':
                    self.actions.append(cmd)
                    self.items['통나무'] = 10
                    raise CLIError(cmd, {'error': 'not_in_game'})
                return super().call(cmd, body)

        cli = PartialCLI()
        engine = self.engine(cli)
        engine.reconnect_seconds = 0.001
        engine.run({'통나무': 10})
        self.assertEqual(cli.actions, ['execute_gathering'])
        self.assertEqual(cli.items['통나무'], 10)

    def test_not_in_game_wait_can_be_canceled(self):
        class OfflineCLI(FakeCLI):
            def call(self, cmd, body=None):
                if cmd == 'get_items':
                    raise CLIError(cmd, {'error': 'not_in_game'})
                return super().call(cmd, body)

        class StopAfterThree:
            def __init__(self):
                self.waits = 0
                self.stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, seconds):
                self.waits += 1
                self.stopped = self.waits == 3
                return self.stopped

        stop = StopAfterThree()
        engine = Engine(OfflineCLI(), lambda _: None, stop, known_recipes={})
        with self.assertRaisesRegex(Halt, '중지됨'):
            engine.stock()
        self.assertEqual(stop.waits, 3)

    def test_timed_out_gather_is_settled_then_replanned(self):
        class TimedOutCLI(FakeCLI):
            def call(self, cmd, body=None):
                if cmd == 'execute_gathering':
                    self.actions.append(cmd)
                    self.items['통나무'] = 10
                    raise CLIError(cmd, {'error': 'timeout', 'gained': 10})
                if cmd == 'stop_action':
                    self.actions.append(cmd)
                    raise CLIError(cmd, {'error': 'invalid_state'})
                return super().call(cmd, body)

        cli = TimedOutCLI()
        engine = self.engine(cli)
        engine.reconnect_seconds = 0.001
        engine.run({'통나무': 10})
        self.assertEqual(cli.actions, ['execute_gathering', 'stop_action'])

    def test_altering_fills_seven_slots_then_refills(self):
        class QueueCLI(FakeCLI):
            def __init__(self):
                super().__init__()
                self.items['통나무'] = 100
                self.max_occupied = 0
                self.submitted_before_first_collection = 0
                self.collections = 0

            def call(self, cmd, body=None):
                if cmd == 'get_altering_works':
                    return {'works': list(self.works)}
                if cmd == 'execute_altering':
                    assert len(self.works) < 7
                    self.items['통나무'] -= 10
                    self.works.append(dict(DisplayName='목재', FacilityName='목재 가공 시설',
                                           State='InProgress', IsCompleted=False, RemainingSeconds=1))
                    self.max_occupied = max(self.max_occupied, len(self.works))
                    if not self.collections:
                        self.submitted_before_first_collection += 1
                    self.actions.append(cmd)
                    return {'result': 'started'}
                if cmd == 'complete_altering_work':
                    completed = [w for w in self.works if w['IsCompleted']]
                    assert completed
                    self.items['목재'] = self.items.get('목재', 0) + 3 * len(completed)
                    self.works = [w for w in self.works if not w['IsCompleted']]
                    self.collections += 1
                    self.actions.append(cmd)
                    return {'collected': len(completed)}
                return super().call(cmd, body)

        class Clock:
            def __init__(self, cli):
                self.cli = cli

            def is_set(self):
                return False

            def wait(self, seconds):
                active = next((w for w in self.cli.works if not w['IsCompleted']), None)
                if active:
                    active.update(State='Completed', IsCompleted=True, RemainingSeconds=0)
                return False

        cli = QueueCLI()
        engine = Engine(cli, lambda _: None, Clock(cli), budget=100, known_recipes={})
        engine.run({'목재': 24})
        self.assertEqual(cli.submitted_before_first_collection, 7)
        self.assertEqual(cli.max_occupied, 7)
        self.assertEqual(cli.actions.count('execute_altering'), 8)
        self.assertEqual(cli.items['목재'], 24)

    def test_gathers_while_altering_then_collects_together(self):
        class OverlapCLI(FakeCLI):
            def __init__(self):
                super().__init__()
                self.items['통나무'] = 30
                self.items['거미줄'] = 0
                self.sequence = []

            def call(self, cmd, body=None):
                if cmd == 'get_gatherable_items':
                    return {'items': [dict(DisplayName=n, ToolOk=True) for n in ('통나무', '거미줄')]}
                if cmd == 'get_altering_works':
                    return {'works': list(self.works)}
                if cmd == 'execute_altering':
                    self.items['통나무'] -= 10
                    self.works.append(dict(DisplayName='목재', FacilityName='목재 가공 시설',
                                           State='InProgress', IsCompleted=False, RemainingSeconds=2))
                    self.sequence.append('queue')
                    return {'result': 'started'}
                if cmd == 'execute_gathering':
                    self.items[body['displayName']] = 10
                    for w in self.works:
                        w.update(State='Completed', IsCompleted=True, RemainingSeconds=0)
                    self.sequence.append('gather')
                    return {'result': 'completed'}
                if cmd == 'complete_altering_work':
                    assert all(w['IsCompleted'] for w in self.works)
                    self.items['목재'] = 3 * len(self.works)
                    self.works = []
                    self.sequence.append('collect')
                    return {'collected': 2}
                return super().call(cmd, body)

        cli = OverlapCLI()
        engine = Engine(cli, lambda _: None, threading.Event(), budget=50, known_recipes={})
        engine.run({'목재': 6, '거미줄': 10})
        self.assertEqual(cli.sequence, ['queue', 'queue', 'gather', 'collect'])

    def test_other_recipe_occupies_facility_slot(self):
        class QueueCLI(FakeCLI):
            def __init__(self):
                super().__init__()
                self.items['통나무'] = 100
                self.works = [dict(DisplayName='다른 목재', FacilityName='목재 가공 시설',
                                   State='InProgress', IsCompleted=False, RemainingSeconds=1)] * 3
                self.first_completed = None

            def call(self, cmd, body=None):
                if cmd == 'get_altering_works':
                    return {'works': list(self.works)}
                if cmd == 'execute_altering':
                    assert len(self.works) < 7
                    self.items['통나무'] -= 10
                    self.works.append(dict(DisplayName='목재', FacilityName='목재 가공 시설',
                                           State='InProgress', IsCompleted=False, RemainingSeconds=1))
                    self.actions.append(cmd)
                    return {'result': 'started'}
                return super().call(cmd, body)

        cli = QueueCLI()
        engine = self.engine(cli)
        engine.load()
        engine.recipes = {'목재': [('alter', '목재')]}
        for _ in range(4):
            engine.act('execute_altering', '목재')
        self.assertEqual(len(cli.works), 7)
        from production import Scheduler
        scheduler = Scheduler(engine, {'목재': 24})
        scheduler.snapshot()
        same = scheduler.groups['목재 가공 시설']
        self.assertEqual((sum(w['DisplayName'] == '목재' for w in same), len(same)), (4, 7))

    def test_quantity_monitor_stops_before_100(self):
        class SlowCLI(FakeCLI):
            def __init__(self):
                super().__init__()
                self.stopped = threading.Event()

            def call(self, cmd, body=None):
                if cmd == 'execute_gathering':
                    self.actions.append(cmd)
                    self.items[body['displayName']] = 10
                    if not self.stopped.wait(2):
                        raise AssertionError('monitor did not stop gathering')
                    return {'result': 'stopped', 'gained': 10}
                if cmd == 'stop_action':
                    self.actions.append(cmd)
                    self.stopped.set()
                    return {}
                return super().call(cmd, body)
        cli = SlowCLI()
        e = self.engine(cli)
        e.poll_seconds = 0.01
        e.run({'통나무': 10})
        self.assertEqual(cli.items['통나무'], 10)
        self.assertEqual(cli.actions, ['execute_gathering', 'stop_action'])

    def test_blocked_resume_keeps_progress_and_budget(self):
        class BlockCLI(FakeCLI):
            def call(self, cmd, body=None):
                if cmd == 'execute_gathering' and not self.items:
                    self.items['통나무'] = 8
                    raise CLIError(cmd, {'error': 'blocked', 'kind': 'unknown_modal'})
                return super().call(cmd, body)
        cli = BlockCLI()
        seen = []
        e = Engine(cli, lambda _: None, threading.Event(), recovery=lambda data, stop: seen.append(data), known_recipes={})
        e.run({'통나무': 10})
        self.assertEqual(len(seen), 1)
        self.assertEqual(e.calls, 2)
        self.assertEqual(cli.items['통나무'], 108)

    def test_non_blocked_error_not_retried(self):
        class BrokenCLI(FakeCLI):
            def call(self, cmd, body=None):
                if cmd == 'execute_gathering':
                    raise CLIError(cmd, {'error': 'tool_broken'})
                return super().call(cmd, body)
        e = Engine(BrokenCLI(), lambda _: None, threading.Event(), recovery=lambda *_: self.fail('unexpected recovery'), known_recipes={})
        with self.assertRaises(CLIError):
            e.run({'통나무': 10})

    def engine(self, cli, budget=100):
        return Engine(cli, lambda _: None, threading.Event(), budget, known_recipes={})

    def test_inputs(self):
        self.assertEqual(parse_targets('목재 30, 실크 10'), {'목재': 30, '실크': 10})
        self.assertEqual(parse_targets('목재,30,실크,10'), {'목재': 30, '실크': 10})
        with self.assertRaises(Halt):
            parse_targets('목재 0')

    def test_recursive_gather_process_collect(self):
        cli = FakeCLI()
        self.engine(cli).run({'목재': 5})
        self.assertEqual(cli.items['목재'], 6)
        self.assertEqual(cli.actions.count('execute_gathering'), 1)
        self.assertEqual(cli.actions.count('complete_altering_work'), 1)

    def test_existing_stock(self):
        cli = FakeCLI()
        cli.items['목재'] = 10
        self.engine(cli).run({'목재': 5})
        self.assertEqual(cli.actions, [])

    def test_raw_gather_1000(self):
        cli = FakeCLI()
        self.engine(cli).run(parse_targets('최상급 거미줄 1000개'))
        self.assertEqual(cli.items['최상급 거미줄'], 1000)
        self.assertEqual(cli.actions, ['execute_gathering'] * 10)

    def test_raw_existing_and_mixed_targets(self):
        cli = FakeCLI()
        cli.items['최상급 거미줄'] = 850
        self.engine(cli).run(parse_targets('최상급 거미줄,1000개,목재,3개'))
        self.assertEqual(cli.items['최상급 거미줄'], 1050)
        self.assertEqual(cli.items['목재'], 3)
        self.assertEqual(cli.actions.count('execute_gathering'), 3)

    def test_gather_route_wins_over_ambiguous_recipe(self):
        cli = FakeCLI()
        engine = self.engine(cli)
        engine.recipes['최상급 거미줄'] = [('craft', '최상급 거미줄')] * 2
        engine.run({'최상급 거미줄': 100})
        self.assertEqual(cli.actions, ['execute_gathering'])

    def test_budget(self):
        cli = FakeCLI()
        with self.assertRaises(Halt):
            self.engine(cli, 5).run({'목재': 5})
        self.assertEqual(cli.actions, ['execute_gathering'])

    def test_pending_collect_before_enqueue(self):
        cli = FakeCLI()
        cli.works = [dict(DisplayName='목재', FacilityName='목재 가공', IsCompleted=True)]
        self.engine(cli).run({'목재': 3})
        self.assertEqual(cli.actions, ['complete_altering_work'])

    def test_cycle(self):
        cli = FakeCLI()
        engine = self.engine(cli)
        with self.assertRaises(Halt):
            engine.ensure('목재', 3, ('목재',))

    def test_cancel(self):
        engine = self.engine(FakeCLI())
        engine.stop.set()
        with self.assertRaises(Halt):
            engine.run({'목재': 3})

    def test_default_recipe_and_explicit_override(self):
        engine = self.engine(FakeCLI())
        engine.recipes = {'철괴(광석)': [('alter', '철괴(광석)')],
                          '철괴(철 광석)': [('alter', '철괴(철 광석)')]}
        engine.defaults = {'철괴': ['alter', '철괴(철 광석)']}
        self.assertEqual(engine.resolve('철괴'), ('alter', '철괴(철 광석)'))
        self.assertEqual(engine.resolve('철괴(광석)'), ('alter', '철괴(광석)'))
        engine.defaults = {'철괴': ['alter', '삭제된 경로']}
        with self.assertRaises(Halt):
            engine.resolve('철괴')

    def test_identical_names_cannot_be_selected(self):
        engine = self.engine(FakeCLI())
        engine.recipes = {'철괴': [('alter', '철괴')] * 2}
        engine.defaults = {'철괴': ['alter', '철괴']}
        self.assertEqual(engine.resolve('철괴'), ('alter', '철괴'))


if __name__ == '__main__':
    unittest.main()
