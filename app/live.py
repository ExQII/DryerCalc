"""Режим «по факту»: состояние текущего прохода по журналу, прогноз окончания и подсказка разгрузки D.

* Текущий проход — проход последней пробы журнала (calibration.journal_passes): от подачи из силоса до
  рециркуляции, сколько бы силосов («Закончился 2 с., сушим с 1 с.»), остановок и перерывов в нём ни было. Новый
  проход начинается, когда после рециркуляции (или охлаждения) снова пошла подача, или со сменой «Партия» /
  «Проход». Рециркуляция, когда выход уже у цели партии (≤ Wк + 1 п.п.), — досушка и конец партии.
* Выгружено с начала прохода = Σ Q(D) · Δt по строкам журнала (D переносится вперёд, как в журнале); время от
  строки «Остановка» до строки «Запуск» не считается, рециркуляция и охлаждение тоже.
* Поправка по факту: модель сравнивается с последними (до 6, то есть ~3 ч) замерами выхода этого прохода
  (пары «вход в t − τ / выход в t»); средняя разница, ослабленная коэффициентом n/(n+3), прибавляется к
  прогнозу. Так учитывается то, чего модель не знает (погода, партия, состояние сушилки), но один
  случайный замер прогноз не дёргает.
* Изменение D проявится на выходе через τ = lag_k · m_сушилки / Q(D_нов): зерно, которое уже в шахте,
  выходит со старой скоростью. Менять D чаще, чем раз в τ/2, бессмысленно — результат ещё не виден.
* В реальном времени (now): выгрузка досчитывается от последней строки журнала до текущего момента при последнем
  D — но не дальше live_cap_h после последней записи: дольше без записей — возможно, остановка, которую не
  записали, и счёт останавливается (frozen). Журнал, который не ведётся больше OLD_JOURNAL_H, считается на момент
  последней записи — текущее время к нему не относится.
* Масса на входе прохода — по сухому веществу (pass_mass): m_k = M₀·(100 − W₁)/(100 − W_k), где M₀ — масса партии,
  W₁ — средняя влажность на входе 1-го прохода партии по журналу (нет в журнале — W₀ партии), W_k — средняя на
  входе текущего прохода.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import model as mdl
from .pages import JOURNAL, RECIRC

OLD_JOURNAL_H = 12.0      # журнал не ведётся дольше — «текущее время» к нему не относится


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
    pass_auto: bool = False              # номер прохода посчитан программой (в журнале «Проход» не вписан)
    batch_no: int = 1                    # номер партии по счёту в журнале
    end_kind: Optional[str] = None       # подача прохода закончена: "recirc" / "cool"; None — подача идёт
    batch_end: bool = False              # партия высушена: досушка (рециркуляция у цели) или охлаждение
    w_end: Optional[float] = None        # истинная влажность выхода в конце подачи, %
    recirc_since: Optional[datetime] = None   # с какого времени рециркуляция
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
    end: Optional[datetime] = None            # до какого момента досчитана выгрузка (последняя запись или «сейчас»)
    frozen: bool = False                      # «сейчас», но записей давно нет — счёт остановлен на last + live_cap_h
    batch_passes: List[Dict] = field(default_factory=list)   # проходы этой партии до текущего: № и средняя W входа
    regime: List[Tuple] = field(default_factory=list)   # весь журнал: (время, Q, T, идёт ли выгрузка, D) от строки дальше
    inlet: List[Tuple[datetime, float]] = field(default_factory=list)   # весь журнал: (время, влажность на входе)
    series: List[Tuple[datetime, Optional[float], Optional[float]]] = field(default_factory=list)  # (t, вход, выход)


def live_state(rows: List[Dict], p: mdl.DryerParams, now: Optional[datetime] = None, weather=None,
               corr_window: int = 6, final_w: Optional[float] = None, live_cap_h: float = 1.5) -> LiveState:
    """final_w — выше какой влажности выхода (истинной) рециркуляция — конец прохода, а не досушка партии
    (по умолчанию calibration.FINAL_W = 15 %; раздел «Текущий проход» передаёт Wк + 1 п.п.).
    now — считать выгрузку до этого момента (реальное время), но не дальше live_cap_h после последней записи."""
    from .calibration import FINAL_W, STOP_STEMS, _t_proc, journal_passes, observe_all, pass_of_segment, prepare
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
    jp = journal_passes(prep, p, FINAL_W if final_w is None else final_w)
    info = next((x for x in jp if seg in x["segs"]), None)
    if info is not None:
        # проходы этой партии до текущего: средняя влажность на входе (подача — то, что вышло с прошлого прохода)
        mine = [x for x in jp if x["batch_no"] == info["batch_no"]]
        for x in mine[: mine.index(info) + 1]:
            wins = [r["w_in"] for r in prep if r.get("pass", 0) == x["pass"] and r.get("w_in") is not None
                    and not r.get("recirc") and not r.get("burner_off")]
            st.batch_passes.append({"pass_no": x["pass_no"], "w_in": sum(wins) / len(wins) if wins else None,
                                    "n_in": len(wins), "w_end": x["w_end"], "start": x["start"]})
        st.batch, st.pass_no, st.pass_auto = info["batch"], info["pass_no"], info["pass_auto"]
        st.batch_no, st.end_kind, st.w_end = info["batch_no"], info["end_kind"], info["w_end"]
        st.batch_end = bool(info["batch_end"] and info["end_kind"])
    # начало прохода — первая строка, где сушилка работает (проход может начаться строкой «Остановка» после
    # рециркуляции прошлого прохода)
    st.start = next((r["dt"] for r in rs if r.get("d_f") is not None and not r.get("recirc")
                     and not r.get("burner_off") and not any(w in (r.get("note") or "").lower() for w in STOP_STEMS)),
                    rs[0]["dt"])
    st.last = rs[-1]["dt"]
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
        if r.get("recirc") and st.recirc_since is None:
            st.recirc_since = r["dt"]
        st.series.append((r["dt"], r.get("w_in"), r.get("w_out")))
    end = st.last
    if now is not None and now > st.last:
        gap_h = (now - st.last).total_seconds() / 3600.0
        if gap_h <= live_cap_h:
            end = now
        elif gap_h <= OLD_JOURNAL_H:          # записей давно нет: возможно, остановка, которую не записали
            end, st.frozen = st.last + timedelta(hours=live_cap_h), True
    st.end = end
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
    st.regime, st.inlet = _journal_regime(prep, p)
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


def pass_mass(m0: float, w0: Optional[float], passes: List[Dict]) -> Tuple[Optional[float], str]:
    """Масса на входе текущего прохода по сухому веществу: m = M₀·(100 − W₁)/(100 − W_k).

    passes — LiveState.batch_passes (проходы партии до текущего включительно). W₁ — средняя на входе 1-го прохода
    партии по журналу; если 1-го прохода в журнале нет или на входе нет проб — w0 (W₀ партии). W_k — средняя на входе
    текущего прохода; проб входа ещё нет — влажность выхода прошлого прохода. Возвращает (масса, как посчитано)."""
    if m0 <= 0 or not passes:
        return None, "нет данных о проходах партии"
    cur = passes[-1]
    first = passes[0] if passes[0].get("pass_no") == 1 else None
    if cur is first or (len(passes) == 1 and cur.get("pass_no") == 1):
        return m0, f"1-й проход — вся партия {m0:.0f} т"
    w1, w1_src = (first["w_in"], "вход 1-го прохода по журналу") if first and first.get("w_in") is not None else \
        (w0, "W₀ партии")
    wk, wk_src = cur.get("w_in"), "вход сейчас"
    if wk is None and len(passes) >= 2 and passes[-2].get("w_end") is not None:
        wk, wk_src = passes[-2]["w_end"], "выход прошлого прохода"
    if w1 is None or wk is None or not (0 < w1 < 100 and 0 < wk < 100):
        return None, "не хватает влажности на входе"
    m = m0 * (100.0 - w1) / (100.0 - wk)
    return m, f"{m0:.0f} т · (100 − {w1:.1f}) / (100 − {wk:.1f}) = {m:.0f} т ({w1_src}; {wk_src})"


def _journal_regime(prep: List[Dict], p: mdl.DryerParams) -> Tuple[List[Tuple], List[Tuple[datetime, float]]]:
    """Весь журнал по времени: режим [(время, Q, T блоков, идёт ли выгрузка, D)] — от строки до следующей — и вход
    [(время, W)]. Во время остановки, охлаждения и рециркуляции зерно в шахте не движется к выходу из силоса
    (выгрузка = 0) и не сушится в учёт времени прохода."""
    from .calibration import STOP_STEMS, _t_proc
    tl: List[Tuple] = []
    inlet: List[Tuple[datetime, float]] = []
    stopped = False
    for r in prep:
        if r.get("dt") is None:
            continue
        note = (r.get("note") or "").lower()
        if any(w in note for w in STOP_STEMS):
            stopped = True
        elif "запуск" in note:
            stopped = False
        d = r.get("d_f")
        ok = d is not None and 0 < d <= 100
        running = ok and not stopped and not r.get("burner_off") and not r.get("recirc")
        tl.append((r["dt"], mdl.q_at(p, d) if ok else None, _t_proc(r, p.temp_source), running, d if ok else None))
        if r.get("w_in") is not None and not r.get("recirc"):
            inlet.append((r["dt"], r["w_in"]))
    return tl, inlet


def _walk_back(tl: List[Tuple], t_now: datetime, mass: float, times: Optional[List[datetime]] = None):
    """Порция, за которой с момента её входа выгружено mass т: (вход, часов сушки, ∫T dt по известной T, часов с
    известной T, D — если он не менялся, иначе None) или None — журнала не хватает (зашла раньше первой записи)."""
    import bisect
    times = times if times is not None else [row[0] for row in tl]
    idx = bisect.bisect_right(times, t_now) - 1
    acc = h_run = t_int = t_known = 0.0
    ds = set()
    end = t_now
    for j in range(idx, -1, -1):
        t_j, q_j, temp_j, run_j, d_j = tl[j]
        dur = max((end - t_j).total_seconds() / 3600.0, 0.0)
        end = t_j
        if dur <= 0 or not run_j:
            continue                      # остановка / охлаждение / рециркуляция: зерно стоит, время не считаем
        if q_j is None or q_j <= 0:
            return None
        ds.add(d_j)
        part = dur
        done = acc + q_j * dur >= mass - 1e-12
        if done:
            part = (mass - acc) / q_j
        h_run += part
        if temp_j is not None:
            t_int += temp_j * part
            t_known += part
        if done:
            return t_j + timedelta(hours=dur - part), h_run, t_int, t_known, (ds.pop() if len(ds) == 1 else None)
        acc += q_j * dur
    return None


def transit_profile(p: mdl.DryerParams, st: LiveState, c: mdl.Conditions, d_future: float, corr: float,
                    t_now: datetime, n: int = 24) -> Optional[Dict]:
    """Что выйдет из шахты в ближайшие часы — с учётом ВСЕХ режимов, которые зерно уже прошло.

    Шахта — плотный поток массы M = lag_k·вместимость, порезанный на n порций. У каждой порции своя история: когда
    вошла (по журналу: с её входа через выгрузку прошло M − «масса впереди»), с какой влажностью (вход в тот момент),
    сколько часов и при какой T блоков уже сушилась — по всем D и T блоков, записанным за это время. Дальше она
    идёт при D = d_future и T = c.t_proc и выходит через «масса впереди» / Q(d_future) часов. Её влажность на выходе —
    по модели с «действующим» режимом всего пути: Q = M / (время в шахте), T — средняя по времени (то же, что при
    калибровке, calibration.residence). Поэтому смена D действует и на зерно, уже находящееся в шахте: при большем D
    оно идёт быстрее и досушивается меньше.
    Возвращает {"t": время выхода порций, "w": истинная влажность на выходе, "w_in", "m": масса впереди, т,
    "known": порции, историю которых удалось восстановить по журналу} или None."""
    m_shaft = p.lag_k * p.m_dryer
    q_f = mdl.q_at(p, d_future)
    if m_shaft <= 0 or q_f <= 0 or not st.regime or not st.inlet:
        return None
    m_ahead = (np.arange(n) + 0.5) * m_shaft / n
    h_fut = m_ahead / q_f
    in_t = np.array([(t - t_now).total_seconds() / 3600.0 for t, _ in st.inlet])
    in_w = np.array([w for _, w in st.inlet])
    w_in = np.empty(n)
    d_eff = np.empty(n)
    t_eff = np.empty(n)
    known = np.zeros(n, dtype=bool)
    times = [row[0] for row in st.regime]
    for j in range(n):
        wb = _walk_back(st.regime, t_now, m_shaft - m_ahead[j], times)
        if wb is None:                     # журнала не хватает — как будто вошла сейчас и идёт при новом режиме
            w_in[j], d_eff[j], t_eff[j] = st.w_in, d_future, c.t_proc
            continue
        t_in, h_past, t_int, t_known, d_same = wb
        tau = h_past + h_fut[j]
        w_in[j] = float(np.interp((t_in - t_now).total_seconds() / 3600.0, in_t, in_w))
        d_eff[j] = d_future if d_same is not None and abs(d_same - d_future) < 1e-9 else mdl.d_at(p, m_shaft / tau)
        t_eff[j] = (t_int + c.t_proc * (tau - t_known)) / tau
        known[j] = True
    w_out, _ = mdl.solve_out_arr(p, w_in, d_eff, t_eff, c.t_amb, c.rh)
    return {"t": [t_now + timedelta(hours=float(h)) for h in h_fut], "w": w_out + corr, "w_in": w_in, "m": m_ahead,
            "known": known, "d": d_future}


def _transit_mean(prof: Optional[Dict], m_tr: float, fallback: float) -> float:
    """Средняя влажность первых m_tr тонн, которые выйдут из шахты (порции равной массы)."""
    if prof is None or m_tr <= 0:
        return fallback
    m = prof["m"]
    step = m[0] * 2.0
    weights = np.clip((m_tr - (m - 0.5 * step)) / step, 0.0, 1.0)
    return float(np.sum(prof["w"] * weights) / np.sum(weights)) if np.sum(weights) > 0 else fallback


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
    transit: Optional[Dict] = None         # что выйдет из шахты в ближайшие часы при выбранном D (transit_profile)
    overrun: bool = False                  # выгружено больше расчётной массы прохода, а подача идёт («рециркуляции»
    #                                        в журнале нет): масса на входе занижена, конец прохода неизвестен
    warnings: List[str] = field(default_factory=list)


def live_forecast(p: mdl.DryerParams, c: mdl.Conditions, st: LiveState, m_pass: float, w_target: float,
                  wk_final: float, soft: float, hard: float, use_corr: bool = True, cool: bool = True,
                  now: Optional[datetime] = None, d_override: Optional[float] = None, silo: bool = False,
                  silo_lim: float = 1.0, n_total: Optional[int] = None) -> LiveForecast:
    """w_target — желаемая истинная влажность на выходе ЭТОГО прохода (средняя по приёмному силосу); wk_final —
    цель партии.

    silo=True — выравнивание средней влажности приёмного силоса: всё, что выгружено за проход, ложится в один силос,
    и важна его средняя влажность, а не каждая проба. Если часть уже ушла суше или сырее цели (например, в начале
    прохода выходил хвост прошлого), остатку назначается такая влажность, чтобы среднее по массе вышло в цель:
        W_нужно = (W_цель·M_всего − W_ушло·M_ушло − W_шахта·M_шахта) / M_новое,
    где M_шахта ≈ lag_k·вместимость — зерно, уже находящееся в шахте: оно выйдет со старым режимом (≈ W при текущем
    D), и подстроить можно только зерно, которое войдёт с этого момента (M_новое). W_нужно ограничивается
    W_цель ± silo_lim, чтобы силос не стал «полосатым»: сильно разная влажность слоёв плохо хранится.

    n_total — число проходов партии по заданию (None — авто): остальные проходы до цели партии — n_total минус номер
    текущего, а не наименьшее число в лимите съёма."""
    fc = LiveForecast()
    corr = st.corr if use_corr else 0.0
    w_in = st.w_in
    d_now = st.d
    t_now = (st.end or max(now, st.last)) if now else st.last      # «сейчас» — с тем же ограничением, что выгрузка
    fc.w_out_now = mdl.solve_pass(p, c, w_in, 1.0, d_now).w_out + corr
    fc.w_out_now_read = mdl.read_from_true(p, fc.w_out_now, st.t_grain)

    def choose_d(w_goal_: float) -> Tuple[float, float, str]:
        raw_ = mdl.d_for_target(p, c, w_in, w_goal_ - corr)
        if raw_ > 100.5:
            return 100.0, raw_, "даже при D = 100 снимется больше, чем нужно — цель прохода можно поставить ниже"
        if raw_ < 1.0:
            return 1.0, raw_, "сушилка не успевает: даже при D = 1 цель прохода не достигается"
        return float(round(raw_)), raw_, ""

    w_goal = w_target
    fc.d_rec, fc.d_rec_raw, fc.d_note = choose_d(w_goal)
    if silo and st.silo_w is not None and st.unloaded > 1.0:
        m_total = m_pass * (100.0 - w_in) / (100.0 - w_target)
        m_done = st.unloaded
        m_rem = max(m_total - m_done, 0.0)
        m_tr = min(p.lag_k * p.m_dryer, m_rem)
        m_new = m_rem - m_tr
        # Конец подачи прохода знает только журнал («рециркуляция»): масса прохода — оценка (масса на входе и
        # выгрузка по Q(D)). Если по ней до конца подачи осталось мало нового зерна (short), а подача идёт, — силос не
        # выравниваем (для малого остатка нужная влажность неустойчива), D подбирается на цель прохода, как без
        # выравнивания. Совет «D уже не менять» — только при рециркуляции (live_advice).
        fc.silo = {"m_total": m_total, "m_done": m_done, "w_done": st.silo_w, "w_done_read": st.silo_w_read,
                   "m_tr": m_tr, "w_tr": fc.w_out_now, "m_new": m_new, "need": None, "limited": False,
                   "short": m_new < max(0.05 * m_total, 5.0), "lim": silo_lim}
        # зерно, которое уже в шахте, выйдет с влажностью по своей истории (все режимы за его путь) и по новому D:
        # при другом D оно пойдёт быстрее или медленнее. Поэтому D и цель для нового зерна уточняются по кругу.
        d_try_ = d_override if d_override else fc.d_rec
        for _ in range(4):
            prof = transit_profile(p, st, c, d_try_, corr, t_now)
            w_tr = _transit_mean(prof, m_tr, fc.w_out_now)
            fc.silo["w_tr"] = w_tr
            if fc.silo["short"]:
                break
            need = (w_target * m_total - st.silo_w * m_done - w_tr * m_tr) / m_new
            w_goal = min(max(need, w_target - silo_lim), w_target + silo_lim)
            fc.silo.update(need=need, limited=abs(need - w_goal) > 0.05)
            fc.d_rec, fc.d_rec_raw, fc.d_note = choose_d(w_goal)
            if d_override or abs(fc.d_rec - d_try_) < 0.5:
                break
            d_try_ = fc.d_rec
    fc.w_goal = w_goal
    d_use = d_override if d_override else fc.d_rec
    fc.transit = transit_profile(p, st, c, d_use, corr, t_now)
    fc.w_out_rec = mdl.solve_pass(p, c, w_in, 1.0, d_use).w_out + corr
    if fc.silo:
        sl = fc.silo
        new_part = fc.w_out_rec * sl["m_new"] if sl["m_new"] > 0 else 0.0
        sl["avg"] = (sl["w_done"] * sl["m_done"] + sl["w_tr"] * sl["m_tr"] + new_part) / max(
            sl["m_done"] + sl["m_tr"] + max(sl["m_new"], 0.0), 1e-9)
    q_new = mdl.q_at(p, d_use)
    fc.tau_h = p.lag_k * p.m_dryer / q_new if q_new > 0 else float("inf")
    fc.visible_at = t_now + timedelta(hours=fc.tau_h) if fc.tau_h < 1e6 else None
    # остаток прохода: масса на выходе прохода по ожидаемой влажности (с выравниванием — средняя по силосу)
    w_pass_out = fc.silo["avg"] if fc.silo else fc.w_out_rec
    fc.m_out_pass = m_pass * (100.0 - w_in) / (100.0 - w_pass_out) if w_pass_out < 100 else m_pass
    fc.remaining = max(fc.m_out_pass - st.unloaded, 0.0)
    fc.pass_end = t_now + timedelta(hours=fc.remaining / q_new) if q_new > 0 else None
    if not st.recirc and fc.remaining <= 0.05:
        # по массе проход выгружен, а «рециркуляции» в журнале нет — подача идёт: масса на входе занижена (или Q по
        # таблице выше настоящей). Когда закончится подача, неизвестно; остаток партии — не раньше, чем от «сейчас»
        fc.overrun, fc.pass_end = True, None
        fc.warnings.append(f"выгружено {st.unloaded:.0f} т — больше расчётной массы прохода ({fc.m_out_pass:.0f} т), "
                           f"а «рециркуляции» в журнале нет: подача идёт. Масса на входе, похоже, занижена — "
                           f"проверьте её (масса партии в «План сушки партии»)")
    pe = fc.pass_end or (t_now if fc.overrun else None)       # с какого момента считать остаток партии
    # остальные проходы до цели партии
    if pe and w_pass_out > wk_final + 0.05:
        n_left = int(round(n_total - st.pass_no)) if n_total and st.pass_no else 0
        if n_total and st.pass_no and n_left < 1:
            fc.warnings.append(f"по заданию в партии {n_total} прох., это последний, но цель прохода выше цели "
                               f"партии — остаток посчитан по лимиту съёма")
        if n_left >= 1:
            fc.rest_plan = mdl.plan_by_passes(p, c, fc.m_out_pass, w_pass_out, wk_final, n_left,
                                              pe + timedelta(hours=p.overhead_h), None, cool)
        else:
            plans = mdl.scenarios(p, c, fc.m_out_pass, w_pass_out, wk_final, 6,
                                  pe + timedelta(hours=p.overhead_h), None, cool)
            k = mdl.recommend(plans, soft, hard)
            fc.rest_plan = plans[k] if k is not None else plans[-1]
        fc.batch_end = fc.rest_plan.end
        n_rest = len(fc.rest_plan.passes)
        start_rest = pe + timedelta(hours=p.overhead_h)
        # честный допуск остатка партии: проверка на журнале + разброс коэффициентов + расхождение моделей
        from dataclasses import replace as _replace
        sig, fc.sigma_info = mdl.plan_sigma(p, fc.rest_plan, c, mdl.other_model_final(p, fc.rest_plan, c))
        lo, hi = mdl.time_range(lambda pp: mdl.plan_by_passes(pp, c, fc.m_out_pass, w_pass_out, wk_final, n_rest,
                                                              start_rest, None, cool), _replace(p, rate_sigma=sig))
        fc.batch_end_range = (start_rest + timedelta(hours=lo), start_rest + timedelta(hours=hi))
    elif pe:
        cool_h = fc.m_out_pass / mdl.q_full(p) if cool and mdl.q_full(p) > 0 else 0.0
        fc.batch_end = pe + timedelta(hours=(p.overhead_h if cool else 0.0) + cool_h)
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


def outlet_soon(p: mdl.DryerParams, st: LiveState, fc: Optional[LiveForecast], hours=(1.0, 2.0)) -> str:
    """«через 1 ч ≈ 23,4 %, через 2 ч ≈ 23,9 %» — что покажет выходной влагомер по прогнозу зерна, которое уже в
    шахте (transit_profile: все режимы, при которых оно шло, и выбранное D до выхода)."""
    tr = fc.transit if fc is not None else None
    if tr is None or not len(tr["t"]):
        return ""
    step = (tr["t"][1] - tr["t"][0]) if len(tr["t"]) > 1 else timedelta(0)
    t0 = tr["t"][0] - step / 2
    hs = [(x - t0).total_seconds() / 3600.0 for x in tr["t"]]
    return ", ".join(f"через {h:g} ч ≈ {mdl.read_from_true(p, float(np.interp(h, hs, tr['w'])), st.t_grain):.1f} %"
                     for h in hours if h <= hs[-1])


# ----------------------------------------------------------------------------------------------------------------
# Главное на экране «Текущий проход»: одно действие с D и почему
# ----------------------------------------------------------------------------------------------------------------

D_STEP_MIN = 3.0      # разница с советом меньше 3 с (≈ 0,5 п.п. на выходе) — D не менять (правила работы, 7.3)
D_STEP_BIG = 10.0     # больше 10 с — сначала проверить пробу входа, D и T блоков в журнале и задание


def live_advice(p: mdl.DryerParams, st: LiveState, fc: Optional[LiveForecast], d_try: Optional[float] = None,
                now: Optional[datetime] = None) -> Dict:
    """{"kind": "change" | "keep" | "wait" | "info" | "nodata", "head": крупная строка, "sub": [строки],
    "warn": [предупреждения], "go": раздел, куда отправить (или None), "d_now": D сейчас, "d_rec": советуемое D,
    "until": до какого времени ждать (для wait), "d_try": проверяемое D, "late": проход уже не поправить}.

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
                "warn": [], "go": go, "d_now": st.d if st.ok else None, "d_rec": None}
    warn = ([fc.d_note[0].upper() + fc.d_note[1:]] if fc.d_note else []) + [w[0].upper() + w[1:] for w in fc.warnings]
    read = mdl.read_from_true(p, fc.w_out_rec, st.t_grain)
    if st.recirc:
        # подача закончилась: советы про D и съём прохода уже ни к чему, остаются только пределы температуры
        warn = [x for x in warn if x.startswith("T ")]
        since = f" с {st.recirc_since:%H:%M}" if st.recirc_since else ""
        w_txt = f"≈ {st.w_end:.1f} %" if st.w_end is not None else "у цели"
        if st.batch_end:
            return {"kind": "info", "head": "Партия высушена — идёт досушка",
                    "sub": [f"Рециркуляция{since}: подача закончилась, выход к концу подачи {w_txt} — это цель "
                            f"партии. В шахте досушивается остаток.",
                            f"Сколько досушивать — раздел «{RECIRC}», затем охлаждение."],
                    "warn": warn, "go": RECIRC, "d_now": st.d, "d_rec": None, "batch_end": True}
        return {"kind": "info", "head": "Проход закончен — D для этого прохода уже не менять",
                "sub": [f"Рециркуляция{since}: подача из силосов этого прохода закончилась, в шахте только его остаток "
                        f"— он ходит по кругу; выход к концу подачи {w_txt}.",
                        "Следующий проход начнётся, когда пойдёт подача: «сушим с N с.» или «Запуск, сушим с N с.». "
                        f"Сколько досушивать остаток — раздел «{RECIRC}»."],
                "warn": warn, "go": RECIRC, "d_now": st.d, "d_rec": None, "late": True}
    if d_try:
        return {"kind": "info", "head": f"При D = {d_try:.0f} с на выходе ≈ {fc.w_out_rec:.1f} %",
                "sub": [f"Влагомер покажет ≈ {read:.1f} %. Подобранное для цели {fc.w_goal:.1f} %: D = {fc.d_rec:.0f} с."],
                "warn": warn, "go": None, "d_now": st.d, "d_rec": fc.d_rec, "d_try": d_try}
    diff = fc.d_rec - st.d
    t_now = max(now, st.last) if now and st.last else (now or st.last)
    q_cur = mdl.q_at(p, st.d)
    tau_cur = p.lag_k * p.m_dryer / q_cur if q_cur > 0 else 0.0
    soon = outlet_soon(p, st, fc)
    soon_line = [f"Влагомер на выходе: {soon} (зерно уже в шахте, со всеми режимами за его путь)."] if soon else []
    if abs(diff) < D_STEP_MIN:
        w_now_read = fc.w_out_now_read
        return {"kind": "keep", "head": f"D не менять — {st.d:.0f} с",
                "sub": [f"Зерно, которое входит сейчас, выйдет ≈ {fc.w_out_now:.1f} % (влагомер ≈ {w_now_read:.1f} %), "
                        f"цель {fc.w_goal:.1f} %."] + soon_line +
                       [f"Совет {fc.d_rec:.0f} с отличается меньше чем на {D_STEP_MIN:.0f} с — это ≈ 0,5 п.п. на "
                        f"выходе, на уровне точности влагомера."],
                "warn": warn, "go": None, "d_now": st.d, "d_rec": fc.d_rec}
    if st.d_changed_at is not None and t_now is not None:
        ready = st.d_changed_at + timedelta(hours=tau_cur / 2.0)
        if t_now < ready:
            seen = st.d_changed_at + timedelta(hours=tau_cur)
            return {"kind": "wait", "head": f"Подождите до {ready:%H:%M} — D не трогать",
                    "sub": [f"D меняли в {st.d_changed_at:%H:%M} ({st.d_prev:.0f} → {st.d:.0f} с); на выходе это станет "
                            f"видно к {seen:%H:%M}, а пока выходит зерно, которое сушилось при старом D."] + soon_line +
                           [f"Совет сейчас: D = {fc.d_rec:.0f} с — проверьте его после {ready:%H:%M}."],
                    "warn": warn, "go": None, "d_now": st.d, "d_rec": fc.d_rec, "until": ready}
    sub = [f"Сейчас {st.d:.0f} с. На выходе станет ≈ {fc.w_out_rec:.1f} % (влагомер ≈ {read:.1f} %), "
           f"цель {fc.w_goal:.1f} %."]
    if fc.visible_at:
        sub.append(f"Заметно на выходе через {fc.tau_h:.1f} ч — к {fc.visible_at:%H:%M}. После смены запишите её: "
                   f"«Изменить режим…» в журнале.")
    if soon:
        sub.append(f"При D = {fc.d_rec:.0f} влагомер на выходе: {soon}.")
    if abs(diff) > D_STEP_BIG:
        warn = ["Большое изменение: сначала проверьте последнюю пробу входа, D и T блоков в журнале и задание "
                "прохода; всё верно — меняйте, сомневаетесь — позвоните инженеру."] + warn
    return {"kind": "change", "head": f"Поставьте D = {fc.d_rec:.0f} с", "sub": sub, "warn": warn, "go": None,
            "d_now": st.d, "d_rec": fc.d_rec}
