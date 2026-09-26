import json
from app import ROOT

ORDER = ['금속 가공 시설', '목재 가공 시설', '가죽 가공 시설', '옷감 가공 시설',
         '약품 가공 시설', '식재료 가공 시설', '무기 제작대', '방어구 제작대',
         '약품 제작대', '음식 제작대', '다목적 제작대', '데코 제작대']
try:
    DATA = json.loads((ROOT / 'facilities.json').read_text(encoding='utf-8'))
except (OSError, ValueError):
    DATA = {'items': {}, 'metal_order': []}


def facility_for(kind, name):
    if kind == 'gather':
        return '채집'
    return DATA['items'].get(kind + ':' + name, {}).get('facility', '미분류')


def ordered_names(kind, names):
    # Preserve live CLI order within a facility, except verified screenshot order.
    indices = {n: i for i, n in enumerate(names)}
    metal = DATA.get('metal_order', [])
    def key(name):
        facility = facility_for(kind, name)
        rank = ORDER.index(facility) if facility in ORDER else len(ORDER)
        position = metal.index(name) if facility == '금속 가공 시설' and name in metal else indices[name] + len(metal)
        return rank, position
    return sorted(names, key=key)
