"""Whole-order material planning and cooperative facility scheduling.

The planner is pure. The scheduler performs exactly one game action at a time
and replans from live inventory and queues after every action.
"""
from collections import deque
from dataclasses import dataclass, field
import difflib
import time


ORE_ROUTES = {'철괴(광석)': '철괴', '철괴(철 광석)': '철괴'}


def product_name(name):
    # Parentheses may encode a real colour/grade; only verified alternatives
    # may share a stock balance.
    return ORE_ROUTES.get(name, name)


@dataclass
class Recipe:
    kind: str
    exact: str
    output: str
    produced: int
    ingredients: dict
    complete: bool
    facility: str
    possible: bool
    reason: str = ''


@dataclass
class Plan:
    required: dict = field(default_factory=dict)
    jobs: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    recipes: dict = field(default_factory=dict)
    blocked: dict = field(default_factory=dict)


def build_plan(goals, bag, storage, pending, gatherable, recipe_for):
    """Aggregate shared demand; propagate only increments in required works.

    Queued inputs have already been consumed: only their output is credited.
    Storage may cover consumption, but cannot satisfy final bag holdings.
    """
    plan = Plan(required=dict(goals))
    todo = deque(goals)
    edges = {}

    def reaches(start, target, seen=None):
        if start == target:
            return True
        seen = set() if seen is None else seen
        if start in seen:
            return False
        seen.add(start)
        return any(reaches(n, target, seen) for n in edges.get(start, ()))

    while todo:
        name = todo.popleft()
        required = plan.required[name]
        usable_storage = min(storage.get(name, 0), max(0, required - goals.get(name, 0)))
        deficit = max(0, required - bag.get(name, 0) - usable_storage - pending.get(name, 0))
        if not deficit:
            continue
        if name in gatherable:
            plan.raw[name] = deficit
            continue
        recipe = plan.recipes.get(name)
        if recipe is None:
            recipe = recipe_for(name)
            if recipe is None:
                plan.blocked[name] = '구매·교환 또는 확보 경로 확인 필요'
                continue
            plan.recipes[name] = recipe
        if recipe.reason and recipe.reason != 'not_enough_ingredient' and not recipe.possible:
            plan.blocked[name] = recipe.reason
        count = (deficit + recipe.produced - 1) // recipe.produced
        delta = count - plan.jobs.get(name, 0)
        if delta <= 0:
            continue
        plan.jobs[name] = count
        for ingredient, units in recipe.ingredients.items():
            if reaches(ingredient, name):
                raise ValueError(f'재료 순환 참조: {name} → {ingredient}')
            edges.setdefault(name, set()).add(ingredient)
            plan.required[ingredient] = plan.required.get(ingredient, 0) + units * delta
            todo.append(ingredient)
    return plan


class Scheduler:
    def __init__(self, engine, targets):
        from app import Halt
        self.engine = engine
        self.Halt = Halt
        self.goals = {}
        self.routes = {}
        for name, count in targets.items():
            output = product_name(name)
            self.goals[output] = max(self.goals.get(output, 0), count)
            if name in ORE_ROUTES:
                if output in self.routes and self.routes[output] != name:
                    raise Halt(f'{output}: 서로 다른 원료 경로를 동시에 지정했습니다.')
                self.routes[output] = name
        self.learned = {}
        self.warned = set()
        self.craft_limits = {}
        self.facility_aliases = {}
        self.recipe_facilities = {}
        self.untracked_outputs = set()
        self.current_facility = None
        self.no_progress = 0
        self.last_wait = None
        self.last_change = time.monotonic()

    def log(self, level, event, **fields):
        from app import log_event
        log_event(level, event, run_id=getattr(self.engine, 'run_id', None), **fields)

    def warn_once(self, key, message):
        if key not in self.warned:
            self.warned.add(key)
            self.engine.emit(message)
            self.log('WARNING', 'scheduler.warning', key=repr(key), message=message)

    @staticmethod
    def merge_recipe_rows(rows, kind, exact, log=None):
        """Collapse duplicate live rows while retaining the most conservative data.

        The CLI can return the same exact output more than once when it is exposed
        through multiple categories. Availability fields are dynamic, so they must
        not make an otherwise identical recipe look like two different routes.
        Truly different yields remain ambiguous and are rejected.
        """
        if not rows:
            return None
        yield_key = 'ProducedPerWork' if kind == 'alter' else 'ProducedPerCraft'
        groups = {}
        for row in rows:
            shape = (row.get('DisplayName'), row.get(yield_key))
            groups.setdefault(shape, []).append(row)
        if len(groups) > 1:
            if log:
                log('ERROR', 'recipe.candidates_incompatible', exact=exact, kind=kind,
                    candidates=rows, shapes=[list(shape) for shape in groups])
            raise ValueError(f'{exact}: 레시피 조회 결과가 모호합니다.')
        candidates = next(iter(groups.values()))
        merged = dict(candidates[0])
        missing = {}
        for row in candidates:
            for item in row.get('MissingIngredients', []) or []:
                name = item.get('DisplayName')
                if not name:
                    continue
                current = missing.get(name)
                required = int(item.get('Required', 0))
                if current is None or required > current['Required']:
                    missing[name] = dict(item, Required=required)
                elif required == current['Required']:
                    # Keep the least favorable observed ownership for diagnosis.
                    current['Owned'] = min(int(current.get('Owned', 0)),
                                           int(item.get('Owned', 0)))
        if missing:
            merged['MissingIngredients'] = list(missing.values())
        else:
            merged.pop('MissingIngredients', None)
        availability_key = 'Alterable' if kind == 'alter' else 'Craftable'
        merged[availability_key] = all(bool(row.get(availability_key)) for row in candidates)
        reasons = [row.get('Reason') for row in candidates if row.get('Reason')]
        if reasons:
            merged['Reason'] = reasons[0]
        else:
            merged.pop('Reason', None)
        if log and len(candidates) > 1:
            log('WARNING', 'recipe.candidates_merged', exact=exact, kind=kind,
                candidates=candidates, merged=merged,
                reason='same output/yield; dynamic availability fields differed')
        return merged

    @staticmethod
    def reconcile_ingredients(exact, ingredients, missing):
        """Let the live CLI correct stale public-table ingredient names.

        MissingIngredients is a partial list, so keep verified entries that
        are not mentioned. Replace only a same-quantity close variant, a
        self-reference, or a known spelling variant.
        """
        result = dict(ingredients)
        for item in missing:
            live_name = item['DisplayName']
            required = int(item['Required'])
            if result.get(live_name) == required:
                continue
            candidates = []
            for old_name, old_units in result.items():
                if old_units != required or old_name == live_name:
                    continue
                old_base, live_base = old_name.rstrip('+'), live_name.rstrip('+')
                similarity = difflib.SequenceMatcher(None, old_base, live_base).ratio()
                if old_name == exact or old_base == live_base or similarity >= 0.82:
                    candidates.append((old_name, similarity))
            if candidates:
                old_name = max(candidates, key=lambda pair: pair[1])[0]
                del result[old_name]
                result[live_name] = required
            else:
                result[live_name] = required
        return result

    def recipe_for(self, output):
        from facilities import facility_for
        engine = self.engine
        route = engine.resolve(self.routes.get(output, output))
        if not route:
            self.log('WARNING', 'recipe.not_found', output=output)
            return None
        kind, exact = route
        if product_name(exact) != output:
            raise self.Halt(f'{output}: 색상·등급이 포함된 정확한 품목명으로 지정하세요: {exact}')
        key = (kind, exact)
        if key in self.recipe_cache:
            return self.recipe_cache[key]
        command = 'get_alterable_items' if kind == 'alter' else 'get_craftable_items'
        rows = [r for r in engine.read(command, exact).get('items', []) if r['DisplayName'] == exact]
        fingerprints = []
        unique_rows = []
        seen = set()
        for candidate in rows:
            fingerprint = repr(sorted(candidate.items()))
            fingerprints.append(fingerprint)
            if fingerprint not in seen:
                seen.add(fingerprint)
                unique_rows.append(candidate)
        self.log('DEBUG', 'recipe.query_candidates', output=output, exact=exact,
                 kind=kind, command=command, rows=rows, fingerprints=fingerprints,
                 unique_count=len(unique_rows))
        try:
            row = self.merge_recipe_rows(unique_rows, kind, exact, log=self.log)
        except ValueError as error:
            self.log('ERROR', 'recipe.ambiguous', output=output, exact=exact,
                     kind=kind, rows=rows, unique_rows=unique_rows,
                     fingerprints=fingerprints, error=str(error))
            raise self.Halt(str(error)) from error
        if row is None:
            self.log('ERROR', 'recipe.empty_candidates', output=output, exact=exact,
                     kind=kind, rows=rows)
            raise self.Halt(f'{exact}: 레시피 조회 결과가 없습니다.')
        engine.catalog[key] = row
        produced = int(row.get('ProducedPerWork' if kind == 'alter' else 'ProducedPerCraft', 1))
        if produced <= 0:
            raise self.Halt(f'{exact}: 생산 수량이 올바르지 않습니다.')
        saved = engine.known_recipes.get(f'{kind}:{exact}')
        ingredients = dict(saved.get('ingredients', {})) if saved else {}
        original_ingredients = dict(ingredients)
        ingredients.update(self.learned.get(key, {}))
        complete = bool(saved)
        mismatch = bool(saved and int(saved['yield']) != produced)
        live_missing = row.get('MissingIngredients', [])
        ingredients = self.reconcile_ingredients(exact, ingredients, live_missing)
        if ingredients != original_ingredients:
            mismatch = True
        for item in live_missing:
            name, units = item['DisplayName'], int(item['Required'])
            if saved and ingredients.get(name) != units:
                mismatch = True
            ingredients[name] = units
        if any(not isinstance(v, int) or v <= 0 for v in ingredients.values()):
            raise self.Halt(f'{exact}: 재료 수량이 올바르지 않습니다.')
        self.learned[key] = ingredients
        if mismatch:
            self.warned.add(('partial', key))
        if mismatch or ('partial', key) in self.warned:
            complete = False
            self.warn_once(('changed', key), f'{exact}: 공개 표와 현재 수량 차이 · 게임에서 확인한 수량 우선 적용')
        if not saved:
            self.warn_once(('missing', key), f'{exact}: 전체 레시피 미등록 · 확인되는 부족 재료를 계획에 추가합니다.')
        facility = facility_for(kind, exact)
        if facility in ('무기 제작대', '방어구 제작대'):
            # Equipment is deliberately absent from get_items.  Treat its goal
            # as the amount to make in this run and count only acknowledged
            # execute_crafting completions.
            self.untracked_outputs.add(output)
            self.warn_once(('untracked', output),
                           f'{output}: 장비 보유량은 조회할 수 없어 이번 실행의 신규 제작 수량으로 진행합니다.')
        if kind == 'alter':
            live = next((w.get('FacilityName') for w in self.works
                         if w.get('DisplayName') == exact and w.get('FacilityName')), None)
            facility = live or self.recipe_facilities.get(exact) or self.facility_aliases.get(facility, facility)
        if facility == '미분류':
            facility = f'미확인 시설:{exact}'
        recipe = Recipe(kind, exact, output, produced, ingredients, complete, facility,
                        bool(row.get('Alterable' if kind == 'alter' else 'Craftable')),
                        row.get('Reason', ''))
        self.log('INFO', 'recipe.resolved', output=output, exact=exact, kind=kind,
                 row=row, ingredients=ingredients, produced=produced,
                 facility=facility, complete=complete, possible=recipe.possible,
                 mismatch=mismatch)
        self.recipe_cache[key] = recipe
        return recipe

    def work_facility(self, work):
        from facilities import facility_for
        mapped = facility_for('alter', work['DisplayName'])
        facility = (work.get('FacilityName') or self.recipe_facilities.get(work['DisplayName'])
                    or self.facility_aliases.get(mapped, mapped))
        return facility if facility != '미분류' else f'미확인 시설:{work["DisplayName"]}'

    def snapshot(self):
        from facilities import facility_for
        bag, storage = {}, {}
        rows = self.engine.read('get_items')
        if not isinstance(rows, list):
            raise self.Halt(f'보유 아이템 조회 실패: {rows}')
        for row in rows:
            if row.get('IsLocked'):
                continue
            destination = bag if row.get('Location') == 'inventory' else storage
            if row.get('Location') not in ('inventory', 'account_storage', 'character_storage'):
                continue
            name = row['DisplayName']
            destination[name] = destination.get(name, 0) + int(row['Count'])
        self.works = [dict(w) for w in self.engine.read('get_altering_works').get('works', [])]
        for work in self.works:
            if work.get('FacilityName'):
                exact = work['DisplayName']
                self.recipe_facilities[exact] = work['FacilityName']
                if self.current_facility == f'미확인 시설:{exact}':
                    self.current_facility = work['FacilityName']
            mapped = facility_for('alter', work['DisplayName'])
            if mapped != '미분류' and work.get('FacilityName'):
                self.facility_aliases[mapped] = work['FacilityName']
        self.current_facility = self.facility_aliases.get(self.current_facility, self.current_facility)
        pending = {}
        self.groups = {}
        for work in self.works:
            self.groups.setdefault(self.work_facility(work), []).append(work)
            exact = work['DisplayName']
            row = self.engine.catalog.get(('alter', exact))
            if row:
                output = product_name(exact)
                pending[output] = pending.get(output, 0) + int(row.get('ProducedPerWork', 1))
        self.recipe_cache = {}
        for name in self.untracked_outputs:
            bag[name] = self.engine.untracked_produced.get(name, 0)
        self.bag, self.storage, self.pending = bag, storage, pending
        self.log('DEBUG', 'scheduler.snapshot', bag=bag, storage=storage,
                 works=self.works, pending=pending, groups={k: len(v) for k, v in self.groups.items()})

    def make_plan(self):
        try:
            plan = build_plan(self.goals, self.bag, self.storage, self.pending,
                              self.engine.gather, self.recipe_for)
            self.log('INFO', 'scheduler.plan', goals=self.goals, bag=self.bag,
                     storage=self.storage, pending=self.pending, required=plan.required,
                     jobs=plan.jobs, raw=plan.raw, blocked=plan.blocked,
                     recipes={name: {'kind': recipe.kind, 'exact': recipe.exact,
                                     'output': recipe.output, 'produced': recipe.produced,
                                     'ingredients': recipe.ingredients, 'facility': recipe.facility,
                                     'possible': recipe.possible, 'complete': recipe.complete}
                              for name, recipe in plan.recipes.items()})
            return plan
        except ValueError as error:
            self.log('ERROR', 'scheduler.plan_error', goals=self.goals,
                     bag=self.bag, storage=self.storage, pending=self.pending,
                     error=str(error))
            raise self.Halt(str(error)) from error

    def relevant_facilities(self, plan):
        relevant_outputs = {n for n, required in plan.required.items()
                            if required > self.bag.get(n, 0) + min(self.storage.get(n, 0),
                                max(0, required - self.goals.get(n, 0)))}
        return {self.work_facility(w) for w in self.works
                if product_name(w['DisplayName']) in relevant_outputs} | {
                    r.facility for r in plan.recipes.values() if r.kind == 'alter'}

    def completion_probe(self, facilities):
        """Only interrupt for facilities relevant to this order, not old jobs."""
        works = self.engine.read('get_altering_works').get('works', [])
        groups = {}
        for work in works:
            groups.setdefault(self.work_facility(work), []).append(work)
        return any(f in groups and all(w.get('IsCompleted') for w in groups[f]) for f in facilities)

    def affordable(self, recipe):
        if not recipe.complete or not recipe.ingredients:
            return 1 if recipe.possible else 0
        count = min((self.bag.get(n, 0) + self.storage.get(n, 0)) // units
                    for n, units in recipe.ingredients.items())
        # The game may make additional materials accessible via transfer.
        return max(count, 1 if recipe.possible else 0)

    def fingerprint(self):
        return (tuple(sorted(self.bag.items())), tuple(sorted(self.storage.items())),
                tuple(sorted((w['DisplayName'], self.work_facility(w), bool(w.get('IsCompleted')))
                             for w in self.works)))

    def gather_goal(self, name, deficit, plan):
        immediate = self.next_work_materials(plan)
        if name in immediate:
            return self.bag.get(name, 0) + min(deficit, immediate[name])
        # Start idle facilities with one work instead of waiting for seven.
        # Once facilities are running, gather their remaining total deficits.
        chunks = []
        for output, recipe in plan.recipes.items():
            if recipe.kind != 'alter' or name not in recipe.ingredients:
                continue
            if self.groups.get(recipe.facility):
                continue
            count = min(1, plan.jobs[output])
            required = recipe.ingredients[name] * count
            missing = max(0, required - self.bag.get(name, 0) - self.storage.get(name, 0))
            if missing:
                chunks.append(missing)
        return self.bag.get(name, 0) + min([deficit] + chunks)

    def gather_priority(self, name, plan):
        idle_consumers = sum(r.kind == 'alter' and not self.groups.get(r.facility) and
                             r.ingredients[name] * min(1, plan.jobs[r.output]) >
                             self.bag.get(name, 0) + self.storage.get(name, 0)
                             for r in plan.recipes.values() if name in r.ingredients)
        is_material = any(name in r.ingredients for r in plan.recipes.values())
        # Resin gathering also supplies soft logs; credit actual byproducts
        # before scheduling a separate soft-log trip.
        return (-idle_consumers, name == '부드러운 통나무' and '나무 진액' in plan.raw,
                not is_material)

    def next_work_materials(self, plan):
        """Choose one reachable work, including partially occupied facilities.

        Only raw-material deficits qualify: gathering raw inputs for an upper
        recipe cannot unlock it while its intermediate products are missing.
        """
        candidates = []
        for output, recipe in plan.recipes.items():
            if recipe.kind != 'alter' or not plan.jobs.get(output):
                continue
            works = self.groups.get(recipe.facility, [])
            limit = 1 if recipe.facility.startswith('미확인 시설:') else 7
            if len(works) >= limit:
                continue
            missing = {name: units - self.bag.get(name, 0) - self.storage.get(name, 0)
                       for name, units in recipe.ingredients.items()
                       if units > self.bag.get(name, 0) + self.storage.get(name, 0)}
            if not missing or any(name not in plan.raw for name in missing):
                continue
            missing = {name: min(count, self.gather_deficit(name, plan))
                       for name, count in missing.items()}
            if any(count <= 0 for count in missing.values()):
                continue
            running = any(not w.get('IsCompleted') for w in works)
            candidates.append(((running, len(missing), sum(missing.values())), missing))
        return min(candidates, key=lambda item: item[0])[1] if candidates else {}

    def next_gather(self, plan):
        deficits = {n: self.gather_deficit(n, plan) for n in plan.raw}
        deficits = {n: c for n, c in deficits.items() if c > 0}
        if not deficits:
            return None
        immediate = self.next_work_materials(plan)
        choices = immediate or deficits
        name = min(choices, key=lambda n: self.gather_priority(n, plan))
        target = self.bag.get(name, 0) + choices[name] if immediate else self.gather_goal(name, deficits[name], plan)
        return name, target

    def production_probe(self, gathering=None, target=None):
        """Called at a bounded interval while gathering; never executes actions."""
        self.snapshot()
        plan = self.make_plan()
        next_gather = self.next_gather(plan)
        if gathering is not None and next_gather is not None:
            name, revised_target = next_gather
            if name != gathering or (target is not None and revised_target < target
                                    and self.bag.get(gathering, 0) >= revised_target):
                self.log('INFO', 'gather.reprioritized', previous=gathering,
                         next_item=name, target=revised_target,
                         immediate=self.next_work_materials(plan))
                return True
        relevant = self.relevant_facilities(plan)
        if any(f in relevant and ws and all(w.get('IsCompleted') for w in ws)
               for f, ws in self.groups.items()):
            return True
        for name, recipe in plan.recipes.items():
            if recipe.kind != 'alter' or not plan.jobs.get(name) or not recipe.possible:
                continue
            works = self.groups.get(recipe.facility, [])
            limit = 1 if recipe.facility.startswith('미확인 시설:') else 7
            running = [w for w in works if not w.get('IsCompleted')]
            affordable = self.affordable(recipe)
            batch = min(3, limit - len(works), plan.jobs[name])
            if len(works) < limit and (not running or
                    sum(float(w.get('RemainingSeconds', 0)) for w in running) <= 600
                    or affordable >= batch):
                return True
            # Completed works occupy slots until collected. Free those slots
            # when new work can be registered, even if other work is running.
            if (any(w.get('IsCompleted') for w in works)
                    and sum(float(w.get('RemainingSeconds', 0)) for w in running) > 600):
                return True
        return False

    def gather_deficit(self, name, plan):
        deficit = plan.raw[name]
        consumption = plan.required[name] - self.goals.get(name, 0)
        if self.storage.get(name, 0) and consumption > 0:
            # Crafting consumes bag materials before transferring storage.
            # Gathering the final bag reserve now would strand storage and
            # force us to gather that reserve twice.
            deficit = min(deficit, max(0, consumption - self.bag.get(name, 0) - self.storage[name]))
        return deficit

    def run(self):
        engine = self.engine
        self.log('INFO', 'scheduler.start', goals=self.goals, routes=self.routes)
        self.snapshot()
        last_action_state = None
        reported = False
        while True:
            engine.check()
            if all(self.bag.get(n, 0) >= count for n, count in self.goals.items()):
                engine.emit('완료: 모든 목표 수량을 가방에서 확인했습니다.')
                self.log('INFO', 'scheduler.complete', goals=self.goals, bag=self.bag,
                         storage=self.storage, calls=engine.calls)
                return
            state = self.fingerprint()
            if last_action_state is not None:
                self.no_progress = self.no_progress + 1 if state == last_action_state else 0
                if self.no_progress >= 2:
                    raise self.Halt('작업 후 재고·가공 일감이 변하지 않아 중단했습니다. 게임 상태를 확인하세요.')
            last_action_state = None
            weight_full = engine.weight_limited()
            plan = self.make_plan()
            for name in plan.raw:
                if not engine.gather[name].get('ToolOk'):
                    plan.blocked[name] = '채집 도구가 없거나 내구도가 부족합니다.'
            if plan.blocked and not weight_full:
                raise self.Halt('확보 불가: ' + ', '.join(f'{n}: {reason}' for n, reason in plan.blocked.items()))
            if not reported:
                engine.emit('초기 부족 재료: ' + (', '.join(f'{n} {c}개' for n, c in plan.raw.items()) or '없음'))
                engine.emit('보유 재료로 가공 시작 → 다른 시설 가동 → 부족 재료 채집 → 시설별 일괄 수령')
                reported = True
            relevant = self.relevant_facilities(plan)
            candidates = [r for n, r in plan.recipes.items() if plan.jobs.get(n, 0) and r.possible]
            altering = [r for r in candidates if r.kind == 'alter' and
                        len(self.groups.get(r.facility, [])) < (1 if r.facility.startswith('미확인 시설:') else 7)]
            refill_here = any(r.facility == self.current_facility for r in altering)
            ready_groups = [(f, ws) for f, ws in self.groups.items()
                            if f in relevant and any(w.get('IsCompleted') for w in ws)
                            and (all(w.get('IsCompleted') for w in ws)
                                 or (sum(float(w.get('RemainingSeconds', 0)) for w in ws) > 600
                                     and any(r.kind == 'alter' and r.facility == f for r in candidates)))]
            if ready_groups and not refill_here:
                facility, works = min(ready_groups, key=lambda pair: pair[0] != self.current_facility)
                engine.emit(f'{facility}: {len(works)}칸 완료 · 일괄 수령')
                engine.act('complete_altering_work', next(w['DisplayName'] for w in works if w.get('IsCompleted')))
                self.current_facility = facility
                last_action_state = state
                self.snapshot()
                continue

            if altering:
                recipe = min(altering, key=lambda r: (any(not w.get('IsCompleted') for w in self.groups.get(r.facility, [])),
                                                      r.facility != self.current_facility))
                engine.emit(f'{recipe.facility}: {len(self.groups.get(recipe.facility, []))}/7칸 · {recipe.exact} 등록')
                engine.act('execute_altering', recipe.exact)
                self.current_facility = recipe.facility
                last_action_state = state
                self.snapshot()
                continue

            crafting = [r for r in candidates if r.kind == 'craft']
            if crafting:
                from app import CLIError
                recipe = min(crafting, key=lambda r: r.facility != self.current_facility)
                count = min(plan.jobs[recipe.output], self.affordable(recipe),
                            self.craft_limits.get(recipe.facility, 100))
                engine.emit(f'{recipe.exact}: {count}회 묶음 제작')
                try:
                    engine.act('execute_crafting', recipe.exact, craft_count=count)
                except CLIError as error:
                    data = error.data
                    if not isinstance(data, dict) or data.get('error') != 'invalid_count':
                        raise
                    limit = int(data.get('maxCount', 0))
                    if not 0 < limit < count:
                        raise
                    self.craft_limits[recipe.facility] = limit
                    engine.emit(f'{recipe.facility}: 제작 요청 상한 {limit}회 반영')
                    self.snapshot()
                    continue
                if recipe.output in self.untracked_outputs:
                    made = count * recipe.produced
                    engine.untracked_produced[recipe.output] = (
                        engine.untracked_produced.get(recipe.output, 0) + made)
                    self.log('INFO', 'craft.untracked_progress', output=recipe.output,
                             made=made, total=engine.untracked_produced[recipe.output])
                self.current_facility = recipe.facility
                last_action_state = state
                self.snapshot()
                continue

            if weight_full:
                engine.emit('가방 무게 90% 이상 · 채집 보류 · 가공·제작·수령 가능 작업 대기 · 15초 후 재확인')
                engine.stop.wait(15)
                engine.check()
                self.snapshot()
                continue

            gather_next = self.next_gather(plan)
            if gather_next:
                name, target = gather_next
                if not engine.gather[name].get('ToolOk'):
                    raise self.Halt(f'{name}: 채집 도구가 없거나 내구도가 부족합니다.')
                engine.emit(f'{name}: 보유 {self.bag.get(name, 0)}개 · 이번 채집 목표 {target}개')
                self.current_facility = None
                yielded = engine.gather_to(name, target,
                    interrupt_when=lambda: self.production_probe(name, target))
                self.current_facility = None
                last_action_state = None if yielded else state
                self.snapshot()
                continue

            active = [w for f, ws in self.groups.items() if f in relevant for w in ws if not w.get('IsCompleted')]
            if active:
                wait_state = tuple((w['DisplayName'], w.get('State'), w.get('RemainingSeconds')) for w in active)
                now = time.monotonic()
                if wait_state != self.last_wait:
                    self.last_change = now
                    self.last_wait = wait_state
                elif now - self.last_change > 180:
                    raise self.Halt('가공 상태가 3분 이상 갱신되지 않습니다. 게임 연결을 확인하세요.')
                seconds = min((float(w.get('RemainingSeconds', 5)) for w in active
                               if w.get('State') == 'InProgress'), default=5)
                engine.emit(f'시설 가공 중 · 다음 상태 확인까지 {max(1, min(seconds + 1, 5)):.0f}초')
                engine.stop.wait(max(1, min(seconds + 1, 5)))
                self.snapshot()
                continue
            raise self.Halt('실행 가능한 작업이 없습니다. 레시피·가공 시설·재료 상태를 확인하세요.')
