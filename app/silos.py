"""План размещения зерна по оперативным силосам на время сушки.

Задача. Сушилка питается из оперативного силоса и выгружает в другой оперативный силос; партия проходит сушилку
несколько раз. Нужно: куда принять сырое зерно и в каком порядке гонять его из силоса в силос, чтобы после
последнего прохода оно лежало в заданных силосах.

Правила (как на элеваторе):
* за один шаг сушилка берёт зерно из одного силоса и сыплет в один силос; источник и приёмник — разные;
* сыпать можно только в пустой силос или к зерну того же прохода (сырое с подсушенным не смешивают);
* зерно идёт проход за проходом: пока есть зерно, не прошедшее k-й проход, (k+1)-й не начинают;
* силос, освободившийся в ходе прохода, в этом же проходе может стать приёмником;
* масса после прохода меньше: m · (100 − W до) / (100 − W после) — в силос помещается больше высушенного зерна;
* силос приёмки с поля (если зерно продолжает поступать) в плане не участвует — он занят приходом.

Поиск. Состояние — (масса, пройдено проходов) по каждому силосу; шаг — «источник → приёмник» на всё, что есть в
источнике, или сколько поместится в приёмник. Перебор по возрастанию числа шагов (кратчайший путь по состояниям,
их немного: 4 силоса, массы — комбинации исходных). Сначала перебираются размещения сырого зерна (в каком порядке
заполнять пустые силосы), для каждого — кратчайший план; берётся план с наименьшей массой вне заданных силосов,
затем — с наименьшим числом шагов (каждое переключение маршрута — работа и риск ошибки).

Очерёдность. В новом проходе первым сушат зерно, которое лежит дольше: если на прошлом проходе сначала наполнили
С3, а потом С4, то теперь сначала берут из С3. Правило мягкое: планы, где оно соблюдено, идут первыми; если с ним
в отмеченные силосы не прийти, оно нарушается (Step.out_of_turn, SiloPlan.fifo_breaks).

Варианты. Планов, приводящих зерно в отмеченные силосы, обычно много (сотни: можно поменять местами буферные
силосы, порядок источников). Перебираются все цепочки не длиннее кратчайшей + 1 переключение (до MAX_VARIANTS),
сортируются: без нарушений очерёдности → меньше переключений → без дробления источника → меньше занятых силосов
(свободный силос — запас под приход) → раньше конец. В окно отдаются SHOW_VARIANTS лучших.

Если уложить всё зерно в заданные силосы невозможно, возвращается лучший найденный план и причина; если двигаться
нельзя вообще (нет ни пустого силоса, ни места под зерно того же прохода) — только причина.

Чего здесь нет: зерна в самой сушилке (60 т в шахте переходят в приёмник в конце шага) и силосов хранения С5–С8
(в них зерно уходит после сушки, отдельно).
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import permutations
from typing import Dict, List, Optional, Sequence, Tuple

EPS = 0.05                      # т: меньше — силос пуст
MAX_STATES = 60000              # предел перебора на одно размещение
State = Tuple[Tuple[float, int], ...]
INF = 10 ** 9
EXTRA_STEPS = 1                 # варианты не длиннее кратчайшего плана + столько переключений
MAX_VARIANTS = 1500             # предел перебора вариантов
SHOW_VARIANTS = 40              # сколько лучших вариантов отдаётся в окно


@dataclass
class Silo:
    name: str
    cap: float                  # вместимость, т
    mass: float = 0.0           # лежит сейчас, т
    done: int = 0               # сколько проходов это зерно уже прошло
    target: bool = False        # здесь зерно должно лежать после последнего прохода
    intake: bool = False        # силос приёмки с поля: в плане не участвует


@dataclass
class Step:
    k: int                      # номер прохода (1…)
    src: int
    dst: int
    m_in: float                 # т из источника
    m_out: float                # т в приёмник (после сушки)
    after: List[Tuple[float, int]]      # состояние силосов после шага: (масса, пройдено проходов)
    hours: float = 0.0
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    split: bool = False         # источник не опустел: приёмник полон, остаток уйдёт следующим шагом
    out_of_turn: bool = False   # не по очереди: в другом силосе зерно того же прохода лежит дольше


@dataclass
class SiloPlan:
    ok: bool                            # всё зерно после последнего прохода — в заданных силосах
    reason: str = ""                    # почему не получилось / что не так
    placement: List[Tuple[int, float]] = field(default_factory=list)    # куда принять сырое: (силос, т)
    start_state: List[Tuple[float, int]] = field(default_factory=list)  # после размещения сырого
    steps: List[Step] = field(default_factory=list)
    misplaced: float = 0.0              # т вне заданных силосов в конце
    hours: float = 0.0
    warnings: List[str] = field(default_factory=list)
    final: List[Tuple[float, int]] = field(default_factory=list)
    fifo_breaks: int = 0                # сколько шагов не по очереди «что дольше лежит — то раньше сушим»
    variants: List["SiloPlan"] = field(default_factory=list)       # все найденные планы, лучший первым (сам план — [0])
    n_variants: int = 0                 # сколько вариантов найдено всего (показывается не больше SHOW_VARIANTS)
    variants_capped: bool = False       # перебор остановлен на MAX_VARIANTS


def _key(st: Sequence[Tuple[float, int]]) -> State:
    return tuple((round(m, 1), d if m > EPS else 0) for m, d in st)


def _moves(st: Sequence[Tuple[float, int]], caps: Sequence[float], free: Sequence[bool], n: int,
           factors: Sequence[float]):
    """Возможные шаги из состояния: (источник, приёмник, т из источника, т в приёмник, новое состояние)."""
    live = [d for (m, d), ok in zip(st, free) if ok and m > EPS and d < n]
    if not live:
        return
    cur = min(live)                                 # идёт проход cur + 1: остальное зерно ждёт
    f = factors[cur]
    for i, (mi, di) in enumerate(st):
        if not free[i] or mi <= EPS or di != cur:
            continue
        for j, (mj, dj) in enumerate(st):
            if j == i or not free[j]:
                continue
            if mj > EPS and dj != cur + 1:
                continue                            # в приёмнике зерно другого прохода
            room = caps[j] - (mj if mj > EPS else 0.0)
            if room <= EPS:
                continue
            out = min(mi * f, room)
            take = out / f
            new = list(st)
            left = mi - take
            new[i] = (left, di) if left > EPS else (0.0, 0)
            new[j] = ((mj if mj > EPS else 0.0) + out, cur + 1)
            yield i, j, take, out, new


def _search(start: Sequence[Tuple[float, int]], caps: Sequence[float], free: Sequence[bool], targets: Sequence[bool],
            n: int, factors: Sequence[float]):
    """Кратчайшая по числу шагов цепочка до «всё зерно прошло n проходов». Возвращает (масса вне заданных силосов,
    число шагов, цепочка) лучшего конца или None, если ни один конец недостижим."""
    def misplaced(st) -> float:
        return sum(m for (m, _d), tg, ok in zip(st, targets, free) if ok and m > EPS and not tg)

    def finished(st) -> bool:
        return all(d >= n for (m, d), ok in zip(st, free) if ok and m > EPS)

    k0 = _key(start)
    best: Dict[State, float] = {k0: 0.0}
    prev: Dict[State, Tuple[Optional[State], Optional[Tuple]]] = {k0: (None, None)}
    real: Dict[State, List[Tuple[float, int]]] = {k0: list(start)}
    heap: List[Tuple[float, int, State]] = [(0.0, 0, k0)]
    tick = 0
    ends: List[Tuple[float, float, State]] = []
    while heap and len(best) < MAX_STATES:
        cost, _t, key = heapq.heappop(heap)
        if cost > best.get(key, 1e18) + 1e-9:
            continue
        st = real[key]
        if finished(st):
            mis = misplaced(st)
            ends.append((mis, cost, key))
            if mis <= EPS:
                break                               # первым найден самый короткий план без остатка
            continue
        for i, j, take, out, new in _moves(st, caps, free, n, factors):
            nk = _key(new)
            # шаг, после которого источник не опустел, чуть дороже: при прочих равных — без дробления
            c = cost + 1.0 + (0.01 if new[i][0] > EPS else 0.0)
            if c < best.get(nk, 1e18) - 1e-9:
                best[nk] = c
                prev[nk] = (key, (i, j, take, out))
                real[nk] = new
                tick += 1
                heapq.heappush(heap, (c, tick, nk))
    if not ends:
        return None
    mis, cost, key = min(ends, key=lambda e: (round(e[0], 1), e[1]))
    chain = []
    while prev[key][0] is not None:
        pk, mv = prev[key]
        chain.append((mv, real[key]))
        key = pk
    return mis, cost, chain[::-1]


class _Dist:
    """Наименьшее число шагов от состояния до «всё зерно прошло n проходов и лежит в отмеченных силосах» (INF —
    недостижимо). С запоминанием: шаги только продвигают зерно вперёд, циклов нет."""

    def __init__(self, caps, free, targets, n, factors):
        self.caps, self.free, self.targets, self.n, self.factors = caps, free, targets, n, factors
        self.memo: Dict[State, int] = {}

    def done(self, st) -> bool:
        return all(d >= self.n and tg for (m, d), tg, ok in zip(st, self.targets, self.free) if ok and m > EPS)

    def __call__(self, st) -> int:
        key = _key(st)
        hit = self.memo.get(key)
        if hit is not None:
            return hit
        if all(d >= self.n for (m, d), ok in zip(st, self.free) if ok and m > EPS):
            best = 0 if self.done(st) else INF
        else:
            best = INF
            for _i, _j, _take, _out, new in _moves(st, self.caps, self.free, self.n, self.factors):
                best = min(best, 1 + self(new))
        self.memo[key] = best
        return best


def _all_chains(start, caps, free, n, factors, dist: "_Dist", limit: int, cap: int, fifo: bool):
    """Все цепочки шагов от start до цели длиной не больше limit (не больше cap штук). fifo — только цепочки, где
    каждый раз сушится зерно, которое лежит дольше остального зерна того же прохода."""
    out: List[List[Tuple]] = []
    stamp = [0] * len(start)            # номер шага, на котором силос начал наполняться; исходное зерно — 0

    def go(st, chain):
        if len(out) >= cap:
            return
        if dist(st) == 0:
            out.append(list(chain))
            return
        for i, j, take, o, new in _moves(st, caps, free, n, factors):
            if len(chain) + 1 + dist(new) > limit:
                continue
            if fifo and any(x != i and free[x] and st[x][0] > EPS and st[x][1] == st[i][1] and stamp[x] < stamp[i]
                            for x in range(len(st))):
                continue
            keep = (stamp[i], stamp[j])
            if st[j][0] <= EPS:
                stamp[j] = len(chain) + 1
            if new[i][0] <= EPS:
                stamp[i] = 0
            chain.append(((i, j, take, o), new))
            go(new, chain)
            chain.pop()
            stamp[i], stamp[j] = keep
    go(list(start), [])
    return out


def used_silos(plan: "SiloPlan") -> List[int]:
    """Силосы, которые план занимает хоть на одном шаге (остальные всё время свободны — например, под приход)."""
    used = {i for i, (m, _d) in enumerate(plan.start_state) if m > EPS}
    for s in plan.steps:
        used.update((s.src, s.dst))
    return sorted(used)


def _placements(silos: Sequence[Silo], new_mass: float) -> List[List[Tuple[int, float]]]:
    """Варианты приёма сырого зерна: порядок заполнения силосов, где сейчас пусто или лежит сырое (не приёмка)."""
    if new_mass <= EPS:
        return [[]]
    cand = [i for i, s in enumerate(silos) if not s.intake and (s.mass <= EPS or s.done == 0)
            and s.cap - s.mass > EPS]
    out, seen = [], set()
    for r in range(1, len(cand) + 1):
        for order in permutations(cand, r):
            left, pl = new_mass, []
            for i in order:
                put = min(left, silos[i].cap - silos[i].mass)
                if put > EPS:
                    pl.append((i, put))
                    left -= put
                if left <= EPS:
                    break
            if left > EPS or len(pl) != len(order):
                continue
            key = tuple(sorted((i, round(m, 1)) for i, m in pl))
            if key not in seen:
                seen.add(key)
                out.append(pl)
    return out


def plan_silos(silos: Sequence[Silo], new_mass: float, n: int, factors: Sequence[float],
               rates: Optional[Sequence[float]] = None, overhead_h: float = 0.0,
               start: Optional[datetime] = None, inflow_tpd: float = 0.0) -> SiloPlan:
    """План перемещений.

    silos — оперативные силосы с тем, что в них лежит; new_mass — сырого зерна ещё принять, т; n — проходов всего;
    factors[k] — во сколько раз меняется масса на проходе k + 1 (меньше 1); rates[k] — выгрузка на проходе k + 1,
    т/ч (для времени шагов); overhead_h — накладные на проход, ч; inflow_tpd — приход с поля в силос приёмки, т/сут.
    """
    caps = [s.cap for s in silos]
    free = [not s.intake for s in silos]
    targets = [s.target for s in silos]
    names = [s.name for s in silos]
    res = SiloPlan(ok=False)
    if n < 1 or len(factors) < n:
        res.reason = "Не задано число проходов."
        return res
    if not any(t and f for t, f in zip(targets, free)):
        res.reason = "Отметьте хотя бы один силос, где зерно должно лежать после сушки (и это не силос приёмки)."
        return res
    for s in silos:
        if s.mass > s.cap + EPS:
            res.reason = f"В {s.name} вписано {s.mass:.0f} т при вместимости {s.cap:.0f} т."
            return res
    have = sum(s.mass for s in silos if not s.intake)
    if have + new_mass <= EPS:
        res.reason = "Нет зерна для плана: впишите, сколько сырого принять или что уже лежит в силосах."
        return res
    places = _placements(silos, new_mass)
    if not places:
        room = sum(s.cap - s.mass for s in silos if not s.intake and (s.mass <= EPS or s.done == 0))
        res.reason = (f"Сырое зерно ({new_mass:.0f} т) не помещается: в пустых силосах и силосах с сырым зерном "
                      f"свободно {room:.0f} т.")
        return res
    def start_of(pl):
        st = [(s.mass, s.done if s.mass > EPS else 0) for s in silos]
        for i, put in pl:
            st[i] = (st[i][0] + put, 0)
        return st

    def build(pl, st0, chain, mis) -> SiloPlan:
        """Цепочка шагов → план со временем, очерёдностью и причиной, если зерно пришло не туда."""
        r = SiloPlan(ok=mis <= EPS, placement=list(pl), start_state=list(st0), misplaced=mis)
        t = start
        total = 0.0
        last_k = None
        # когда силос начал наполняться зерном, которое в нём лежит (номер шага; исходное зерно — 0): для очерёдности
        stamp = [0] * len(st0)
        cur = list(st0)
        for num, ((i, j, take, out), after) in enumerate(chain, 1):
            k = after[j][1]
            if last_k is not None and k != last_k:
                total += overhead_h                      # между проходами: рециркуляция, переключения
                t = t + timedelta(hours=overhead_h) if t else None
            last_k = k
            older = [x for x in range(len(cur)) if x != i and free[x] and cur[x][0] > EPS and cur[x][1] == cur[i][1]
                     and stamp[x] < stamp[i]]
            h = out / rates[k - 1] if rates and len(rates) >= k and rates[k - 1] > 0 else 0.0
            r.steps.append(Step(k=k, src=i, dst=j, m_in=take, m_out=out, after=list(after), hours=h, start=t,
                                end=t + timedelta(hours=h) if t else None, split=after[i][0] > EPS,
                                out_of_turn=bool(older)))
            if cur[j][0] <= EPS:
                stamp[j] = num
            if after[i][0] <= EPS:
                stamp[i] = 0
            cur = list(after)
            total += h
            t = t + timedelta(hours=h) if t else None
        if r.steps:
            total += overhead_h
        r.hours = total
        r.fifo_breaks = sum(1 for x in r.steps if x.out_of_turn)
        r.final = list(chain[-1][1]) if chain else list(st0)
        if not r.ok:
            where = ", ".join(f"{names[i]} — {m:.0f} т" for i, (m, _d) in enumerate(r.final)
                              if free[i] and m > EPS and not targets[i])
            tcap = sum(c for c, tg, f in zip(caps, targets, free) if tg and f)
            dry = sum(m for (m, _d), f in zip(r.final, free) if f)
            if dry > tcap + EPS:
                r.reason = (f"Высушенного зерна будет {dry:.0f} т, а в отмеченных силосах помещается {tcap:.0f} т. "
                            f"Вне отмеченных останется: {where}. Отметьте ещё один силос.")
            else:
                r.reason = (f"Без смешивания проходов всё зерно в отмеченные силосы не приходит — останется: {where}. "
                            "Освободите перед сушкой ещё один силос, отметьте другой конечный силос или измените "
                            "число проходов: от него зависит, в каком силосе зерно окажется в конце.")
        return r

    # Все варианты, которые приводят зерно в отмеченные силосы: по каждому размещению сырого зерна — все цепочки не
    # длиннее кратчайшей + EXTRA_STEPS. Сортировка: сначала без нарушений очерёдности («что дольше лежит — то раньше
    # сушим»), затем меньше переключений, меньше дроблений, раньше конец.
    exact: List[SiloPlan] = []
    starts = [(pl, start_of(pl)) for pl in places]
    dist = _Dist(caps, free, targets, n, factors)
    shortest = min((dist(st) for _pl, st in starts), default=INF)
    seen = set()
    capped = False
    if shortest < INF:
        # по очереди и кратчайшие — первыми; затем на одно переключение длиннее; затем те же без очерёдности
        for fifo, extra in ((True, 0), (True, EXTRA_STEPS), (False, 0), (False, EXTRA_STEPS)):
            for pl, st0 in starts:
                if dist(st0) > shortest + extra or len(exact) >= MAX_VARIANTS:
                    continue
                for chain in _all_chains(st0, caps, free, n, factors, dist, shortest + extra,
                                         MAX_VARIANTS - len(exact), fifo):
                    sig = (tuple(sorted((i, round(m, 1)) for i, m in pl)), tuple(mv[:2] for mv, _a in chain))
                    if sig not in seen:
                        seen.add(sig)
                        exact.append(build(pl, st0, chain, 0.0))
            if len(exact) >= MAX_VARIANTS:
                capped = True
                break
    if exact:
        exact.sort(key=lambda r: (r.fifo_breaks, len(r.steps), sum(1 for x in r.steps if x.split),
                                  len(used_silos(r)), r.hours))
        res = exact[0]
        res.variants = exact[:SHOW_VARIANTS]
        res.n_variants = len(exact)
        res.variants_capped = capped
    else:
        best = None
        for pl, st in starts:
            found = _search(st, caps, free, targets, n, factors)
            if found is None:
                continue
            mis, cost, chain = found
            rank = (round(mis, 1), cost, len(pl))
            if best is None or rank < best[0]:
                best = (rank, pl, st, chain, mis)
        if best is None:
            res.reason = ("Сушить некуда: нет ни пустого силоса, ни места под зерно следующего прохода. Чтобы не "
                          "смешивать проходы, перед сушкой нужен хотя бы один пустой оперативный силос (или силос, "
                          "куда поместится первый из силосов после сушки).")
            return res
        _rank, pl, st0, chain, mis = best
        res = build(pl, st0, chain, mis)
        res.variants, res.n_variants = [res], 1
    # приёмка с поля — предупреждения у каждого варианта свои (длительность разная)
    for var in res.variants:
        for s in silos:
            if s.intake and inflow_tpd > 0:
                room = s.cap - s.mass
                h_full = room / inflow_tpd * 24.0
                if h_full < var.hours:
                    var.warnings.append(
                        f"Силос приёмки {s.name} заполнится через {h_full:.0f} ч ({room:.0f} т при {inflow_tpd:.0f} "
                        f"т/сут), а план идёт {var.hours:.0f} ч: приход с поля придётся остановить или принимать "
                        "куда-то ещё.")
        if inflow_tpd > 0 and not any(s.intake for s in silos):
            var.warnings.append("Указан приход с поля, но не выбран силос приёмки: место под приход план не держит.")
    return res
