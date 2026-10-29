"""План опытов: какой режим поставить на следующих проходах, чтобы модель училась быстрее всего.

Журнал октября покрывает узкую область: D 35–43, T блоков 48–73 °C. Внутри неё эмпирическая и физическая
модели почти совпадают, а за её пределами расходятся — значит, решить, какая права, и уточнить параметры можно
только проходами в других режимах. Подбор кандидата учитывает две вещи:

* новизна — расстояние от уже пройденных режимов в пространстве (D, T блоков, W вх) с масштабами
  10 с, 10 °C и 5 п.п. (единица ≈ «заметно другой режим»); точка внутри уже изученного облака почти ничего
  не добавит к оценке показателей b_dt и b_w (это идея D-оптимального плана: раздвигать факторы);
* расхождение моделей — |съём по эмпирической − съём по физической|, делённое на типичную ошибку прохода;
  там, где модели расходятся сильнее, один проход сильнее всего показывает, какая из них права
  (T-оптимальный план различения моделей, Atkinson & Fedorov, 1975).

Балл = min(новизна, 2) · (1 + min(расхождение, 3)). Кандидаты проверяются на ограничения: съём за проход в
допустимом лимите, T блоков и ожидаемая T зерна не выше заданных пределов, D 10…100. По умолчанию меняется
один фактор относительно привычного режима (проще для сушильщиков и чище для анализа). План из нескольких
проходов набирается жадно: выбранная точка сразу добавляется к «уже изученным», чтобы следующая была другой.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Dict, List, Sequence, Tuple

from . import model as mdl

SCALE_D, SCALE_T, SCALE_W = 10.0, 10.0, 5.0


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


def explored_passes(rows: List[Dict], p: mdl.DryerParams) -> List[Dict]:
    """Режимы уже пройденных участков журнала: средние D, T, W вх и съём."""
    from .calibration import observe_all, segment_summary
    obs, _, _ = observe_all(rows, p)
    return [{"d": s["d"], "t": s["t"], "w_in": s["w_in"], "dw": s["dw_obs"], "start": s["start"], "n": s["n"]}
            for s in segment_summary(obs, p)]


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
            min_dw: float = 1.5, t_max: float = 70.0) -> Tuple[List[Candidate], List[Candidate]]:
    """Возвращает (план из n_plan проходов, все допустимые кандидаты по убыванию балла).

    hard — лимит съёма за проход для опытов (проверяется по обеим моделям — берётся худшая);
    t_max — потолок T блоков для опытов (не выше заданного предела агента)."""
    emp = replace(p, model_kind="empirical")
    phys = replace(p, model_kind="physics")
    base = p
    d_vals = sorted({int(round(d_typ + k)) for k in range(-25, 41, 5)} | {int(round(d_typ))})
    d_vals = [d for d in d_vals if 10 <= d <= 100]
    t_hi = min(t_max, p.t_agent_max) if p.t_agent_max else t_max
    t_vals = sorted({round(t_typ + k) for k in range(-15, 16, 5)})
    t_vals = [t for t in t_vals if 35 <= t <= t_hi]
    cands: List[Candidate] = []
    for d in d_vals:
        for t in t_vals:
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
                                   why="; ".join(why), issues="; ".join(issues)))
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
