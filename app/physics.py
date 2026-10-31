"""Физическая модель прохода шахтной сушилки (вторая модель, на выбор вместо эмпирической).

Идея
----
Зерно идёт сверху вниз; в горячей зоне оно находится время

    t_h = f_hot · m_сушилки / Q(D)                    [ч]

(f_hot — доля объёма горячей зоны, по ощущению сушильщиков 80–85 %). Как сохнет одна зерновка, обдуваемая
агентом сушки, известно из опытов — это кривая сушки кукурузы в тонком слое (Thompson, Peart, Foster, 1968,
Trans. ASAE 11(4): 582–586; проверена на 120…340 °F ≈ 49…171 °C и начальной влажности 19…33 %):

    t = A · ln(MR) + B · ln(MR)²,   A = −1.862 + 0.00488·T,   B = 427.4·exp(−0.033·T)      (T в °F, t в ч)
    MR = (M − Me) / (M0 − Me),       M — влажность на сухое вещество, %

Me — равновесная влажность кукурузы при температуре и относительной влажности агента (модифицированное
уравнение Хендерсона из той же работы):  1 − RH = exp(−3.82·10⁻⁵ · (T + 50) · Me²),  T в °F, Me в % на с.в.
RH агента считается психрометрически: наружный воздух нагревается горелкой при неизменном влагосодержании.

В шахте зерно обдувается не свежим агентом, а воздухом, который уже прошёл через слой, остыл и увлажнился;
часть времени зерно ещё и прогревается. Это учитывает КПД слоя η — единственный калибруемый по журналу
параметр: эффективное время t_eff = η · t_h · k_нар, где k_нар — поправка на прогрев холодного зерна:
тепло, подведённое к зерну, делится между прогревом (c_зерна · (T_зерна − T_нар)) и испарением (Δu · L);
k_нар = доля испарения при текущей T_нар / доля при опорной T_нар (при опорной погоде k_нар = 1).

Дополнительно можно задать κ — какую долю перепада (T_агента − T_мокр) зерно видит в среднем по слою:
воздух в слое остывает к температуре мокрого термометра по линии адиабатного насыщения (κ = 1 — тонкий слой
при T агента). κ не калибруется автоматически: на журнале октября его подбор переобучал модель.

Что показала проверка на журнале 18–25.10.2026 (4 прохода): физическая модель с одним η описывает съём за
проход с ошибкой ±0,51 п.п. (эмпирическая ±0,14 при трёх параметрах), а исключённый проход предсказывает
с ошибкой ±0,64 п.п. (эмпирическая ±0,4). Сушилка оказалась заметно менее чувствительной к T блоков, чем
тонкий слой (проход при 48 °C снял почти столько же, сколько при 73 °C). Поэтому основная — эмпирическая,
а физическая — независимая вторая оценка: если на плане они сильно расходятся, план вышел за пределы того,
что видно в журнале. Какая точнее на ваших данных, показывает вкладка «Проверка модели».
"""
from __future__ import annotations

from functools import lru_cache
from math import exp, log, sqrt
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .model import Conditions, DryerParams, grain_temp, q_at, q_at_arr

C_DM = 1.5        # теплоёмкость сухого вещества кукурузы, кДж/(кг·К)
C_W = 4.19        # теплоёмкость воды, кДж/(кг·К)
L_EVAP = 2400.0   # теплота испарения из зерна, кДж/кг (без теплоты связи — она одинакова в обеих долях)


def f_of_c(t_c: float) -> float:
    return t_c * 9.0 / 5.0 + 32.0


def p_sat(t_c: float) -> float:
    """Давление насыщенного пара над водой, кПа (формула Магнуса, коэффициенты Alduchov & Eskridge)."""
    return 0.61094 * exp(17.625 * t_c / (t_c + 243.04))


P_ATM = 101.325   # кПа


def agent_rh(t_agent: float, t_amb: float, rh_amb: float) -> float:
    """Относительная влажность агента сушки, %: наружный воздух нагрет при постоянном влагосодержании."""
    pv = max(min(rh_amb, 100.0), 0.0) / 100.0 * p_sat(t_amb)
    return max(min(100.0 * pv / p_sat(max(t_agent, t_amb)), 100.0), 0.01)


def humidity_ratio(t_c: float, rh_pct: float) -> float:
    """Влагосодержание воздуха, кг воды / кг сухого воздуха."""
    pv = max(min(rh_pct, 100.0), 0.0) / 100.0 * p_sat(t_c)
    return 0.622 * pv / (P_ATM - pv)


def _x_sat(t_c: float) -> float:
    ps = p_sat(t_c)
    return 0.622 * ps / (P_ATM - ps)


@lru_cache(maxsize=4096)
def wet_bulb(t_c: float, x: float) -> float:
    """Температура мокрого термометра (адиабатного насыщения), °C, для воздуха t_c с влагосодержанием x."""
    lo, hi = -30.0, t_c
    for _ in range(50):
        tw = 0.5 * (lo + hi)
        # уравнение адиабатного насыщения (ASHRAE Fundamentals): x(tw) при заданных t и tw
        x_tw = ((2501.0 - 2.326 * tw) * _x_sat(tw) - 1.006 * (t_c - tw)) / (2501.0 + 1.86 * t_c - 4.186 * tw)
        if x_tw > x:
            hi = tw
        else:
            lo = tw
    return 0.5 * (lo + hi)


@lru_cache(maxsize=4096)
def bed_air(t_agent: float, t_amb: float, rh_amb: float, kappa: float) -> Tuple[float, float]:
    """Средний по слою воздух: (T, RH %). Агент в слое остывает к мокрому термометру и увлажняется по линии
    адиабатного насыщения; kappa — доля перепада (T_агента − T_мокр), которую зерно видит в среднем.
    kappa = 1 — тонкий слой (свежий агент), kappa → 0 — насыщенный воздух при T_мокр."""
    x = humidity_ratio(t_amb, rh_amb)
    t_agent = max(t_agent, t_amb)
    tw = wet_bulb(round(t_agent, 3), round(x, 7))
    t_eff = tw + kappa * (t_agent - tw)
    x_eff = x + (1.0 - kappa) * max(_x_sat(tw) - x, 0.0)
    pv = x_eff * P_ATM / (0.622 + x_eff)
    return t_eff, max(min(100.0 * pv / p_sat(t_eff), 99.5), 0.01)


def emc_db(t_c: float, rh_pct: float) -> float:
    """Равновесная влажность кукурузы, % на сухое вещество (модиф. Хендерсон, Thompson et al., 1968)."""
    rh = min(max(rh_pct / 100.0, 1e-4), 0.995)
    return sqrt(-log(1.0 - rh) / (3.82e-5 * (f_of_c(t_c) + 50.0)))


def thompson_ln_mr(t_h: float, t_c: float) -> float:
    """ln(MR) после t_h часов сушки тонкого слоя кукурузы агентом с температурой t_c, °C."""
    if t_h <= 0:
        return 0.0
    tf = f_of_c(t_c)
    a = -1.862 + 0.00488 * tf
    b = 427.4 * exp(-0.033 * tf)
    return (-a - sqrt(a * a + 4.0 * b * t_h)) / (2.0 * b)


def db(w: float) -> float:
    """Влажность на сырую массу, % → на сухое вещество, %."""
    return 100.0 * w / (100.0 - w)


def wb(u: float) -> float:
    """На сухое вещество, % → на сырую массу, %."""
    return 100.0 * u / (100.0 + u)


def _evap_share(u0: float, du: float, t_grain: float, t_amb: float) -> float:
    """Доля тепла, ушедшая на испарение (а не на прогрев зерна от T_нар до T_зерна)."""
    q_g = (C_DM + u0 / 100.0 * C_W) * max(t_grain - t_amb, 0.0)
    q_e = max(du, 0.0) / 100.0 * L_EVAP
    return q_e / (q_e + q_g) if q_e + q_g > 1e-12 else 1.0


def residence_hot_h(p: DryerParams, d: float) -> float:
    q = q_at(p, d)
    return p.f_hot * p.m_dryer / q if q > 0 else 1e9


def _dry(p: DryerParams, c: Conditions, w_in: float, t_hot: float, eta: float, kappa: float) -> float:
    """Влажность (истинная, %) после t_hot часов в горячей зоне."""
    t, rh_bed = bed_air(round(c.t_proc, 3), round(c.t_amb, 3), round(c.rh, 3), round(kappa, 5))
    me = emc_db(t, rh_bed)
    u0 = db(w_in)
    if u0 <= me + 1e-6:
        return w_in
    tg = grain_temp(p, c.t_proc, w_in)
    k_amb = 1.0
    u = u0
    for _ in range(6):
        u = me + exp(thompson_ln_mr(eta * t_hot * k_amb * p.rate_scale, t)) * (u0 - me)
        if abs(c.t_amb - p.t_amb_ref) < 1e-9:
            break
        new = _evap_share(u0, u0 - u, tg, c.t_amb) / max(_evap_share(u0, u0 - u, tg, p.t_amb_ref), 1e-9)
        if abs(new - k_amb) < 1e-7:
            break
        k_amb = new
    return wb(u)


def pass_out(p: DryerParams, c: Conditions, w_in: float, d: float, eta: Optional[float] = None,
             kappa: Optional[float] = None) -> float:
    """Влажность на выходе прохода (истинная, %) при входе w_in и разгрузке D."""
    return _dry(p, c, w_in, residence_hot_h(p, d), p.eta if eta is None else eta,
                p.kappa if kappa is None else kappa)


def recirc_w(p: DryerParams, c: Conditions, w0: float, t_h: float) -> float:
    """Рециркуляция: каждая зерновка в горячей зоне долю времени f_hot; влажность через t_h часов."""
    return _dry(p, c, w0, p.f_hot * t_h, p.eta, p.kappa)


def d_for_target(p: DryerParams, c: Conditions, w_in: float, w_target: float) -> float:
    """D, при котором проход даёт w_target. >100 — сушилка не успевает снять так мало; <1 — нужна скорость ниже минимальной."""
    if w_target >= w_in:
        return 0.0
    lo, hi = 0.05, 2000.0               # по ln D; w_out растёт с D
    if pass_out(p, c, w_in, lo) > w_target:
        return lo                         # даже при минимальной выгрузке не досушить (ниже равновесной)
    if pass_out(p, c, w_in, hi) < w_target:
        return hi
    a, b = log(lo), log(hi)
    for _ in range(60):
        m = 0.5 * (a + b)
        if pass_out(p, c, w_in, exp(m)) > w_target:
            b = m
        else:
            a = m
    return exp(0.5 * (a + b))


# ----------------------------------------------------------------------------
# То же для массивов (numpy): калибровка и проверка модели считают сотни–тысячи проходов сразу.
# Формулы и порядок вычислений те же, что в скалярных функциях выше.
# ----------------------------------------------------------------------------

def p_sat_arr(t_c: np.ndarray) -> np.ndarray:
    return 0.61094 * np.exp(17.625 * t_c / (t_c + 243.04))


def _x_sat_arr(t_c: np.ndarray) -> np.ndarray:
    ps = p_sat_arr(t_c)
    return 0.622 * ps / (P_ATM - ps)


def humidity_ratio_arr(t_c: np.ndarray, rh_pct: np.ndarray) -> np.ndarray:
    pv = np.clip(rh_pct, 0.0, 100.0) / 100.0 * p_sat_arr(t_c)
    return 0.622 * pv / (P_ATM - pv)


def wet_bulb_arr(t_c: np.ndarray, x: np.ndarray) -> np.ndarray:
    lo = np.full_like(t_c, -30.0)
    hi = t_c.copy()
    for _ in range(50):
        tw = 0.5 * (lo + hi)
        x_tw = ((2501.0 - 2.326 * tw) * _x_sat_arr(tw) - 1.006 * (t_c - tw)) / (2501.0 + 1.86 * t_c - 4.186 * tw)
        up = x_tw > x
        hi = np.where(up, tw, hi)
        lo = np.where(up, lo, tw)
    return 0.5 * (lo + hi)


def bed_air_arr(t_agent: np.ndarray, t_amb: np.ndarray, rh_amb: np.ndarray, kappa: float
                ) -> Tuple[np.ndarray, np.ndarray]:
    x = humidity_ratio_arr(t_amb, rh_amb)
    t_agent = np.maximum(t_agent, t_amb)
    tw = wet_bulb_arr(np.round(t_agent, 3), np.round(x, 7))
    t_eff = tw + kappa * (t_agent - tw)
    x_eff = x + (1.0 - kappa) * np.maximum(_x_sat_arr(tw) - x, 0.0)
    pv = x_eff * P_ATM / (0.622 + x_eff)
    return t_eff, np.clip(100.0 * pv / p_sat_arr(t_eff), 0.01, 99.5)


def emc_db_arr(t_c: np.ndarray, rh_pct: np.ndarray) -> np.ndarray:
    rh = np.clip(rh_pct / 100.0, 1e-4, 0.995)
    return np.sqrt(-np.log(1.0 - rh) / (3.82e-5 * (t_c * 9.0 / 5.0 + 32.0 + 50.0)))


def thompson_ln_mr_arr(t_h: np.ndarray, t_c: np.ndarray) -> np.ndarray:
    tf = t_c * 9.0 / 5.0 + 32.0
    a = -1.862 + 0.00488 * tf
    b = 427.4 * np.exp(-0.033 * tf)
    with np.errstate(invalid="ignore"):
        out = (-a - np.sqrt(a * a + 4.0 * b * t_h)) / (2.0 * b)
    return np.where(t_h <= 0, 0.0, out)


def _evap_share_arr(u0: np.ndarray, du: np.ndarray, t_grain: np.ndarray, t_amb) -> np.ndarray:
    q_g = (C_DM + u0 / 100.0 * C_W) * np.maximum(t_grain - t_amb, 0.0)
    q_e = np.maximum(du, 0.0) / 100.0 * L_EVAP
    s = q_e + q_g
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(s > 1e-12, q_e / s, 1.0)


class PhysBatch:
    """Набор проходов для физической модели: всё, что не зависит от η, считается один раз
    (воздух в слое, равновесная влажность, время в горячей зоне), затем out(η) — w_вых для всех сразу."""

    def __init__(self, p: DryerParams, w_in, d, t_proc, t_amb, rh, kappa: Optional[float] = None,
                 clamp_d: bool = False):
        self.p = p
        self.w_in = np.asarray(w_in, dtype=float)
        shape = self.w_in.shape
        t_proc, t_amb, rh = (np.broadcast_to(np.asarray(x, dtype=float), shape) for x in (t_proc, t_amb, rh))
        d = np.asarray(d, dtype=float)
        if clamp_d:
            d = np.clip(d, 1.0, 100.0)
        kap = round(p.kappa if kappa is None else kappa, 5)
        self.t, rh_bed = bed_air_arr(np.round(t_proc, 3), np.round(t_amb, 3), np.round(rh, 3), kap)
        self.me = emc_db_arr(self.t, rh_bed)
        self.u0 = 100.0 * self.w_in / (100.0 - self.w_in)
        self.dry = self.u0 <= self.me + 1e-6
        self.tg = grain_temp(p, t_proc, self.w_in)
        q = q_at_arr(p, d)
        with np.errstate(divide="ignore"):
            self.t_hot = np.where(q > 0, p.f_hot * p.m_dryer / q, 1e9)
        self.t_amb = t_amb
        self.at_ref = np.abs(t_amb - p.t_amb_ref) < 1e-9

    def out(self, eta: Optional[float] = None) -> np.ndarray:
        p = self.p
        eta = p.eta if eta is None else eta
        u0, me = self.u0, self.me
        k_amb = np.ones_like(u0)
        u = u0.copy()
        done = np.zeros(u0.shape, dtype=bool)
        for _ in range(6):
            u_new = me + np.exp(thompson_ln_mr_arr(eta * self.t_hot * k_amb * p.rate_scale, self.t)) * (u0 - me)
            u = np.where(done, u, u_new)
            done = done | self.at_ref
            if done.all():
                break
            new = (_evap_share_arr(u0, u0 - u, self.tg, self.t_amb)
                   / np.maximum(_evap_share_arr(u0, u0 - u, self.tg, p.t_amb_ref), 1e-9))
            done = done | (np.abs(new - k_amb) < 1e-7)
            k_amb = np.where(done, k_amb, new)
            if done.all():
                break
        return np.where(self.dry, self.w_in, 100.0 * u / (100.0 + u))


def pass_out_arr(p: DryerParams, w_in, d, t_proc, t_amb, rh, eta: Optional[float] = None,
                 kappa: Optional[float] = None) -> np.ndarray:
    """pass_out для массивов (D уже ограничен 1…100 вызывающим, как в solve_pass)."""
    return PhysBatch(p, w_in, d, t_proc, t_amb, rh, kappa).out(eta)


def obs_arrays(obs: Sequence[Dict]) -> Dict[str, np.ndarray]:
    """Наблюдения журнала -> массивы по полям."""
    keys = ("w_in", "w_out", "d", "t_proc", "t_amb", "rh", "r")
    return {k: np.fromiter((o[k] for o in obs), dtype=float, count=len(obs)) for k in keys}


# ----------------------------------------------------------------------------
# Калибровка η по наблюдениям журнала (те же пары «вход в t − τ / выход в t», что и для эмпирической)
# ----------------------------------------------------------------------------

def predict_obs(p: DryerParams, obs: Sequence[Dict], eta: Optional[float] = None,
                kappa: Optional[float] = None) -> List[float]:
    if not obs:
        return []
    a = obs_arrays(obs)
    return PhysBatch(p, a["w_in"], a["d"], a["t_proc"], a["t_amb"], a["rh"], kappa).out(eta).tolist()


def _golden(f, a: float, b: float, iters: int) -> Tuple[float, float]:
    g = (sqrt(5.0) - 1.0) / 2.0
    x1, x2 = b - g * (b - a), a + g * (b - a)
    f1, f2 = f(x1), f(x2)
    for _ in range(iters):
        if f1 < f2:
            b, x2, f2 = x2, x1, f1
            x1 = b - g * (b - a)
            f1 = f(x1)
        else:
            a, x1, f1 = x1, x2, f2
            x2 = a + g * (b - a)
            f2 = f(x2)
    x = 0.5 * (a + b)
    return x, f(x)


KAPPA_PRIOR = 1.0     # априорно: тонкий слой при T агента (подбор κ на журнале октября давал переобучение)
KAPPA_LAMBDA = 0.05   # слабая регуляризация ln κ к априорному (данных по разным T обычно мало)
ETA_RANGE = (0.03, 8.0)
KAPPA_RANGE = (0.12, 1.0)


def calibrate_physics(obs: Sequence[Dict], p: DryerParams, weights: Optional[Sequence[float]] = None,
                      fit_kappa: bool = True) -> Tuple[float, float, Dict]:
    """Подбор η и κ (взвешенный МНК по w_вых): внутри — η золотым сечением по ln η, снаружи — κ.

    κ подбирается, только если в данных есть разброс температуры (≥ 8 °C между крайними строками),
    иначе остаётся текущим."""
    if not obs:
        raise ValueError("нет наблюдений для калибровки физической модели")
    a = obs_arrays(obs)
    wts = np.asarray(list(weights), dtype=float) if weights else np.ones(len(obs))
    w_sum = float(wts.sum())
    y = a["w_out"]
    fit_kappa = fit_kappa and (float(a["t_proc"].max() - a["t_proc"].min()) >= 8.0)
    batches: Dict[float, PhysBatch] = {}

    def batch(kappa: float) -> PhysBatch:
        key = round(kappa, 5)
        if key not in batches:
            batches[key] = PhysBatch(p, a["w_in"], a["d"], a["t_proc"], a["t_amb"], a["rh"], kappa)
        return batches[key]

    def sse(eta: float, kappa: float) -> float:
        return float(np.sum(wts * (batch(kappa).out(eta) - y) ** 2))

    def best_eta(kappa: float) -> Tuple[float, float]:
        x, f = _golden(lambda le: sse(exp(le), kappa), log(ETA_RANGE[0]), log(ETA_RANGE[1]), 30)
        return exp(x), f

    if fit_kappa:
        def outer(lk: float) -> float:
            _, f = best_eta(exp(lk))
            return f + KAPPA_LAMBDA * w_sum * (lk - log(KAPPA_PRIOR)) ** 2
        lk, _ = _golden(outer, log(KAPPA_RANGE[0]), log(KAPPA_RANGE[1]), 16)
        kappa = exp(lk)
    else:
        kappa = p.kappa
    eta, _ = best_eta(kappa)
    errs = batch(kappa).out(eta) - y
    return eta, kappa, {"mae": float(np.mean(np.abs(errs))), "bias": float(np.mean(errs)),
                        "max": float(np.max(np.abs(errs))),
                        "at_edge": eta < ETA_RANGE[0] * 1.05 or eta > ETA_RANGE[1] * 0.95, "kappa_fitted": fit_kappa}


def calibrate_eta(obs: Sequence[Dict], p: DryerParams, weights: Optional[Sequence[float]] = None
                  ) -> Tuple[float, Dict]:
    """Только η при текущем κ."""
    eta, _, rep = calibrate_physics(obs, p, weights, fit_kappa=False)
    return eta, rep
