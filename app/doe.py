"""План опытов: какой режим поставить на следующих проходах, чтобы модель училась быстрее всего.

Журнал покрывает узкую область режимов (в сезоне 2026: D 33–40,5 с, T блоков в среднем по ступеням 40–68 °C).
Внутри неё эмпирическая и физическая модели почти совпадают, а за её пределами расходятся — значит, решить,
какая права, и уточнить параметры можно только проходами в других режимах. Подбор кандидата учитывает две вещи:

* новизна — расстояние от уже пройденных режимов в пространстве (D, T блоков, W вх) с масштабами
  10 с, 10 °C и 5 п.п. (единица ≈ «заметно другой режим»); точка внутри уже изученного облака почти ничего
  не добавит к оценке показателей b_dt и b_w (это идея D-оптимального плана: раздвигать факторы);
* расхождение моделей — |съём по эмпирической − съём по физической|, делённое на типичную ошибку прохода;
  там, где модели расходятся сильнее, один проход сильнее всего показывает, какая из них права
  (T-оптимальный план различения моделей, Atkinson & Fedorov, 1975).

Балл = min(новизна, 2) · (1 + min(расхождение, 3)). Кандидаты проверяются на ограничения: съём за проход в
допустимом лимите, T блоков и ожидаемая T зерна не выше заданных пределов, D 10…100. Опыт уходит от края
изученного не дальше шага DOE_STEP (10 с по D, 10 °C по T): балл растёт с расстоянием, и без шага первым
опытом выходил бы самый дальний режим (на журнале 2026 — D 79 при изученных 33–40, вдвое больший поток).
Пройденный шаг расширяет изученное, и следующий план идёт дальше — лесенкой. T блоков перебирается парой
уставок (1-я / 2-я ступень, как на пульте): от привычной пары обе ступени сдвигаются на 5 °C; модель
считает по среднему ступеней, как и при калибровке. По умолчанию меняется
один фактор относительно привычного режима (проще для сушильщиков и чище для анализа). План из нескольких
проходов набирается жадно: выбранная точка сразу добавляется к «уже изученным», чтобы следующая была другой.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from collections import Counter
from typing import Dict, List, Optional, Sequence, Tuple

from . import model as mdl

SCALE_D, SCALE_T, SCALE_W = 10.0, 10.0, 5.0
DOE_STEP = {"d": 10.0, "t": 10.0}       # насколько опыт может уйти от края изученного: D, с; T блоков, °C
T_SHIFT = 5.0                           # шаг перебора T блоков (обе ступени сразу), °C


@dataclass
class Candidate:
    d: float
    t: float
    dw_emp: float
    dw_phys: float
    w_out: float          # по выбранной в расчётах модели (истинная)
    t_grain: float
    novelty: float
    disagreement: float
    score: float
    feasible: bool
    why: str
    issues: str
    t_up: Optional[float] = None          # уставки ступеней, если T перебиралась парой (иначе — одно число t)
    t_low: Optional[float] = None


def explored_passes(rows: List[Dict], p: mdl.DryerParams) -> List[Dict]:
    """Режимы уже пройденных участков журнала: средние D, T, W вх и съём."""
    from .calibration import observe_all, segment_summary
    obs, _, _ = observe_all(rows, p)
    return [{"d": s["d"], "t": s["t"], "w_in": s["w_in"], "dw": s["dw_obs"], "start": s["start"], "n": s["n"]}
            for s in segment_summary(obs, p)]


def explored_bounds(p: mdl.DryerParams, passes: Sequence[Dict]) -> Optional[Dict[str, Tuple[float, float]]]:
    """Изученные диапазоны D и T блоков (среднее ступеней): калибровка (p.explored) вместе с проходами журнала —
    проходы могли добавиться после калибровки. None — нет ни того, ни другого."""
    out: Dict[str, Tuple[float, float]] = {}
    ex = p.explored or {}
    for key in ("d", "t"):
        vals = [float(x[key]) for x in passes if x.get(key) is not None]
        try:
            vals += [float(ex[key][0]), float(ex[key][1])]
        except (KeyError, IndexError, TypeError, ValueError):
            pass
        if not vals:
            return None
        out[key] = (min(vals), max(vals))
    return out


def experiment_bounds(p: mdl.DryerParams, passes: Sequence[Dict]) -> Optional[Dict[str, Tuple[float, float]]]:
    """Куда может зайти опыт: изученные диапазоны ± DOE_STEP."""
    b = explored_bounds(p, passes)
    if b is None:
        return None
    return {k: (lo - DOE_STEP[k], hi + DOE_STEP[k]) for k, (lo, hi) in b.items()}


def typical_pair(rows: Sequence[Dict]) -> Optional[Tuple[float, float]]:
    """Привычные уставки T блоков (1-я, 2-я ступень) — пара, простоявшая больше всего строк журнала. Пустая клетка
    в журнале — прежняя уставка; одна ступень не записана ни разу — пары нет (None)."""
    up = low = None
    cnt: Counter = Counter()
    for r in rows:
        if r.get("t_up") is not None:
            up = float(r["t_up"])
        if r.get("t_low") is not None:
            low = float(r["t_low"])
        if up is not None and low is not None:
            cnt[(up, low)] += 1
    return cnt.most_common(1)[0][0] if cnt else None


def typical_regime(passes: Sequence[Dict]) -> Tuple[float, float]:
    if not passes:
        return 35.0, 60.0
    ds = sorted(x["d"] for x in passes)
    ts = sorted(x["t"] for x in passes)
    return round(ds[len(ds) // 2]), round(ts[len(ts) // 2])


def _novelty(d: float, t: float, w: float, pts: Sequence[Tuple[float, float, float]]) -> float:
    if not pts:
        return 2.0
    return min(math.sqrt(((d - a) / SCALE_D) ** 2 + ((t - b) / SCALE_T) ** 2 + ((w - c) / SCALE_W) ** 2)
               for a, b, c in pts)


def suggest(p: mdl.DryerParams, c_amb: mdl.Conditions, w_in: float, passes: Sequence[Dict], hard: float,
            d_typ: float, t_typ: float, n_plan: int = 4, one_factor: bool = True, sigma_pp: float = 0.35,
            min_dw: float = 1.5, t_max: float = 70.0, t_pair: Optional[Tuple[float, float]] = None,
            bounds: Optional[Dict[str, Tuple[float, float]]] = None) -> Tuple[List[Candidate], List[Candidate]]:
    """Возвращает (план из n_plan проходов, все допустимые кандидаты по убыванию балла).

    hard — лимит съёма за проход для опытов (проверяется по обеим моделям — берётся худшая);
    t_max — потолок T блоков для опытов (не выше заданного предела агента; для пары — по старшей ступени);
    t_pair — привычные уставки ступеней: T перебирается парой (обе ступени ± 5 °C), t_typ — их среднее;
    bounds — куда может зайти опыт (experiment_bounds); None — посчитать по p и passes."""
    emp = replace(p, model_kind="empirical")
    phys = replace(p, model_kind="physics")
    base = p
    if bounds is None:
        bounds = experiment_bounds(p, passes)
    d_lo, d_hi = bounds["d"] if bounds else (10.0, 100.0)
    t_lo, t_hi_b = bounds["t"] if bounds else (-1e9, 1e9)
    d_vals = sorted({int(round(d_typ + k)) for k in range(-25, 41, 5)} | {int(round(d_typ))})
    d_vals = [d for d in d_vals if 10 <= d <= 100 and d_lo - 1e-9 <= d <= d_hi + 1e-9]
    t_hi = min(t_max, p.t_agent_max) if p.t_agent_max else t_max
    if t_pair is not None:
        t_typ = 0.5 * (t_pair[0] + t_pair[1])
        t_opts = [(t_pair[0] + T_SHIFT * k, t_pair[1] + T_SHIFT * k) for k in range(-3, 4)]
        t_opts = [(0.5 * (a + b), a, b) for a, b in t_opts if min(a, b) >= 35 and max(a, b) <= t_hi]
    else:
        t_opts = [(float(t), None, None) for t in sorted({round(t_typ + k) for k in range(-15, 16, 5)})
                  if 35 <= t <= t_hi]
    t_opts = [x for x in t_opts if t_lo - 1e-9 <= x[0] <= t_hi_b + 1e-9 or abs(x[0] - t_typ) < 0.5]
    cands: List[Candidate] = []
    for d in d_vals:
        for t, t_up, t_low in t_opts:
            if one_factor and not (abs(d - d_typ) < 0.5 or abs(t - t_typ) < 0.5):
                continue
            if abs(d - d_typ) < 0.5 and abs(t - t_typ) < 0.5:
                continue                                     # привычный режим — нового не даст
            c = mdl.Conditions(float(t), c_amb.t_amb, c_amb.rh)
            pe = mdl.solve_pass(emp, c, w_in, 1.0, d)
            pp = mdl.solve_pass(phys, c, w_in, 1.0, d)
            pb = pe if base.model_kind != "physics" else pp
            dw_e, dw_p = w_in - pe.w_out, w_in - pp.w_out
            tg = mdl.grain_temp(p, t, w_in)
            issues = []
            if max(dw_e, dw_p) > hard + 1e-9:
                issues.append(f"съём до {max(dw_e, dw_p):.1f} > {hard:g} п.п.")
            if min(dw_e, dw_p) < min_dw:
                issues.append(f"съём {min(dw_e, dw_p):.1f} п.п. — слишком мало, шум влагомера его скроет")
            if p.t_grain_max and tg > p.t_grain_max:
                issues.append(f"T зерна ≈ {tg:.0f} > {p.t_grain_max:g} °C")
            nov = _novelty(d, t, w_in, [(x["d"], x["t"], x["w_in"]) for x in passes])
            dis = abs(dw_e - dw_p) / max(sigma_pp, 0.05)
            score = min(nov, 2.0) * (1.0 + min(dis, 3.0))
            why = []
            if abs(t - t_typ) >= 0.5:
                why.append("уточнит влияние T блоков (b_dt)")
            if abs(d - d_typ) >= 0.5:
                why.append("уточнит влияние времени пребывания (D)")
            if dis >= 1.0:
                why.append(f"модели расходятся на {abs(dw_e - dw_p):.1f} п.п. — покажет, какая права")
            cands.append(Candidate(d=float(d), t=float(t), dw_emp=dw_e, dw_phys=dw_p, w_out=pb.w_out, t_grain=tg,
                                   novelty=nov, disagreement=dis, score=score, feasible=not issues,
                                   why="; ".join(why), issues="; ".join(issues), t_up=t_up, t_low=t_low))
    ok = sorted([x for x in cands if x.feasible], key=lambda x: -x.score)
    plan: List[Candidate] = []
    pts = [(x["d"], x["t"], x["w_in"]) for x in passes]
    pool = [Candidate(**x.__dict__) for x in ok]      # копии: в ok остаются исходные баллы
    for _ in range(max(n_plan, 0)):
        if not pool:
            break
        for x in pool:
            x.novelty = _novelty(x.d, x.t, w_in, pts)
            x.score = min(x.novelty, 2.0) * (1.0 + min(x.disagreement, 3.0))
        best = max(pool, key=lambda x: x.score)
        if best.novelty < 0.3:
            break
        plan.append(Candidate(**best.__dict__))
        pts.append((best.d, best.t, w_in))
        pool.remove(best)
    return plan, ok


def fmt_cand_t(x: Candidate) -> str:
    """T блоков кандидата так, как её ставят: «60/55» (пара) или одно число."""
    if x.t_up is not None and x.t_low is not None:
        return f"{x.t_up:.0f}/{x.t_low:.0f}"
    return f"{x.t:.0f}"
