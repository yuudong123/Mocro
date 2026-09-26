import threading
import unittest
from app import Engine


class MetalCLI:
    def __init__(self, iron=0):
        self.stock = {'철괴': iron}
        self.works = []
        self.produced = []
        self.definitions = {'철괴': ('철 광석', 10), '강철괴': ('철괴', 3)}

    def call(self, cmd, body=None):
        if cmd == 'get_inventory':
            return {'CurrentInventoryWeight': 0, 'MaxInventoryWeight': 100}
        if cmd == 'capabilities':
            return {}
        if cmd == 'get_items':
            return [dict(DisplayName=n, Count=c, Location='inventory') for n, c in self.stock.items()]
        if cmd == 'get_gatherable_items':
            return {'items': [dict(DisplayName='철 광석', ToolOk=True)]}
        if cmd == 'get_craftable_items':
            return {'items': []}
        if cmd == 'get_alterable_items':
            rows = []
            for name, (material, required) in self.definitions.items():
                if body and body not in name and body not in material:
                    continue
                owned = self.stock.get(material, 0)
                row = dict(DisplayName=name, Alterable=owned >= required, ProducedPerWork=3)
                if owned < required:
                    row.update(Reason='not_enough_ingredient', MissingIngredients=[dict(DisplayName=material, Required=required, Owned=owned)])
                rows.append(row)
            return {'items': rows}
        if cmd == 'get_altering_works':
            return {'works': self.works}
        if cmd == 'execute_gathering':
            self.stock['철 광석'] = self.stock.get('철 광석', 0) + 100
            return {'result': 'completed'}
        if cmd == 'execute_altering':
            name = body['displayName']
            material, required = self.definitions[name]
            assert self.stock.get(material, 0) >= required
            self.stock[material] -= required
            self.works.append(dict(DisplayName=name, FacilityName='금속 가공 시설', IsCompleted=True))
            self.produced.append(name)
            return {'result': 'started'}
        if cmd == 'complete_altering_work':
            for work in self.works:
                name = work['DisplayName']
                self.stock[name] = self.stock.get(name, 0) + 3
            count = len(self.works)
            self.works = []
            return {'collected': count}
        raise AssertionError(cmd)


class Dependencies(unittest.TestCase):
    def test_static_recipe_plan_counts_shared_final_stock(self):
        cli = MetalCLI()
        engine = Engine(cli, lambda _: None, threading.Event(), known_recipes={
            'alter:철괴': {'yield': 3, 'ingredients': {'철 광석': 10}},
            'alter:강철괴': {'yield': 3, 'ingredients': {'철괴': 3, '석탄': 4}},
        })
        engine.load()
        self.assertEqual(engine.plan_background_gathering({'철괴': 100, '강철괴': 100}),
                         {'철 광석': 680})

    def test_final_reserves_in_both_input_orders(self):
        for targets in ({'철괴': 100, '강철괴': 100}, {'강철괴': 100, '철괴': 100}):
            cli = MetalCLI()
            engine = Engine(cli, lambda _: None, threading.Event(), budget=2000, known_recipes={})
            engine.run(targets)
            self.assertGreaterEqual(cli.stock['철괴'], 100)
            self.assertGreaterEqual(cli.stock['강철괴'], 100)
            # 202 iron needed => 68 batches produce 204, then 102 consumed.
            self.assertEqual(cli.produced.count('철괴'), 68)
            self.assertEqual(cli.produced.count('강철괴'), 34)

    def test_existing_iron_not_subtracted_per_batch(self):
        cli = MetalCLI(2)
        engine = Engine(cli, lambda _: None, threading.Event(), budget=2000, known_recipes={})
        engine.run({'철괴': 100, '강철괴': 100})
        self.assertEqual(cli.produced.count('철괴'), 67)
        self.assertGreaterEqual(cli.stock['철괴'], 100)


if __name__ == '__main__':
    unittest.main()
