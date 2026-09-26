import unittest
from facilities import facility_for, ordered_names


class Facilities(unittest.TestCase):
    def test_known_and_unknown(self):
        self.assertEqual(facility_for('alter', '철괴(철 광석)'), '금속 가공 시설')
        self.assertEqual(facility_for('craft', '숏소드'), '무기 제작대')
        self.assertEqual(facility_for('craft', '확인 안 된 아이템'), '미분류')

    def test_screenshot_order(self):
        names = ['타르', '강철괴', '철괴(철 광석)', '철괴(광석)']
        self.assertEqual(ordered_names('alter', names), ['철괴(광석)', '철괴(철 광석)', '강철괴', '타르'])
