import json
import unittest
from pathlib import Path

from build_recipes import canonical_ingredient


class RecipeData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads((Path(__file__).parent / 'recipes.json').read_text(encoding='utf-8'))['recipes']

    def test_no_recipe_requires_itself(self):
        bad = [(key, key.split(':', 1)[1]) for key, row in self.data.items()
               if key.split(':', 1)[1] in row.get('ingredients', {})]
        self.assertEqual(bad, [])

    def test_known_public_typos_are_canonicalized(self):
        self.assertEqual(canonical_ingredient('부드러운 통나무+', 30, '부드러운 목재',
                                              {'부드러운 목재'}, []), '부드러운 통나무')
        self.assertEqual(canonical_ingredient('상금 마법 유탄 부품', 3, '상급 화염 마법 유탄',
                                              {'상급 마법 유탄 부품'}, []), '상급 마법 유탄 부품')

    def test_repaired_records(self):
        self.assertEqual(self.data['alter:최상급 목재+']['ingredients']['최상급 목재'], 5)
        self.assertNotIn('최상급 목재+', self.data['alter:최상급 목재+']['ingredients'])
        self.assertEqual(self.data['alter:부드러운 목재']['ingredients']['부드러운 통나무'], 30)
        self.assertEqual(self.data['alter:단단한 목재']['ingredients']['단단한 통나무'], 30)
        self.assertEqual(self.data['craft:상급 화염 마법 유탄']['ingredients']['상급 마법 유탄 부품'], 3)


if __name__ == '__main__':
    unittest.main()
