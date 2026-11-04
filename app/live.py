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

import numpy as np

from . import model as mdl
from .pages import JOURNAL, RECIRC


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
    silo_w: Optional[float] = None       # средневзвешенная по массе влажность ушедшего в приёмный силос (истинная), %
    silo_w_read: Optional[float] = None  # то же по показаниям выходного влагомера, %
    silo_n: int = 0                      # сколько проб выхода в это легло
    d_changed_at: Optional[datetime] = None   # когда в этом проходе последний раз меняли D (строка с новым D)
    d_prev: Optional[float] = None            # каким D было до этого
    last_sample: Optional[datetime] = None    # время последней пробы выхода
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
    prev_d = None
    for r in rs:
        if r.get("d_f") is not None:
            if prev_d is not None and abs(r["d_f"] - prev_d) > 1e-9:
                st.d_changed_at, st.d_prev = r["dt"], prev_d
            prev_d = r["d_f"]
        if r.get("w_out") is not None:
            st.last_sample = r["dt"]
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
    # влажность выхода по времени (истинная — с поправкой влагомера) — для средней влажности приёмного силоса
    rd = [(r["dt"], r["w_out"], mdl.true_from_read(p, r["w_out"], 0.5 * (r["t_a1"] + r["t_a2"])
                                                    if r.get("t_a1") is not None and r.get("t_a2") is not None else None))
          for r in rs if r.get("w_out") is not None and not r.get("recirc")]
    t_rd = [(t - rs[0]["dt"]).total_seconds() / 3600.0 for t, _, _ in rd]
    unloaded = silo_sum = silo_sum_read = 0.0
    stopped = False
    for a, b in zip(rs, rs[1:] + [None]):
        note = (a.get("note") or "").lower()
        if any(w in note for w in STOP_STEMS):
            stopped = True
        elif "запуск" in note:
            stopped = False
        t_next = b["dt"] if b is not None else end
        if a.get("d_f") is not None and not stopped and not a.get("recirc") and not a.get("burner_off"):
            dm = mdl.q_at(p, a["d_f"]) * max((t_next - a["dt"]).total_seconds(), 0.0) / 3600.0
            unloaded += dm
            if rd and dm > 0:
                ha = (a["dt"] - rs[0]["dt"]).total_seconds() / 3600.0
                hb = (t_next - rs[0]["dt"]).total_seconds() / 3600.0
                silo_sum += dm * 0.5 * float(np.interp(ha, t_rd, [x[2] for x in rd]) + np.interp(hb, t_rd, [x[2] for x in rd]))
                silo_sum_read += dm * 0.5 * float(np.interp(ha, t_rd, [x[1] for x in rd]) + np.interp(hb, t_rd, [x[1] for x in rd]))
    st.unloaded = unloaded
    if rd and unloaded > 0:
        st.silo_w, st.silo_w_read, st.silo_n = silo_sum / unloaded, silo_sum_read / unloaded, len(rd)
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
                      "Для рециркуляции — раздел «Досушка».")
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
    sigma_info: Optional[Dict] = None      # из чего сложился допуск конца партии (model.plan_sigma)
    w_goal: float = 0.0                    # на какую влажность подбирается D (цель прохода или поправленная по силосу)
    silo: Optional[Dict] = None            # выравнивание средней влажности приёмного силоса (см. live_forecast)
    t_grain_est: float = 0.0
    warnings: List[str] = field(default_factory=list)


def live_forecast(p: mdl.DryerParams, c: mdl.Conditions, st: LiveState, m_pass: float, w_target: float,
                  wk_final: float, soft: float, hard: float, use_corr: bool = True, cool: bool = True,
                  now: Optional[datetime] = None, d_override: Optional[float] = None, silo: bool = False,
                  silo_lim: float = 1.0) -> LiveForecast:
    """w_target — желаемая истинная влажность на выходе ЭТОГО прохода (средняя по приёмному силосу); wk_final —
    цель партии.

    silo=True — выравнивание средней влажности приёмного силоса: всё, что выгружено за проход, ложится в один силос,
    и важна его средняя влажность, а не каждая проба. Если часть уже ушла суше или сырее цели (например, в начале
    прохода выходил хвост прошлого), остатку назначается такая влажность, чтобы среднее по массе вышло в цель:
        W_нужно = (W_цель·M_всего − W_ушло·M_ушло − W_шахта·M_шахта) / M_новое,
    где M_шахта ≈ lag_k·вместимость — зерно, уже находящееся в шахте: оно выйдет со старым режимом (≈ W при текущем
    D), и подстроить можно только зерно, которое войдёт с этого момента (M_новое). W_нужно ограничивается
    W_цель ± silo_lim, чтобы силос не стал «полосатым»: сильно разная влажность слоёв плохо хранится."""
    fc = LiveForecast()
    corr = st.corr if use_corr else 0.0
    w_in = st.w_in
    d_now = st.d
    fc.w_out_now = mdl.solve_pass(p, c, w_in, 1.0, d_now).w_out + corr
    fc.w_out_now_read = mdl.read_from_true(p, fc.w_out_now, st.t_grain)
    w_goal = w_target
    if silo and st.silo_w is not None and st.unloaded > 1.0:
        m_total = m_pass * (100.0 - w_in) / (100.0 - w_target)
        m_done = st.unloaded
        m_rem = max(m_total - m_done, 0.0)
        m_tr = min(p.lag_k * p.m_dryer, m_rem)
        m_new = m_rem - m_tr
        fc.silo = {"m_total": m_total, "m_done": m_done, "w_done": st.silo_w, "w_done_read": st.silo_w_read,
                   "m_tr": m_tr, "w_tr": fc.w_out_now, "m_new": m_new, "need": None, "limited": False,
                   "late": m_new < max(0.05 * m_total, 5.0), "lim": silo_lim}
        if not fc.silo["late"]:
            need = (w_target * m_total - st.silo_w * m_done - fc.w_out_now * m_tr) / m_new
            w_goal = min(max(need, w_target - silo_lim), w_target + silo_lim)
            fc.silo.update(need=need, limited=abs(need - w_goal) > 0.05)
    fc.w_goal = w_goal
    raw = mdl.d_for_target(p, c, w_in, w_goal - corr)
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
    if fc.silo:
        sl = fc.silo
        new_part = fc.w_out_rec * sl["m_new"] if sl["m_new"] > 0 else 0.0
        sl["avg"] = (sl["w_done"] * sl["m_done"] + sl["w_tr"] * sl["m_tr"] + new_part) / max(
            sl["m_done"] + sl["m_tr"] + max(sl["m_new"], 0.0), 1e-9)
    q_new = mdl.q_at(p, d_use)
    fc.tau_h = p.lag_k * p.m_dryer / q_new if q_new > 0 else float("inf")
    t_now = max(now, st.last) if now else st.last
    fc.visible_at = t_now + timedelta(hours=fc.tau_h) if fc.tau_h < 1e6 else None
    # остаток прохода: масса на выходе прохода по ожидаемой влажности (с выравниванием — средняя по силосу)
    w_pass_out = fc.silo["avg"] if fc.silo else fc.w_out_rec
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
        # честный допуск остатка партии: проверка на журнале + разброс коэффициентов + расхождение моделей
        from dataclasses import replace as _replace
        sig, fc.sigma_info = mdl.plan_sigma(p, fc.rest_plan, c, mdl.other_model_final(p, fc.rest_plan, c))
        lo, hi = mdl.time_range(lambda pp: mdl.plan_by_passes(pp, c, fc.m_out_pass, w_pass_out, wk_final, n_rest,
                                                              start_rest, None, cool), _replace(p, rate_sigma=sig))
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


# ----------------------------------------------------------------------------------------------------------------
# Главное на экране «Текущий проход»: одно действие с D и почему
# ----------------------------------------------------------------------------------------------------------------

D_STEP_MIN = 3.0      # разница с советом меньше 3 с (≈ 0,5 п.п. на выходе) — D не менять (правила работы, 7.3)
D_STEP_BIG = 10.0     # больше 10 с — сначала проверить пробу входа, D и T блоков в журнале и задание


def live_advice(p: mdl.DryerParams, st: LiveState, fc: Optional[LiveForecast], d_try: Optional[float] = None,
                now: Optional[datetime] = None) -> Dict:
    """{"kind": "change" | "keep" | "wait" | "info" | "nodata", "head": крупная строка, "sub": [строки],
    "warn": [предупреждения], "go": раздел, куда отправить (или None)}.

    Правила работы (раздел 7.3), зашитые в совет:
      * совет отличается от текущего D меньше чем на 3 с — «D не менять»: это ≈ 0,5 п.п. на выходе, на уровне
        точности влагомера, а лишние перестановки раскачивают выход;
      * после смены D — ждать хотя бы половину времени «будет видно на выходе» (τ при текущем D), иначе следующая
        смена делается вслепую, по зерну, которое ещё шло со старым D;
      * совет требует изменения больше 10 с — сначала проверить последнюю пробу входа, D и T блоков в журнале.
    now — настоящее время (для «подождите до …»); без него — время последней записи."""
    if not st.ok or fc is None:
        go = RECIRC if st.burner_off else JOURNAL
        return {"kind": "nodata", "head": "Совета пока нет", "sub": [st.message or "Нет данных текущего прохода."],
                "warn": [], "go": go}
    warn = ([fc.d_note[0].upper() + fc.d_note[1:]] if fc.d_note else []) + [w[0].upper() + w[1:] for w in fc.warnings]
    read = mdl.read_from_true(p, fc.w_out_rec, st.t_grain)
    if st.recirc:
        return {"kind": "info", "head": "Идёт рециркуляция",
                "sub": [f"Из силоса не подаётся — сколько досушивать, смотрите в разделе «{RECIRC}»."], "warn": warn,
                "go": RECIRC}
    sl = fc.silo
    if sl and sl["late"]:
        return {"kind": "info", "head": f"D для этого прохода уже не менять — {st.d:.0f} с",
                "sub": [("До конца прохода выйдет только зерно, которое уже в шахте" if sl["m_tr"] >= 1 else
                         "Масса прохода уже выгружена")
                        + f": среднее по силосу ≈ {sl['avg']:.1f} %.", "Готовьте следующий проход."],
                "warn": warn, "go": None}
    if d_try:
        return {"kind": "info", "head": f"При D = {d_try:.0f} с на выходе ≈ {fc.w_out_rec:.1f} %",
                "sub": [f"Влагомер покажет ≈ {read:.1f} %. Подобранное для цели {fc.w_goal:.1f} %: D = {fc.d_rec:.0f} с."],
                "warn": warn, "go": None}
    diff = fc.d_rec - st.d
    t_now = max(now, st.last) if now and st.last else (now or st.last)
    q_cur = mdl.q_at(p, st.d)
    tau_cur = p.lag_k * p.m_dryer / q_cur if q_cur > 0 else 0.0
    if abs(diff) < D_STEP_MIN:
        w_now_read = fc.w_out_now_read
        return {"kind": "keep", "head": f"D не менять — {st.d:.0f} с",
                "sub": [f"На выходе ≈ {fc.w_out_now:.1f} % (влагомер ≈ {w_now_read:.1f} %), цель {fc.w_goal:.1f} %.",
                        f"Совет {fc.d_rec:.0f} с отличается меньше чем на {D_STEP_MIN:.0f} с — это ≈ 0,5 п.п. на "
                        f"выходе, на уровне точности влагомера."],
                "warn": warn, "go": None}
    if st.d_changed_at is not None and t_now is not None:
        ready = st.d_changed_at + timedelta(hours=tau_cur / 2.0)
        if t_now < ready:
            seen = st.d_changed_at + timedelta(hours=tau_cur)
            return {"kind": "wait", "head": f"Подождите до {ready:%H:%M} — D не трогать",
                    "sub": [f"D меняли в {st.d_changed_at:%H:%M} ({st.d_prev:.0f} → {st.d:.0f} с); на выходе это станет "
                            f"видно к {seen:%H:%M}, а пока выходит зерно, которое сушилось при старом D.",
                            f"Совет сейчас: D = {fc.d_rec:.0f} с — проверьте его после {ready:%H:%M}."],
                    "warn": warn, "go": None}
    sub = [f"Сейчас {st.d:.0f} с. На выходе станет ≈ {fc.w_out_rec:.1f} % (влагомер ≈ {read:.1f} %), "
           f"цель {fc.w_goal:.1f} %."]
    if fc.visible_at:
        sub.append(f"Заметно на выходе через {fc.tau_h:.1f} ч — к {fc.visible_at:%H:%M}. После смены запишите её: "
                   f"«Изменить режим…» в журнале.")
    if abs(diff) > D_STEP_BIG:
        sub.append("Большое изменение: сначала проверьте последнюю пробу входа, D и T блоков в журнале и задание "
                   "слева; всё верно — меняйте, сомневаетесь — позвоните инженеру.")
    return {"kind": "change", "head": f"Поставьте D = {fc.d_rec:.0f} с", "sub": sub, "warn": warn, "go": None}
