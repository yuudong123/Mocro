"""Build a reviewed, offline recipe snapshot from public Inven tables."""
import concurrent.futures
import json
import re
import urllib.request
from datetime import date

from bs4 import BeautifulSoup

from app import CLI, DEFAULT_CLI, ROOT
from build_facilities import PAGES, normalize

COUNT = re.compile(r'^(.+?)\s*[x×]\s*(\d+)$')

KNOWN_NAME_FIXES = {
    '마력이 깃든 돌': '마력 깃든 돌',
    '똑닥 반딧불이': '똑딱 반딧불이',
    '상금 마법 유탄 부품': '상급 마법 유탄 부품',
    '부드러운 통나무+': '부드러운 통나무',
    '단단한 통나무+': '단단한 통나무',
}

SUPPLEMENTAL_RECIPES = {
    'craft:뛰어난 회복 물약S': {
        'output': '뛰어난 회복 물약S', 'yield': 5,
        'ingredients': {'블러디 허브': 20, '솔솔 버섯 진액': 5, '생명의 마나석': 10},
        'source': 'https://mobile.mabi.zip/ttwieonanhoebokmulyaks',
    },
    'craft:뛰어난 자동회복 물약S': {
        'output': '뛰어난 자동회복 물약S', 'yield': 5,
        'ingredients': {'뛰어난 회복 물약S': 5, '분해된 장비 부품': 180},
        'source': 'https://mobile.mabi.zip/ttwieonanjadonghoebokmulyaks',
    },
}


def canonical_ingredient(name, units, output, known_names, missing):
    """Correct common public-table typos without merging real variants."""
    if name == output:
        same_units = [m['DisplayName'] for m in missing
                      if m['DisplayName'] != output and int(m.get('Required', 0)) == units]
        if same_units:
            return same_units[0]
    fixed = KNOWN_NAME_FIXES.get(name, name)
    if fixed in known_names:
        return fixed
    if name.endswith('+') and name[:-1] in known_names:
        return name[:-1]
    return fixed


def parse_page(number):
    url = f'https://mabimo.inven.co.kr/dataninfo/craft/?boardidx={number}&comeidx=6366'
    soup = BeautifulSoup(urllib.request.urlopen(url, timeout=30).read().decode('utf-8'), 'html.parser')
    records = []
    for table in soup.select('table')[1:]:
        for tr in table.select('tr'):
            cells = tr.find_all(['td', 'th'], recursive=False)
            if len(cells) not in (3, 4):
                continue
            output_cell = cells[0] if number < 130 else cells[-3]
            output_text = output_cell.get_text(' ', strip=True)
            output = COUNT.fullmatch(output_text)
            if number < 130 and not output:
                continue
            if number >= 130 and (not output_text or output_text in ('무기', '방어구', '장신구', '도구', '음식')):
                continue
            parts = [p.strip() for p in cells[-1].stripped_strings if p.strip()]
            ingredients = {}
            for part in parts:
                for segment in re.split(r'\s*,\s*', part):
                    match = COUNT.fullmatch(segment)
                    if not match:
                        ingredients = {}
                        break
                    ingredients[match[1].strip()] = int(match[2])
                if not ingredients:
                    break
            if ingredients:
                records.append({'output': output[1].strip() if output else output_text,
                                'yield': int(output[2]) if output else 1,
                                'ingredients': ingredients, 'source': url})
    return records


def build():
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        pages = list(pool.map(parse_page, PAGES))
    public = {}
    for page in pages:
        for record in page:
            public.setdefault(normalize(record['output']), []).append(record)
    cli = CLI(DEFAULT_CLI)
    live_rows = {}
    all_known_names = set()
    recipes = {}
    stats = {}
    for kind, command in [('alter', 'get_alterable_items'), ('craft', 'get_craftable_items')]:
        rows = cli.call(command)['items']
        for row in rows:
            live_rows[(kind, row['DisplayName'])] = row
            all_known_names.add(row['DisplayName'])
        mapped = 0
        for row in rows:
            name = row['DisplayName']
            hits = public.get(normalize(name), [])
            if name in ('철괴(광석)', '철괴(철 광석)'):
                ore = '광석' if name == '철괴(광석)' else '철 광석'
                hits = [h for h in public.get(normalize('철괴'), []) if ore in h['ingredients']]
            elif name == '강철괴':
                hits = [h for h in public.get(normalize('철괴'), []) if '철괴' in h['ingredients'] and '석탄' in h['ingredients']]
            unique = {(h['yield'], tuple(sorted(h['ingredients'].items()))) for h in hits}
            if len(unique) != 1:
                continue
            selected = hits[0]
            cli_yield = row.get('ProducedPerWork' if kind == 'alter' else 'ProducedPerCraft')
            if cli_yield is not None and int(cli_yield) != selected['yield']:
                continue
            ingredients = {}
            for ingredient, units in selected['ingredients'].items():
                fixed = canonical_ingredient(ingredient, units, name, all_known_names,
                                             row.get('MissingIngredients', []))
                ingredients[fixed] = ingredients.get(fixed, 0) + units
            record = dict(selected)
            record['output'] = name
            record['ingredients'] = ingredients
            recipes[f'{kind}:{name}'] = record
            mapped += 1
        stats[kind] = {'cli': len(rows), 'mapped': mapped}
    for key, record in SUPPLEMENTAL_RECIPES.items():
        kind, name = key.split(':', 1)
        row = live_rows.get((kind, name))
        if row and int(row.get('ProducedPerCraft', 0)) == record['yield']:
            recipes.setdefault(key, record)
    payload = {'checked': str(date.today()), 'stats': stats, 'recipes': recipes}
    (ROOT / 'recipes.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(stats))


if __name__ == '__main__':
    build()
