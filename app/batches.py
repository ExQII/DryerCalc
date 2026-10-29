"""Партии: сверка с весовой, топливо, тонно-проценты, сводка по журналу; смешивание и очередь партий.

Термины
-------
* Сухое вещество (с.в.) сохраняется при сушке: m · (100 − W) = const. Отсюда ожидаемая масса после сушки
      m_вых = (m_вх − отходы) · (100 − W_вх) / (100 − W_вых).
* Если весовая показала другую массу, то влажность, при которой массы сходятся,
      W_вых(по массам) = 100 − (m_вх − отходы) · (100 − W_вх) / m_вых.
  Расхождение с лабораторной W_вых — это либо систематическая ошибка влагомеров, либо неучтённые потери
  (пыль, аспирация, просыпи). Разделить их по одной партии нельзя; по нескольким видно, есть ли устойчивый сдвиг.
* Тонно-процент — снижение влажности 1 т зерна на 1 %: (m_вх − отходы) · (W_вх − W_вых), т·%.
* Удельный расход топлива — на 1 т испарённой воды; так он мало зависит от влажности и массы партии.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from . import model as mdl

BATCH_FIELDS = ["name", "start", "end", "m_in", "w_in", "m_out", "w_out", "waste", "fuel", "fuel_price",
                "purpose", "cracks", "broken", "test_weight", "germ", "note"]
BATCH_HEADERS = {
    "name": "Партия", "start": "Начало", "end": "Конец", "m_in": "Масса вх, т (весовая)",
    "w_in": "W вх, % (лаб.)", "m_out": "Масса вых, т (весовая)", "w_out": "W вых, % (лаб.)",
    "waste": "Отходы, т", "fuel": "Топливо, ед.", "fuel_price": "Цена ед., ₽",
    "purpose": "Назначение", "cracks": "Трещиноватость, %", "broken": "Битое/дроблёное, %",
    "test_weight": "Натура, г/л", "germ": "Всхожесть, %", "note": "Примечание",
}
BATCH_TEXT = {"name", "start", "end", "purpose", "note"}
QUALITY_FIELDS = ["cracks", "broken", "test_weight", "germ"]
L_EVAP_MJ = 2.5   # МДж на 1 кг воды, испаряемой из зерна (теплота испарения ≈ 2,4 + теплота связи)


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ".").replace(" ", ""))
    except ValueError:
        return None


def parse_when(s: str) -> Optional[datetime]:
    from .weather import parse_dt
    return parse_dt(s) if s else None


# ----------------------------------------------------------------------------
# Баланс, топливо, тонно-проценты
# ----------------------------------------------------------------------------

def balance(b: Dict) -> Dict:
    """Сверка партии по сухому веществу. Все поля могут отсутствовать — считается то, что можно."""
    m_in, w_in, m_out, w_out = _f(b.get("m_in")), _f(b.get("w_in")), _f(b.get("m_out")), _f(b.get("w_out"))
    waste = _f(b.get("waste")) or 0.0
    res: Dict = {}
    if m_in is None or w_in is None:
        return res
    m_net = max(m_in - waste, 0.0)
    dm = m_net * (100.0 - w_in) / 100.0
    res["dm"] = dm
    if w_out is not None:
        m_exp = dm * 100.0 / (100.0 - w_out)
        res["m_out_expected"] = m_exp
        res["water_by_moisture"] = m_net - m_exp
        res["shrink_formula_pct"] = 100.0 * (w_in - w_out) / (100.0 - w_out)   # убыль массы по формуле, %
        res["ton_percent"] = m_net * (w_in - w_out)
    if m_out is not None and m_out > 0:
        res["water_by_mass"] = m_net - m_out
        res["shrink_actual_pct"] = 100.0 * (m_net - m_out) / m_net if m_net > 0 else None
        res["w_out_by_mass"] = 100.0 - dm * 100.0 / m_out
        if w_out is not None:
            res["m_diff"] = m_out - res["m_out_expected"]
            res["m_diff_pct"] = 100.0 * res["m_diff"] / res["m_out_expected"]
            res["w_diff"] = res["w_out_by_mass"] - w_out
            if "ton_percent" not in res:
                res["ton_percent"] = m_net * (w_in - w_out)
    if "ton_percent" not in res and "w_out_by_mass" in res:
        res["ton_percent"] = m_net * (w_in - res["w_out_by_mass"])
    return res


def water_removed(b: Dict) -> Optional[float]:
    """Испарённая вода, т: по весовой, если есть масса после сушки, иначе по влажности."""
    bal = balance(b)
    return bal.get("water_by_mass", bal.get("water_by_moisture"))


def fuel_metrics(b: Dict, lhv: Optional[float] = None) -> Dict:
    """Топливо на единицу результата; lhv — теплота сгорания, МДж на единицу топлива (для КПД)."""
    fuel, price = _f(b.get("fuel")), _f(b.get("fuel_price"))
    bal = balance(b)
    water = water_removed(b)
    out: Dict = {}
    if fuel is None or fuel <= 0:
        return out
    out["fuel"] = fuel
    if water and water > 0:
        out["per_water"] = fuel / water
        if lhv and lhv > 0:
            out["energy_gj"] = fuel * lhv / 1000.0
            out["mj_per_kg"] = fuel * lhv / (water * 1000.0)          # МДж на 1 кг испарённой воды
            out["thermal_eff"] = L_EVAP_MJ / out["mj_per_kg"]
    tp = bal.get("ton_percent")
    if tp and tp > 0:
        out["per_ton_percent"] = fuel / tp
    m_out = _f(b.get("m_out")) or bal.get("m_out_expected")
    if m_out and m_out > 0:
        out["per_ton_dried"] = fuel / m_out
    if price and price > 0:
        out["cost"] = fuel * price
        if water and water > 0:
            out["cost_per_water"] = fuel * price / water
        if tp and tp > 0:
            out["cost_per_ton_percent"] = fuel * price / tp
        if m_out and m_out > 0:
            out["cost_per_ton_dried"] = fuel * price / m_out
    return out


def fuel_rate_from_batches(batches: Sequence[Dict]) -> Tuple[Optional[float], int]:
    """Удельный расход топлива на 1 т воды по всем партиям, где есть топливо и вода: Σ топлива / Σ воды."""
    fs, ws, n = 0.0, 0.0, 0
    for b in batches:
        fuel = _f(b.get("fuel"))
        water = water_removed(b)
        if fuel and fuel > 0 and water and water > 0:
            fs += fuel
            ws += water
            n += 1
    return (fs / ws if ws > 0 else None), n


def season_meter_shift(batches: Sequence[Dict]) -> Tuple[Optional[float], Optional[float], int]:
    """Средний сдвиг «W по массам − W лаб.» по партиям и его стандартная ошибка."""
    ds = [balance(b).get("w_diff") for b in batches]
    ds = [d for d in ds if d is not None]
    if not ds:
        return None, None, 0
    m = sum(ds) / len(ds)
    if len(ds) < 2:
        return m, None, 1
    sd = math.sqrt(sum((d - m) ** 2 for d in ds) / (len(ds) - 1))
    return m, sd / math.sqrt(len(ds)), len(ds)


# ----------------------------------------------------------------------------
# Сводка по журналу за время партии
# ----------------------------------------------------------------------------

def journal_summary(rows: List[Dict], p: mdl.DryerParams, start: Optional[datetime], end: Optional[datetime]
                    ) -> Dict:
    """Проходы журнала между start и end: длительность, средние W, D, T, съём."""
    from .calibration import _t_proc, prepare
    prep, reliable = prepare(rows)
    if not reliable:
        return {"passes": [], "hours": 0.0, "note": "в журнале не распознано время"}
    sel = [r for r in prep if r.get("dt") is not None and (start is None or r["dt"] >= start)
           and (end is None or r["dt"] <= end)]
    segs: Dict[int, List[Dict]] = {}
    for r in sel:
        segs.setdefault(r["seg"], []).append(r)
    passes = []
    for s, lst in sorted(segs.items()):
        data = [r for r in lst if r.get("w_in") is not None or r.get("w_out") is not None]
        if len(data) < 2:
            continue
        t0, t1 = data[0]["dt"], data[-1]["dt"]
        hours = (t1 - t0).total_seconds() / 3600.0
        win = [r["w_in"] for r in data if r.get("w_in") is not None]
        wout = [r["w_out"] for r in data if r.get("w_out") is not None]
        ds = [r["d_f"] for r in data if r.get("d_f") is not None]
        ts = [x for x in (_t_proc(r, p.temp_source) for r in data) if x is not None]
        unloaded = 0.0
        for a, b in zip(data, data[1:]):
            if a.get("d_f") is not None and not a.get("recirc") and not a.get("burner_off"):
                unloaded += mdl.q_at(p, a["d_f"]) * (b["dt"] - a["dt"]).total_seconds() / 3600.0
        w_in_avg = sum(win) / len(win) if win else None
        w_out_avg = sum(wout) / len(wout) if wout else None
        tgs = [0.5 * (r["t_a1"] + r["t_a2"]) for r in data if r.get("t_a1") is not None and r.get("t_a2") is not None]
        tg_avg = sum(tgs) / len(tgs) if tgs else None
        w_true = mdl.true_from_read(p, w_out_avg, tg_avg) if w_out_avg is not None else None
        passes.append({
            "seg": s, "start": t0, "end": t1, "hours": hours, "n": len(data),
            "w_in": w_in_avg, "w_out_read": w_out_avg, "w_out": w_true,
            "dw": (w_in_avg - w_true) if (w_in_avg is not None and w_true is not None) else None,
            "d": sum(ds) / len(ds) if ds else None, "t": sum(ts) / len(ts) if ts else None,
            "t_grain_max": max(tgs) if tgs else None, "t_grain": tg_avg,
            "unloaded": unloaded, "batch": lst[0].get("batch") or "", "pass_no": lst[0].get("pass_no"),
        })
    return {"passes": passes, "hours": sum(x["hours"] for x in passes)}


def batches_from_journal(rows: List[Dict]) -> List[Dict]:
    """Черновики партий по колонке «Партия» журнала: имя, начало и конец по времени строк."""
    from .calibration import assign_times
    times, reliable = assign_times(rows)
    spans: Dict[str, List[datetime]] = {}
    order: List[str] = []
    for r, dt in zip(rows, times):
        name = (r.get("batch") or "").strip()
        if not name or dt is None:
            continue
        if name not in spans:
            spans[name] = [dt, dt]
            order.append(name)
        spans[name][0] = min(spans[name][0], dt)
        spans[name][1] = max(spans[name][1], dt)
    out = []
    for name in order:
        a, b = spans[name]
        out.append({"name": name, "start": a.strftime("%d.%m.%Y %H:%M"), "end": b.strftime("%d.%m.%Y %H:%M")})
    return out if reliable else []


# ----------------------------------------------------------------------------
# Смешивание и очередь
# ----------------------------------------------------------------------------

def mix(lots: Sequence[Tuple[float, float]]) -> Tuple[float, float]:
    """Смесь партий [(масса, W %)] -> (масса, W %). Вода суммируется: W = Σ m·W / Σ m."""
    lots = [(m, w) for m, w in lots if m and m > 0 and w is not None]
    total = sum(m for m, _ in lots)
    if total <= 0:
        return 0.0, 0.0
    return total, sum(m * w for m, w in lots) / total


@dataclass
class QueueEntry:
    name: str
    mass: float
    w0: float
    wk: float
    ready: Optional[datetime]
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    plan: Optional[mdl.Plan] = None
    wait_h: float = 0.0
    note: str = ""
    passes: int = 0
    max_dw: float = 0.0


def schedule(items: Sequence[Dict], p: mdl.DryerParams, c: mdl.Conditions, start: datetime, method: str = "model",
             soft: float = 6.0, hard: float = 10.0, q: float = 18.2, lim: float = 6.0, cool: bool = True,
             changeover_h: float = 0.0, t_list: Optional[Sequence[float]] = None) -> List[QueueEntry]:
    """Партии по очереди на одной сушилке. Партия занимает сушилку на все свои проходы.

    method = "model": рекомендованный сценарий модели (минимум проходов в лимите съёма);
    method = "q": по производительности q т/ч и лимиту съёма lim (как вкладка «По производительности»).
    """
    out: List[QueueEntry] = []
    free = start
    for it in items:
        name = str(it.get("name") or f"Партия {len(out) + 1}")
        mass, w0, wk = _f(it.get("mass")), _f(it.get("w0")), _f(it.get("wk"))
        ready = parse_when(str(it.get("ready") or "")) if it.get("ready") else None
        e = QueueEntry(name=name, mass=mass or 0.0, w0=w0 or 0.0, wk=wk or 0.0, ready=ready)
        if not mass or not w0 or not wk or w0 <= wk:
            e.note = "нет массы/влажности или W₀ ≤ Wк"
            out.append(e)
            continue
        begin = max(free + timedelta(hours=changeover_h) if out else free, ready or free)
        e.wait_h = (begin - free).total_seconds() / 3600.0 if out else 0.0
        if method == "q":
            n = max(1, int(math.ceil((w0 - wk) / max(lim, 0.1) - 1e-9)))
            plan = mdl.plan_simple(p, mass, w0, wk, [q], n, begin, cool)
        else:
            plans = mdl.scenarios(p, c, mass, w0, wk, 6, begin, t_list, cool)
            k = mdl.recommend(plans, soft, hard)
            plan = plans[k] if k is not None else plans[-1]
            if k is None:
                e.note = f"в лимит {hard:g} п.п. не уложиться — взят вариант {len(plan.passes)} прох."
        e.plan = plan
        e.start, e.end = begin, plan.end
        e.passes = len(plan.passes)
        e.max_dw = plan.max_dw
        free = plan.end
        out.append(e)
    return out


# ----------------------------------------------------------------------------
# Качество зерна и режим сушки
# ----------------------------------------------------------------------------

QUALITY_NAMES = {"cracks": "Трещиноватость, %", "broken": "Битое/дроблёное, %", "test_weight": "Натура, г/л",
                 "germ": "Всхожесть, %"}
REGIME_NAMES = {"max_dw": "Макс. съём за проход, п.п.", "t_mean": "Средняя T блоков, °C",
                "tg_max": "Макс. T зерна, °C", "passes": "Проходов", "total_dw": "Общий съём, п.п."}


def quality_table(batches: Sequence[Dict], rows: List[Dict], p: mdl.DryerParams) -> List[Dict]:
    """По каждой партии: показатели качества и режим по журналу за её время."""
    out = []
    for b in batches:
        start, end = parse_when(b.get("start") or ""), parse_when(b.get("end") or "")
        js = journal_summary(rows, p, start, end) if (start or end) else {"passes": []}
        ps = [x for x in js.get("passes", []) if x.get("dw") is not None]
        rec = {"name": b.get("name") or "", "purpose": b.get("purpose") or ""}
        for k in QUALITY_FIELDS:
            rec[k] = _f(b.get(k))
        rec["passes"] = len(ps) or None
        rec["max_dw"] = max(x["dw"] for x in ps) if ps else None
        ts = [x["t"] for x in ps if x.get("t") is not None]
        rec["t_mean"] = sum(ts) / len(ts) if ts else None
        tgs = [x["t_grain_max"] for x in ps if x.get("t_grain_max") is not None]
        rec["tg_max"] = max(tgs) if tgs else None
        w_in, w_out = _f(b.get("w_in")), _f(b.get("w_out"))
        rec["total_dw"] = (w_in - w_out) if (w_in is not None and w_out is not None) else None
        out.append(rec)
    return out


def correlation(xs: Sequence[float], ys: Sequence[float]) -> Optional[Tuple[float, float, int]]:
    """(r Пирсона, наклон МНК, n) или None, если точек < 3 или нет разброса."""
    pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pts)
    if n < 3:
        return None
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    syy = sum((y - my) ** 2 for _, y in pts)
    sxy = sum((x - mx) * (y - my) for x, y in pts)
    if sxx <= 1e-12 or syy <= 1e-12:
        return None
    return sxy / math.sqrt(sxx * syy), sxy / sxx, n


def quality_findings(table: Sequence[Dict], top: int = 6) -> List[str]:
    """Текстовые выводы: самые сильные связи качества с режимом.

    Связи оцениваются только при ≥ 5 партиях с данными (по 3–4 точкам коэффициент r случаен);
    |r| ≥ 0,7 — сильная, ≥ 0,5 — заметная, меньше — не показываются."""
    found = []
    n_max = 0
    for qk, qn in QUALITY_NAMES.items():
        for rk, rn in REGIME_NAMES.items():
            res = correlation([t[rk] for t in table], [t[qk] for t in table])
            if res is None:
                continue
            r, slope, n = res
            n_max = max(n_max, n)
            if n >= 5 and abs(r) >= 0.5:
                found.append((abs(r), f"{qn} ↔ {rn}: r = {r:+.2f}, {slope:+.2f} на единицу, партий {n} — "
                                      f"{'сильная' if abs(r) >= 0.7 else 'заметная'} связь"))
    if n_max < 5:
        return [f"Партий с качеством и журналом: {n_max}. Связи оцениваются с 5 партий — по 3–4 точкам "
                f"коэффициент связи случаен. График справа — для наглядности."]
    if not found:
        return ["Заметных связей качества с режимом (|r| ≥ 0,5) не видно."]
    return [t for _, t in sorted(found, reverse=True)[:top]]
