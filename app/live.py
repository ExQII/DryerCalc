"""Режим «по факту»: состояние текущего прохода по журналу, прогноз окончания и подсказка разгрузки D.

* Текущий проход — последний участок журнала с пробами вместе с предыдущими участками того же прохода:
  участки, разделённые только остановкой/пуском или перерывом, при тех же «Партия» и «Проход» — один проход
  (calibration.pass_of_segment). Новый проход начинают «Закончился …», «открыли с …», смена партии/прохода.
* Выгружено с начала прохода = Σ Q(D) · Δt по строкам журнала (D переносится вперёд, как в журнале); время от
  строки «Остановка» до строки «Запуск» не считается, рециркуляция и охлаждение тоже.
* Поправка по факту: модель сравнивается с последними (до 6, то есть ~3 ч) замерами выхода этого прохода
  (пары «вход в t − τ / выход в t»); средняя разница, ослабленная коэффициентом n/(n+3), прибавляется к
  прогнозу. Так учитывается то, чего модель не знает (погода, партия, состояние сушилки), но один
  случайный замер прогноз не дёргает.
* Изменение D проявится на выходе через τ = lag_k · m_сушилки / Q(D_нов): зерно, которое уже в шахте,
  выходит со старой скоростью. Менять D чаще, чем раз в τ/2, бессмысленно — результат ещё не виден.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from . import model as mdl


@dataclass
class LiveState:
    ok: bool = False
    message: str = ""
    seg: int = -1
    start: Optional[datetime] = None
    last: Optional[datetime] = None
    w_in: Optional[float] = None
    w_out_read: Optional[float] = None
    d: Optional[float] = None
    t_proc: Optional[float] = None
    t_grain: Optional[float] = None
    t_amb: Optional[float] = None
    rh: Optional[float] = None
    batch: str = ""
    pass_no: Optional[float] = None
    unloaded: float = 0.0
    hours: float = 0.0
    corr: float = 0.0
    corr_n: int = 0
    recirc: bool = False
    burner_off: bool = False
    series: List[Tuple[datetime, Optional[float], Optional[float]]] = field(default_factory=list)  # (t, вход, выход)


def live_state(rows: List[Dict], p: mdl.DryerParams, now: Optional[datetime] = None, weather=None,
               corr_window: int = 6) -> LiveState:
    from .calibration import STOP_STEMS, _t_proc, observe_all, pass_of_segment, prepare
    st = LiveState()
    if weather is not None:
        rows = weather.fill_rows(rows)
    prep, reliable = prepare(rows)
    if not prep:
        st.message = "Журнал пуст."
        return st
    if not reliable:
        st.message = "В журнале не распознано время проб — режим «по факту» работает только с временем."
        return st
    data_segs = [r["seg"] for r in prep if r.get("w_in") is not None or r.get("w_out") is not None]
    if not data_segs:
        st.message = "В журнале нет замеров влажности."
        return st
    seg = data_segs[-1]
    pass_of = pass_of_segment(prep)
    chain = {s for s, first in pass_of.items() if first == pass_of[seg]}      # участки этого прохода
    rs = [r for r in prep if r["seg"] in chain and r.get("dt") is not None]
    if not rs:
        st.message = "У последнего прохода нет времени проб."
        return st
    st.seg = pass_of[seg]
    st.start, st.last = rs[0]["dt"], rs[-1]["dt"]
    for r in rs:
        if r.get("w_in") is not None:
            st.w_in = r["w_in"]
        if r.get("w_out") is not None:
            st.w_out_read = r["w_out"]
        if r.get("d_f") is not None:
            st.d = r["d_f"]
        tp = _t_proc(r, p.temp_source)
        if tp is not None:
            st.t_proc = tp
        if r.get("t_a1") is not None and r.get("t_a2") is not None:
            st.t_grain = 0.5 * (r["t_a1"] + r["t_a2"])
        if r.get("t_amb") is not None:
            st.t_amb = r["t_amb"]
        if r.get("rh") is not None:
            st.rh = r["rh"]
        st.batch = r.get("batch") or st.batch
        st.pass_no = r.get("pass_no") if r.get("pass_no") is not None else st.pass_no
        st.series.append((r["dt"], r.get("w_in"), r.get("w_out")))
    end = max(now, st.last) if now else st.last
    unloaded = 0.0
    stopped = False
    for a, b in zip(rs, rs[1:] + [None]):
        note = (a.get("note") or "").lower()
        if any(w in note for w in STOP_STEMS):
            stopped = True
        elif "запуск" in note:
            stopped = False
        t_next = b["dt"] if b is not None else end
        if a.get("d_f") is not None and not stopped and not a.get("recirc") and not a.get("burner_off"):
            unloaded += mdl.q_at(p, a["d_f"]) * max((t_next - a["dt"]).total_seconds(), 0.0) / 3600.0
    st.unloaded = unloaded
    st.recirc = bool(rs[-1].get("recirc"))
    st.burner_off = bool(rs[-1].get("burner_off"))
    st.hours = (end - st.start).total_seconds() / 3600.0
    # поправка по последним наблюдениям этого прохода
    seg_rows = [rows[i] for i, r in enumerate(prep) if r["seg"] in chain]
    obs, _, _ = observe_all(seg_rows, p)
    obs = [o for o in obs if o.get("dt") is not None][-corr_window:]
    if obs:
        res = []
        for o in obs:
            pred = mdl.solve_pass(p, mdl.Conditions(o["t_proc"], o["t_amb"], o["rh"]), o["w_in"], 1.0, o["d"]).w_out
            res.append(o["w_out"] - pred)
        # среднее с усадкой к нулю: по 1–2 замерам поправка почти не действует, по 6 — на 2/3
        st.corr = sum(res) / (len(res) + 3.0)
        st.corr_n = len(res)
    if st.burner_off:
        st.message = ("Горелки выключены (в журнале «охлаждение» / «откл. т.б.»): проход закончен, идёт охлаждение. "
                      "Для рециркуляции — вкладка «Рециркуляция».")
        return st
    if st.w_in is None or st.d is None or st.t_proc is None:
        st.message = "В текущем проходе не хватает данных: нужны влага на входе, разгрузка D и температура."
        return st
    st.ok = True
    return st


@dataclass
class LiveForecast:
    w_out_now: float = 0.0          # истинная на выходе при текущем D (с поправкой)
    w_out_now_read: float = 0.0     # показание влагомера
    d_rec: float = 0.0
    d_rec_raw: float = 0.0
    d_note: str = ""
    w_out_rec: float = 0.0
    tau_h: float = 0.0
    visible_at: Optional[datetime] = None
    m_out_pass: float = 0.0
    remaining: float = 0.0
    pass_end: Optional[datetime] = None
    rest_plan: Optional[mdl.Plan] = None
    batch_end: Optional[datetime] = None
    batch_end_range: Optional[Tuple[datetime, datetime]] = None   # 90 % по ошибке скорости сушки
    t_grain_est: float = 0.0
    warnings: List[str] = field(default_factory=list)


def live_forecast(p: mdl.DryerParams, c: mdl.Conditions, st: LiveState, m_pass: float, w_target: float,
                  wk_final: float, soft: float, hard: float, use_corr: bool = True, cool: bool = True,
                  now: Optional[datetime] = None, d_override: Optional[float] = None) -> LiveForecast:
    """w_target — желаемая истинная влажность на выходе ЭТОГО прохода; wk_final — цель партии."""
    fc = LiveForecast()
    corr = st.corr if use_corr else 0.0
    w_in = st.w_in
    d_now = st.d
    fc.w_out_now = mdl.solve_pass(p, c, w_in, 1.0, d_now).w_out + corr
    fc.w_out_now_read = mdl.read_from_true(p, fc.w_out_now, st.t_grain)
    raw = mdl.d_for_target(p, c, w_in, w_target - corr)
    fc.d_rec_raw = raw
    if raw > 100.5:
        fc.d_rec = 100.0
        fc.d_note = "даже при D = 100 снимется больше, чем нужно — цель прохода можно поставить ниже"
    elif raw < 1.0:
        fc.d_rec = 1.0
        fc.d_note = "сушилка не успевает: даже при D = 1 цель прохода не достигается"
    else:
        fc.d_rec = float(round(raw))
    d_use = d_override if d_override else fc.d_rec
    fc.w_out_rec = mdl.solve_pass(p, c, w_in, 1.0, d_use).w_out + corr
    q_new = mdl.q_at(p, d_use)
    fc.tau_h = p.lag_k * p.m_dryer / q_new if q_new > 0 else float("inf")
    t_now = max(now, st.last) if now else st.last
    fc.visible_at = t_now + timedelta(hours=fc.tau_h) if fc.tau_h < 1e6 else None
    # остаток прохода: масса на выходе прохода по ожидаемой влажности
    w_pass_out = fc.w_out_rec
    fc.m_out_pass = m_pass * (100.0 - w_in) / (100.0 - w_pass_out) if w_pass_out < 100 else m_pass
    fc.remaining = max(fc.m_out_pass - st.unloaded, 0.0)
    fc.pass_end = t_now + timedelta(hours=fc.remaining / q_new) if q_new > 0 else None
    # остальные проходы до цели партии
    if fc.pass_end and w_pass_out > wk_final + 0.05:
        plans = mdl.scenarios(p, c, fc.m_out_pass, w_pass_out, wk_final, 6,
                              fc.pass_end + timedelta(hours=p.overhead_h), None, cool)
        k = mdl.recommend(plans, soft, hard)
        fc.rest_plan = plans[k] if k is not None else plans[-1]
        fc.batch_end = fc.rest_plan.end
        n_rest = len(fc.rest_plan.passes)
        start_rest = fc.pass_end + timedelta(hours=p.overhead_h)
        lo, hi = mdl.time_range(lambda pp: mdl.plan_by_passes(pp, c, fc.m_out_pass, w_pass_out, wk_final, n_rest,
                                                              start_rest, None, cool), p)
        fc.batch_end_range = (start_rest + timedelta(hours=lo), start_rest + timedelta(hours=hi))
    elif fc.pass_end:
        cool_h = fc.m_out_pass / mdl.q_full(p) if cool and mdl.q_full(p) > 0 else 0.0
        fc.batch_end = fc.pass_end + timedelta(hours=(p.overhead_h if cool else 0.0) + cool_h)
    fc.t_grain_est = mdl.grain_temp(p, c.t_proc, w_in)
    if p.t_agent_max and c.t_proc > p.t_agent_max:
        fc.warnings.append(f"T блоков {c.t_proc:.0f} °C выше заданного ограничения {p.t_agent_max:.0f} °C")
    tg = st.t_grain if st.t_grain is not None else fc.t_grain_est
    if p.t_grain_max and tg > p.t_grain_max:
        fc.warnings.append(f"T зерна {tg:.0f} °C выше ограничения {p.t_grain_max:.0f} °C")
    dw = w_in - fc.w_out_rec
    if dw > hard + 1e-9:
        fc.warnings.append(f"съём за проход {dw:.1f} п.п. больше допустимого {hard:g}")
    return fc
