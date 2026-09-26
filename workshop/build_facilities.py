"""Refresh factual item/facility mappings from public recipe tables.
Requires beautifulsoup4; normal app use needs no network or parser dependency.
"""
import concurrent.futures
import json
import re
import urllib.request
from datetime import date
from bs4 import BeautifulSoup
from app import ROOT, CLI, DEFAULT_CLI

PAGES = list(range(124, 136)) + [466]


def normalize(name):
    return re.sub(r'\s+', '', re.sub(r'\s*[x×]\s*\d+\s*$', '', name)).casefold()


def parse_page(number):
    url = f'https://mabimo.inven.co.kr/dataninfo/craft/?boardidx={number}&comeidx=6366'
    html = urllib.request.urlopen(url, timeout=30).read().decode('utf-8')
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for table in soup.select('table')[1:]:
        spans = {}
        for tr in table.select('tr'):
            row = {}
            for col, (remaining, text) in list(spans.items()):
                row[col] = text
                if remaining <= 1:
                    del spans[col]
                else:
                    spans[col] = (remaining - 1, text)
            col = 0
            for td in tr.find_all(['td', 'th'], recursive=False):
                while col in row:
                    col += 1
                text = td.get_text(' ', strip=True)
                for _ in range(int(td.get('colspan', 1))):
                    row[col] = text
                    if int(td.get('rowspan', 1)) > 1:
                        spans[col] = (int(td['rowspan']) - 1, text)
                    col += 1
            for col, text in row.items():
                match = re.search(r'(금속 가공 시설|목재 가공 시설|가죽 가공 시설|옷감 가공 시설|약품 가공 시설|식재료 가공 시설|무기 제작대|방어구 제작대|약품 제작대|음식 제작대|다목적 제작대|데코 제작대)', text)
                if match and col > 0:
                    records.append((normalize(row[col - 1]), match[1], url))
                    break
    return records


if __name__ == '__main__':
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        pages = list(pool.map(parse_page, PAGES))
    records = {}
    for page in pages:
        for name, facility, url in page:
            records.setdefault(name, set()).add((facility, url))
    cli = CLI(DEFAULT_CLI)
    result = {}
    counts = {}
    for kind, command in [('alter', 'get_alterable_items'), ('craft', 'get_craftable_items')]:
        rows = cli.call(command)['items']
        mapped = 0
        for row in rows:
            name = row['DisplayName']
            hits = records.get(normalize(name), set())
            # Only these ore alternatives have externally verified common output.
            if name in ('철괴(광석)', '철괴(철 광석)'):
                hits = records.get(normalize('철괴'), set())
            facilities = {v[0] for v in hits}
            if len(facilities) == 1:
                result[kind + ':' + name] = {'facility': next(iter(facilities)), 'sources': sorted({v[1] for v in hits})}
                mapped += 1
        counts[kind] = {'rows': len(rows), 'mapped': mapped}
    # User-supplied in-game metal facility screenshot is newer than the tables.
    metal = ['철괴(광석)', '철괴(철 광석)', '강철괴', '합금강괴', '타르', '특수강괴', '은합금괴', '운철괴', '백금강괴']
    for name in metal:
        result['alter:' + name] = {'facility': '금속 가공 시설', 'sources': ['user screenshot: metal processing Lv.6']}
    # Official update names these as 데코 트로피(동) 상자, etc.; CLI puts
    # the variant suffix after 상자. Keep these explicit, reviewed aliases.
    for grade in ('동', '은', '금'):
        result[f'craft:데코 트로피 상자({grade})'] = {
            'facility': '데코 제작대',
            'sources': ['https://mabinogimobile.nexon.com/News/Update/3521707']}
    payload = {'checked': str(date.today()), 'counts': counts, 'items': result, 'metal_order': metal}
    (ROOT / 'facilities.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(counts))
    print('mapped unique', len(result))
