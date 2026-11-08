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
        res.update({"sd": sd, "se": se, "ci": (mean - half, mean + half), "half": half, "se_a": se})
    # M1
    xr = [a for a, _, _ in use]
    if n >= 4 and max(xr) - min(xr) >= 2.0:
        beta, se_b, s_res = _ols([[1.0, a - 20.0] for a in xr], diffs)
        res["m1"] = {"a": beta[0], "kw": beta[1], "se_kw": se_b[1], "sd": s_res, "se_a": se_b[0]}
        if n >= 10 and abs(beta[1]) > 2.0 * se_b[1]:
            res.update({"choice": "M1", "a": beta[0], "kw": beta[1], "se_a": se_b[0]})
    # M2
    with_t = [(a, b, t) for a, b, t in use if t is not None]
    if len(with_t) >= 5 and len(with_t) >= 0.7 * n:
        ts = [t for _, _, t in with_t]
        if max(ts) - min(ts) >= 5.0:
            beta, se_b, s_res = _ols([[1.0, a - 20.0, t - 30.0] for a, _, t in with_t], [a - b for a, b, _ in with_t])
            res["m2"] = {"a": beta[0], "kw": beta[1], "kt": beta[2], "se_kw": se_b[1], "se_kt": se_b[2], "sd": s_res,
                         "se_a": se_b[0]}
            if len(with_t) >= 12 and abs(beta[2]) > 2.0 * se_b[2]:
                res.update({"choice": "M2", "a": beta[0], "kw": beta[1], "kt": beta[2], "se_a": se_b[0]})
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


METER_NEAR = 2.0    # п.п.: данные «у цели», если показание пары (или лабораторная W партии) в пределах ±2 от цели


def meter_coverage(pairs: Sequence[Dict], points: Sequence[Dict], target: float, use_batches: bool = True,
                   res: Optional[Dict] = None) -> Dict:
    """Какие показания влагомера покрыты парами и есть ли данные у целевой влажности.

    Поправка нужна в первую очередь у цели (там решается, досушено ли зерно), а пары обычно набирают на
    сыром зерне. Без пар и партий у цели поправка там — заход за данные: при наклоне k_w, известном
    ±se_kw на 1 %, неизвестность у цели добавляет ±se_kw·|цель − среднее показание| (1σ).
    points — партии (batch_meter_points); res — результат meter_bias по тем же парам (для se_kw)."""
    skip = set((res or {}).get("outliers") or [])           # выбросы в поправку не идут — и в покрытие тоже
    reads = []
    for i, pr in enumerate(pairs):
        a, b = _f(pr.get("reading")), _f(pr.get("ref"))
        if a is not None and b is not None and i not in skip:
            reads.append(a)
    out: Dict = {"n": len(reads), "target": target, "n_near": 0, "n_batch_near": 0, "lo": None, "hi": None,
                 "u_target": None, "warn": False, "text": ""}
    if not reads:
        return out
    lo, hi = min(reads), max(reads)
    n_near = sum(1 for a in reads if abs(a - target) <= METER_NEAR)
    nb = sum(1 for x in points if x.get("diff") is not None and x.get("lab") is not None
             and abs(x["lab"] - target) <= METER_NEAR) if use_batches else 0
    out.update(lo=lo, hi=hi, n_near=n_near, n_batch_near=nb)
    txt = (f"Пары покрывают показания {lo:.1f}–{hi:.1f} %; у цели {target:g} % (±{METER_NEAR:g} п.п.): "
           f"пар {n_near}" + (f", партий {nb}" if use_batches else ""))
    if n_near or nb:
        txt += "."
    else:
        out["warn"] = True
        m1 = (res or {}).get("m1")
        if m1 and m1.get("se_kw"):
            mean_r = sum(reads) / len(reads)
            out["u_target"] = abs(m1["se_kw"]) * abs(target - mean_r)
        txt += (" — поправка у цели посчитана с заходом за данные"
                + (f" (наклон k_w известен ±{abs(m1['se_kw']):.3f} на 1 % → у цели ещё "
                   f"±{out['u_target']:.1f} п.п.)" if out["u_target"] is not None else "")
                + ". Возьмите 3–5 пар на последнем проходе, когда выход у цели; партия с лабораторной W тоже "
                  "даёт точку у цели.")
    out["text"] = txt
    return out


# ----------------------------------------------------------------------------
# Поправка влагомера по партиям: лабораторная влажность готовой партии против показаний выходного влагомера
# ----------------------------------------------------------------------------

BATCH_SD_PRIOR = 0.4    # п.п.: ожидаемый разброс оценки по одной партии, пока партий меньше трёх (отбор пробы из
                        # силоса, лаборатория, охлаждение/хранение)


def batch_meter_points(batches: Sequence[Dict], rows: List[Dict], p: mdl.DryerParams) -> List[Dict]:
    """По каждой партии с лабораторной W после охлаждения — средневзвешенное по выгрузке показание выходного влагомера
    за последний проход партии (то, что ушло в силос готовой партии), против лабораторной влажности.

    В расчёт идут строки последнего прохода с работающими горелками (охлаждение — другой режим влагомера, его
    показания не используются; масса охлаждения обычно — одна загрузка шахты). Разница «показание − лаборатория» —
    одна оценка поправки влагомера на средних для партии влажности и температуре зерна."""
    from .batches import parse_when
    from .calibration import pass_of_segment, prepare
    prep, reliable = prepare(rows)
    if not reliable:
        return []
    pass_of = pass_of_segment(prep)
    out: List[Dict] = []
    for b in batches:
        lab = _f(b.get("w_out"))
        t0, t1 = parse_when(str(b.get("start") or "")), parse_when(str(b.get("end") or ""))
        if lab is None or t0 is None or t1 is None:
            continue
        pt: Dict = {"name": str(b.get("name") or ""), "lab": lab, "read": None, "t": None, "diff": None, "n": 0,
                    "mass": 0.0, "note": ""}
        sel = [r for r in prep if r.get("dt") is not None and t0 <= r["dt"] <= t1]
        data = [r for r in sel if r.get("w_out") is not None and not r.get("recirc") and not r.get("burner_off")]
        if not data:
            pt["note"] = "нет проб выхода за время партии"
            out.append(pt)
            continue
        last = pass_of[data[-1]["seg"]]
        lst = [r for r in sel if pass_of[r["seg"]] == last]
        rd = [r for r in lst if r.get("w_out") is not None and not r.get("recirc") and not r.get("burner_off")]
        hours = [(r["dt"] - lst[0]["dt"]).total_seconds() / 3600.0 for r in rd]
        vals = [r["w_out"] for r in rd]
        temps = [0.5 * (r["t_a1"] + r["t_a2"]) if r.get("t_a1") is not None and r.get("t_a2") is not None else None
                 for r in rd]
        mass = s_w = s_t = m_t = 0.0
        for a, nb in zip(lst, lst[1:]):
            if a.get("d_f") is None or a.get("stopped") or a.get("fill") or a.get("recirc") or a.get("burner_off"):
                continue
            dm = mdl.q_at(p, a["d_f"]) * max((nb["dt"] - a["dt"]).total_seconds(), 0.0) / 3600.0
            if dm <= 0:
                continue
            ha = (a["dt"] - lst[0]["dt"]).total_seconds() / 3600.0
            hb = (nb["dt"] - lst[0]["dt"]).total_seconds() / 3600.0
            w_mid = 0.5 * (_interp(ha, hours, vals) + _interp(hb, hours, vals))
            mass += dm
            s_w += dm * w_mid
            tt = [(h, t) for h, t in zip(hours, temps) if t is not None]
            if tt:
                s_t += dm * 0.5 * (_interp(ha, [h for h, _ in tt], [t for _, t in tt])
                                   + _interp(hb, [h for h, _ in tt], [t for _, t in tt]))
                m_t += dm
        if mass <= 0:
            mass, s_w = float(len(vals)), float(sum(vals))
        pt.update(read=s_w / mass, t=(s_t / m_t) if m_t else None, n=len(rd), mass=mass)
        pt["diff"] = pt["read"] - lab
        if len(rd) < 6:
            pt["note"] = f"мало проб выхода в последнем проходе ({len(rd)})"
        out.append(pt)
    return out


def _interp(x: float, xs: Sequence[float], ys: Sequence[float]) -> float:
    if not xs:
        return float("nan")
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for (x0, y0), (x1, y1) in zip(zip(xs, ys), zip(xs[1:], ys[1:])):
        if x0 <= x <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return ys[-1]


def combine_meter(pair_res: Dict, points: Sequence[Dict], use_batches: bool = True) -> Dict:
    """Поправка влагомера по парам проб и по партиям вместе: обратно-дисперсионные веса.

    Наклоны k_w, k_t (если пары их показали) берутся из пар; каждая партия даёт оценку свободного члена a:
        a_партии = (показание − лаборатория) − k_w·(показание − 20) − k_t·(T − 30).
    Точность по парам — стандартная ошибка a; по партиям — разброс оценок между партиями / √n (пока партий меньше
    трёх — ожидаемый разброс BATCH_SD_PRIOR). Если пары и партии расходятся больше двух стандартных ошибок разницы,
    это признак систематики (отбор проб в пары, проба из силоса, усушка при хранении) — об этом предупреждаем."""
    kw, kt = float(pair_res.get("kw") or 0.0), float(pair_res.get("kt") or 0.0)
    res: Dict = {"kw": kw, "kt": kt, "a": pair_res.get("a"), "source": "пары", "se": pair_res.get("se_a"),
                 "n_pairs": pair_res.get("n", 0), "n_batches": 0, "warn": ""}
    pts = [x for x in points if x.get("diff") is not None] if use_batches else []
    a_b = [x["diff"] - kw * (x["read"] - 20.0) - (kt * (x["t"] - 30.0) if (kt and x.get("t") is not None) else 0.0)
           for x in pts]
    res["n_batches"] = len(a_b)
    if not a_b:
        return res
    mean_b = sum(a_b) / len(a_b)
    if len(a_b) >= 3:
        sd_b = math.sqrt(sum((v - mean_b) ** 2 for v in a_b) / (len(a_b) - 1))
        se_b = max(sd_b, 0.15) / math.sqrt(len(a_b))
    else:
        sd_b = None
        se_b = BATCH_SD_PRIOR / math.sqrt(len(a_b))
    res.update(a_batches=mean_b, se_batches=se_b, sd_batches=sd_b)
    a_p, se_p = pair_res.get("a"), pair_res.get("se_a")
    if a_p is None or not se_p or pair_res.get("n", 0) < 2:
        res.update(a=mean_b, se=se_b, source="партии")
        return res
    w_p, w_b = 1.0 / se_p ** 2, 1.0 / se_b ** 2
    a = (a_p * w_p + mean_b * w_b) / (w_p + w_b)
    res.update(a=a, se=1.0 / math.sqrt(w_p + w_b), source="пары и партии", a_pairs=a_p, se_pairs=se_p,
               share_batches=w_b / (w_p + w_b))
    z = abs(a_p - mean_b) / math.sqrt(se_p ** 2 + se_b ** 2)
    if z > 2.0:
        res["warn"] = (f"пары ({a_p:+.2f}) и партии ({mean_b:+.2f}) расходятся на {abs(a_p - mean_b):.2f} п.п. — больше "
                       f"случайного. Проверьте отбор проб в пары (та же проба, закрытая тара), отбор пробы из силоса "
                       f"для лаборатории, усушку при хранении.")
    return res


RATE_SIGMA_PRIOR = (0.05, 3)    # стартовая ошибка скорости сушки 5 % «весит» как 3 прохода


def rate_sigma_from_backtest(bt: Dict, model_kind: str) -> Optional[Tuple[float, int]]:
    """Относительная ошибка прогноза съёма (СКО) по проходам, где был честный прогноз; ≥ 3 прохода.

    По 3–5 проходам СКО само по себе ненадёжно (три удачных прохода дают ±1 %, а следующий может ошибиться на 5 %),
    поэтому оно сглаживается к стартовым 5 % с весом трёх проходов: σ² = (n·s² + 3·0,05²) / (n + 3). С ростом журнала
    вес данных растёт, и допуск сходится к ошибке на ваших проходах."""
    key = "fc_phys" if model_kind == "physics" else "fc_emp"
    rel = [(r[key] - r["dw_obs"]) / r["dw_obs"] for r in bt.get("rows", []) if r.get(key) is not None and r["dw_obs"] > 0.5]
    if len(rel) < 3:
        return None
    n = len(rel)
    s0, n0 = RATE_SIGMA_PRIOR
    sig = math.sqrt((sum(e * e for e in rel) + n0 * s0 * s0) / (n + n0))
    return min(max(sig, 0.02), 0.30), n


# ----------------------------------------------------------------------------
# Прогноз против факта
# ----------------------------------------------------------------------------

def _seg_eval(obs_seg: List[Dict], p: mdl.DryerParams, arr=None) -> Optional[Dict]:
    """Сводка одного прохода (наблюдения одного участка журнала) по модели p."""
    from .calibration import segment_summary
    segs = segment_summary(obs_seg, p, arr)
    return max(segs, key=lambda s: s["n"]) if segs else None


def backtest(rows: List[Dict], p: mdl.DryerParams, half_life_days: float = 0.0, weather=None,
             max_segments: int = 15, min_rows: int = 12, progress=None, cancelled=None) -> Dict:
    """По каждому проходу журнала (в порядке времени):
    * «текущая» — ошибка съёма текущими параметрами (обе модели);
    * «прогноз» — модель откалибрована только по проходам ДО этого и предсказывает его (как было бы в жизни).

    Журнал разбирается один раз: наблюдения (пары «вход в t − τ / выход в t») не зависят от
    подбираемых параметров модели, поэтому калибровка «до прохода» идёт по готовым наблюдениям
    предыдущих проходов. progress(k, n) — ход расчёта; cancelled() → True — прервать."""
    from .calibration import _obs_arr, calibrate_obs, observe_all, prepare
    if weather is not None:
        rows = weather.fill_rows(rows)
    prep, _ = prepare(rows)
    obs_all, _, _ = observe_all(rows, p)
    seg_rows: Dict[int, List[Dict]] = {}
    for r in prep:
        seg_rows.setdefault(r["seg"], []).append(r)
    seg_start: Dict[int, datetime] = {}
    for s, lst in seg_rows.items():
        dts = [r["dt"] for r in lst if r.get("dt") is not None]
        if dts:
            seg_start[s] = min(dts)
    obs_by_seg: Dict[int, List[Dict]] = {}
    for o in obs_all:
        obs_by_seg.setdefault(o["seg"], []).append(o)
    order = []
    for s, lst in seg_rows.items():
        if sum(1 for r in lst if r.get("w_out") is not None) >= min_rows:
            order.append((seg_start.get(s, datetime.min), s))
    order.sort()
    order = order[-max_segments:] if max_segments else order
    p_emp = replace(p, model_kind="empirical")
    p_phys = replace(p, model_kind="physics")
    out = []
    for k, (t0, s) in enumerate(order):
        if progress:
            progress(k, len(order))
        if cancelled and cancelled():
            break
        test = obs_by_seg.get(s, [])
        if not test:
            continue
        arr = _obs_arr(test)
        row: Dict = {"seg": s, "start": t0}
        cur_e = _seg_eval(test, p_emp, arr)
        cur_p = _seg_eval(test, p_phys, arr)
        if cur_e is None:
            continue
        row.update({"n": cur_e["n"], "w_in": cur_e["w_in"], "w_out": cur_e["w_out"], "d": cur_e["d"],
                    "t": cur_e["t"], "dw_obs": cur_e["dw_obs"], "cur_emp": cur_e["dw_pred"],
                    "cur_phys": cur_p["dw_pred"] if cur_p else None,
                    "end": cur_e["end"]})
        train = [o for o in obs_all if seg_start.get(o["seg"]) is not None and seg_start[o["seg"]] < t0]
        if train:
            try:
                fit, _ = calibrate_obs(train, p, half_life_days=half_life_days, report=False)
                fe = _seg_eval(test, replace(fit, model_kind="empirical"), arr)
                fp = _seg_eval(test, replace(fit, model_kind="physics"), arr)
                row["fc_emp"] = fe["dw_pred"] if fe else None
                row["fc_phys"] = fp["dw_pred"] if fp else None
            except ValueError:
                pass
        out.append(row)
    if progress:
        progress(len(order), len(order))

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
            f"перекалибруйте (раздел «Журнал проб» → «Калибровать модель») с весом свежих данных.")
