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
    best = None
    for pl in places:
        st = [(s.mass, s.done if s.mass > EPS else 0) for s in silos]
        for i, put in pl:
            st[i] = (st[i][0] + put, 0)
        found = _search(st, caps, free, targets, n, factors)
        if found is None:
            continue
        mis, cost, chain = found
        rank = (round(mis, 1), cost, len(pl))
        if best is None or rank < best[0]:
            best = (rank, pl, st, chain, mis)
    if best is None:
        res.reason = ("Сушить некуда: нет ни пустого силоса, ни места под зерно следующего прохода. Чтобы не смешивать "
                      "проходы, перед сушкой нужен хотя бы один пустой оперативный силос (или силос, куда поместится "
                      "первый из силосов после сушки).")
        return res
    _rank, pl, st0, chain, mis = best
    res.placement, res.start_state, res.misplaced = pl, st0, mis
    t = start
    total = 0.0
    last_k = None
    for (i, j, take, out), after in chain:
        k = after[j][1]
        if last_k is not None and k != last_k:
            total += overhead_h                          # между проходами: рециркуляция, переключения
            t = t + timedelta(hours=overhead_h) if t else None
        last_k = k
        h = out / rates[k - 1] if rates and len(rates) >= k and rates[k - 1] > 0 else 0.0
        res.steps.append(Step(k=k, src=i, dst=j, m_in=take, m_out=out, after=list(after), hours=h, start=t,
                              end=t + timedelta(hours=h) if t else None, split=after[i][0] > EPS))
        total += h
        t = t + timedelta(hours=h) if t else None
    if res.steps:
        total += overhead_h
    res.hours = total
    res.final = list(chain[-1][1]) if chain else list(st0)
    res.ok = mis <= EPS
    if not res.ok:
        where = ", ".join(f"{names[i]} — {m:.0f} т" for i, (m, _d) in enumerate(res.final)
                          if free[i] and m > EPS and not targets[i])
        tcap = sum(c for c, tg, f in zip(caps, targets, free) if tg and f)
        dry = sum(m for (m, _d), f in zip(res.final, free) if f)
        if dry > tcap + EPS:
            res.reason = (f"Высушенного зерна будет {dry:.0f} т, а в отмеченных силосах помещается {tcap:.0f} т. "
                          f"Вне отмеченных останется: {where}. Отметьте ещё один силос.")
        else:
            res.reason = (f"Без смешивания проходов всё зерно в отмеченные силосы не приходит — останется: {where}. "
                          "Освободите перед сушкой ещё один силос, отметьте другой конечный силос или измените число "
                          "проходов: от него зависит, в каком силосе зерно окажется в конце.")
    # приёмка с поля
    for s in silos:
        if s.intake and inflow_tpd > 0:
            room = s.cap - s.mass
            h_full = room / inflow_tpd * 24.0
            if h_full < total:
                res.warnings.append(
                    f"Силос приёмки {s.name} заполнится через {h_full:.0f} ч ({room:.0f} т при {inflow_tpd:.0f} т/сут), а "
                    f"план идёт {total:.0f} ч: приход с поля придётся остановить или принимать куда-то ещё.")
    if inflow_tpd > 0 and not any(s.intake for s in silos):
        res.warnings.append("Указан приход с поля, но не выбран силос приёмки: место под приход план не держит.")
    return res
