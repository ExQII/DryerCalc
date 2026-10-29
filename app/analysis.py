"""Проверка модели и приборов: поправка влагомера по парам замеров, прогноз против факта по проходам."""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from . import model as mdl

# t-квантили Стьюдента 0,975 (двусторонний 95 %) для 1…30 степеней свободы
_T975 = [12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145,
         2.131, 2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048,
         2.045, 2.042]


def t975(df: int) -> float:
    if df <= 0:
        return float("inf")
    if df <= 30:
        return _T975[df - 1]
    return 1.96 + 2.4 / df          # приближение, точнее 0,005 при df > 30


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# Поправка выходного влагомера по парам «показание на выходе — после охлаждения»
# ----------------------------------------------------------------------------

def _ols(xs: List[List[float]], ys: List[float]) -> Tuple[List[float], List[float], float]:
    """МНК: коэффициенты, их стандартные ошибки, остаточное СКО."""
    from .calibration import _solve
    n, k = len(ys), len(xs[0])
    xtx = [[sum(x[i] * x[j] for x in xs) for j in range(k)] for i in range(k)]
    xty = [sum(x[i] * y for x, y in zip(xs, ys)) for i in range(k)]
    beta = _solve(xtx, xty)
    res = [y - sum(b * xi for b, xi in zip(beta, x)) for x, y in zip(xs, ys)]
    dof = max(n - k, 1)
    s2 = sum(r * r for r in res) / dof
    inv_diag = []
    for i in range(k):
        e = [1.0 if j == i else 0.0 for j in range(k)]
        inv_diag.append(_solve(xtx, e)[i])
    se = [math.sqrt(max(s2 * d, 0.0)) for d in inv_diag]
    return beta, se, math.sqrt(s2)


def meter_bias(pairs: Sequence[Dict]) -> Dict:
    """Пары {reading, ref, t?}: reading — выходной влагомер сушилки, ref — та же проба после охлаждения/в
    лаборатории, t — T зерна по датчикам сушилки в момент пробы (как в журнале), необязательно.

    Поправка = reading − ref. Три варианта:
      M0: постоянная a (среднее; 95 % интервал по Стьюденту);
      M1: a + k_w·(показание − 20) — влагомер ошибается по-разному на сырых и сухих пробах;
      M2: a + k_w·(показание − 20) + k_t·(T − 30) — ещё и зависит от температуры зерна.
    Сложный вариант выбирается, только когда данных достаточно (M1: ≥ 10 пар, M2: ≥ 12 пар с T) и его
    коэффициент значим (больше двух своих стандартных ошибок). Выбросы (> 3 MAD от медианы) не учитываются."""
    rows = []
    for i, pr in enumerate(pairs):
        a, b = _f(pr.get("reading")), _f(pr.get("ref"))
        if a is not None and b is not None:
            rows.append((i, a, b, _f(pr.get("t"))))
    res: Dict = {"n_all": len(rows), "outliers": [], "choice": "M0", "a": None, "kw": 0.0, "kt": 0.0}
    if not rows:
        res["text"] = "Пар нет. Вносите пары: показание выходного влагомера и влажность той же пробы после охлаждения."
        return res
    vals = [a - b for _, a, b, _ in rows]
    sv = sorted(vals)
    med = sv[len(sv) // 2] if len(sv) % 2 else 0.5 * (sv[len(sv) // 2 - 1] + sv[len(sv) // 2])
    mad = sorted(abs(v - med) for v in vals)[len(vals) // 2] * 1.4826 if len(vals) >= 5 else 0.0
    if mad > 1e-9:
        res["outliers"] = [i for (i, _, _, _), d in zip(rows, vals) if abs(d - med) > 3.0 * mad]
    use = [(a, b, t) for (i, a, b, t) in rows if i not in res["outliers"]]
    diffs = [a - b for a, b, _ in use]
    n = len(use)
    mean = sum(diffs) / n
    res.update({"n": n, "mean": mean, "median": med, "a": mean})
    if n >= 2:
        sd = math.sqrt(sum((d - mean) ** 2 for d in diffs) / (n - 1))
        se = sd / math.sqrt(n)
        half = t975(n - 1) * se
        res.update({"sd": sd, "se": se, "ci": (mean - half, mean + half), "half": half})
    # M1
    xr = [a for a, _, _ in use]
    if n >= 4 and max(xr) - min(xr) >= 2.0:
        beta, se_b, s_res = _ols([[1.0, a - 20.0] for a in xr], diffs)
        res["m1"] = {"a": beta[0], "kw": beta[1], "se_kw": se_b[1], "sd": s_res}
        if n >= 10 and abs(beta[1]) > 2.0 * se_b[1]:
            res.update({"choice": "M1", "a": beta[0], "kw": beta[1]})
    # M2
    with_t = [(a, b, t) for a, b, t in use if t is not None]
    if len(with_t) >= 5 and len(with_t) >= 0.7 * n:
        ts = [t for _, _, t in with_t]
        if max(ts) - min(ts) >= 5.0:
            beta, se_b, s_res = _ols([[1.0, a - 20.0, t - 30.0] for a, _, t in with_t], [a - b for a, b, _ in with_t])
            res["m2"] = {"a": beta[0], "kw": beta[1], "kt": beta[2], "se_kw": se_b[1], "se_kt": se_b[2], "sd": s_res}
            if len(with_t) >= 12 and abs(beta[2]) > 2.0 * se_b[2]:
                res.update({"choice": "M2", "a": beta[0], "kw": beta[1], "kt": beta[2]})
    if n < 2:
        res["text"] = f"Одна пара: поправка {mean:+.2f} п.п., оценить точность нельзя."
    else:
        tail = ("мало пар: интервал широкий, копите дальше" if n < 8 else
                "оценка устойчивая" if res["half"] <= 0.3 else "разброс большой: проверьте отбор проб")
        txt = (f"Постоянная поправка {mean:+.2f} п.п. (95 %: {res['ci'][0]:+.2f} … {res['ci'][1]:+.2f}), "
               f"разброс пар ±{res['sd']:.2f}, пар {n}" +
               (f", выбросов {len(res['outliers'])}" if res["outliers"] else "") + f" — {tail}.")
        if "m1" in res:
            m1 = res["m1"]
            sig = abs(m1["kw"]) > 2.0 * m1["se_kw"]
            txt += (f"<br>Зависимость от показания: k_w = {m1['kw']:+.3f} ± {m1['se_kw']:.3f} на 1 % — "
                    + (("значима" if sig else "не значима") if n >= 10 else f"пар мало ({n}), не учитывается: нужно ≥ 10")
                    + ".")
        if "m2" in res:
            m2 = res["m2"]
            sig = abs(m2["kt"]) > 2.0 * m2["se_kt"]
            txt += (f"<br>Зависимость от T зерна: k_t = {m2['kt']:+.3f} ± {m2['se_kt']:.3f} на 1 °C — "
                    + (("значима" if sig else "не значима") if len(with_t) >= 12
                       else f"пар с T мало ({len(with_t)}), не учитывается: нужно ≥ 12") + ".")
        names = {"M0": "постоянная", "M1": "с наклоном по показанию", "M2": "с наклоном и температурой"}
        txt += f"<br>Рекомендуется: <b>{names[res['choice']]}</b> — a = {res['a']:+.2f}" + \
               (f", k_w = {res['kw']:+.3f}" if res["choice"] != "M0" else "") + \
               (f", k_t = {res['kt']:+.3f}" if res["choice"] == "M2" else "") + "."
        res["text"] = txt
    return res


def rate_sigma_from_backtest(bt: Dict, model_kind: str) -> Optional[Tuple[float, int]]:
    """Относительная ошибка прогноза съёма (СКО) по проходам, где был честный прогноз; ≥ 3 прохода."""
    key = "fc_phys" if model_kind == "physics" else "fc_emp"
    rel = [(r[key] - r["dw_obs"]) / r["dw_obs"] for r in bt.get("rows", []) if r.get(key) is not None and r["dw_obs"] > 0.5]
    if len(rel) < 3:
        return None
    sig = math.sqrt(sum(e * e for e in rel) / len(rel))
    return min(max(sig, 0.02), 0.30), len(rel)


# ----------------------------------------------------------------------------
# Прогноз против факта
# ----------------------------------------------------------------------------

def _rows_by_segment(rows: List[Dict]) -> Tuple[Dict[int, List[int]], List[Dict]]:
    from .calibration import prepare
    prep, _ = prepare(rows)
    segs: Dict[int, List[int]] = {}
    for i, r in enumerate(prep):
        segs.setdefault(r["seg"], []).append(i)
    return segs, prep


def _seg_eval(test_rows: List[Dict], p: mdl.DryerParams) -> Optional[Dict]:
    from .calibration import observe_all, segment_summary
    obs, _, _ = observe_all(test_rows, p)
    segs = segment_summary(obs, p)
    if not segs:
        return None
    # в тестовых строках один проход, но prepare мог разделить его — берём самый длинный участок
    return max(segs, key=lambda s: s["n"])


def backtest(rows: List[Dict], p: mdl.DryerParams, half_life_days: float = 0.0, weather=None,
             max_segments: int = 15, min_rows: int = 12, progress=None) -> Dict:
    """По каждому проходу журнала (в порядке времени):
    * «текущая» — ошибка съёма текущими параметрами (обе модели);
    * «прогноз» — модель откалибрована только по проходам ДО этого и предсказывает его (как было бы в жизни).
    """
    from .calibration import calibrate
    if weather is not None:
        rows = weather.fill_rows(rows)
    segs, prep = _rows_by_segment(rows)
    seg_start: Dict[int, datetime] = {}
    for s, idxs in segs.items():
        dts = [prep[i]["dt"] for i in idxs if prep[i].get("dt") is not None]
        if dts:
            seg_start[s] = min(dts)
    order = []
    for s, idxs in segs.items():
        data = [i for i in idxs if prep[i].get("w_out") is not None]
        if len(data) >= min_rows:
            dts = [prep[i]["dt"] for i in idxs if prep[i].get("dt") is not None]
            order.append((min(dts) if dts else datetime.min, s))
    order.sort()
    order = order[-max_segments:] if max_segments else order
    p_emp = replace(p, model_kind="empirical")
    p_phys = replace(p, model_kind="physics")
    out = []
    for k, (t0, s) in enumerate(order):
        if progress:
            progress(k, len(order))
        idxs = set(segs[s])
        test = [rows[i] for i in sorted(idxs)]
        row: Dict = {"seg": s, "start": t0}
        cur_e = _seg_eval(test, p_emp)
        cur_p = _seg_eval(test, p_phys)
        if cur_e is None:
            continue
        row.update({"n": cur_e["n"], "w_in": cur_e["w_in"], "w_out": cur_e["w_out"], "d": cur_e["d"],
                    "t": cur_e["t"], "dw_obs": cur_e["dw_obs"], "cur_emp": cur_e["dw_pred"],
                    "cur_phys": cur_p["dw_pred"] if cur_p else None,
                    "end": cur_e["end"]})
        train_idx = sorted(i for ss, ii in segs.items() if seg_start.get(ss) is not None and seg_start[ss] < t0
                           for i in ii)
        if train_idx:
            try:
                fit, _ = calibrate([rows[i] for i in train_idx], p, half_life_days=half_life_days)
                fe = _seg_eval(test, replace(fit, model_kind="empirical"))
                fp = _seg_eval(test, replace(fit, model_kind="physics"))
                row["fc_emp"] = fe["dw_pred"] if fe else None
                row["fc_phys"] = fp["dw_pred"] if fp else None
            except ValueError:
                pass
        out.append(row)

    def mae(key: str) -> Optional[float]:
        e = [abs(r[key] - r["dw_obs"]) for r in out if r.get(key) is not None]
        return sum(e) / len(e) if e else None

    def bias(key: str) -> Optional[float]:
        e = [r[key] - r["dw_obs"] for r in out if r.get(key) is not None]
        return sum(e) / len(e) if e else None

    summary = {k: mae(k) for k in ("cur_emp", "cur_phys", "fc_emp", "fc_phys")}
    recent = [r for r in out if r.get("cur_emp" if p.model_kind != "physics" else "cur_phys") is not None][-2:]
    key = "cur_phys" if p.model_kind == "physics" else "cur_emp"
    drift = (sum(r[key] - r["dw_obs"] for r in recent) / len(recent)) if recent else None
    return {"rows": out, "mae": summary, "bias": {k: bias(k) for k in summary}, "drift": drift,
            "drift_n": len(recent)}


def drift_text(bt: Dict, model_kind: str) -> str:
    d = bt.get("drift")
    if d is None:
        return "Мало проходов для оценки дрейфа."
    name = "физическая" if model_kind == "physics" else "эмпирическая"
    if abs(d) <= 0.5:
        return f"Дрейфа нет: на последних {bt['drift_n']} проходах {name} модель ошибается в среднем на {d:+.2f} п.п."
    sign = "завышает" if d > 0 else "занижает"
    return (f"⚠ На последних {bt['drift_n']} проходах {name} модель {sign} съём в среднем на {abs(d):.2f} п.п. — "
            f"перекалибруйте (вкладка «Журнал» → «Калибровать модель») с весом свежих данных.")
