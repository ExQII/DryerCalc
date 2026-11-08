"""Расчётное ядро: время сушки зерна в шахтной зерносушилке.

Схема процесса
--------------
Вся партия массой M проходит через сушилку, после прохода ложится в силос, затем
(если нужно) сразу идёт следующий проход. За проход зерно выгружается со скоростью

    Q = Q(D)            [т/ч]

где D — число секунд работы выгрузного механизма из 100-секундного цикла (1..100).
Q(D) берётся из таблицы пересчёта сушилки (по умолчанию — натура 0,65 т/м³, она линейна: Q = 0,52·D).

Баланс влаги за проход (w — % на сырую массу, Q — поток ВЫГРУЖЕННОГО зерна):

    R = Q * (w_вх - w_вых) / (100 - w_вх)            [т воды / ч]

Расход влаги R — свойство сушилки в текущих условиях. Его даёт эмпирическая модель:

    R = R_ref * ((w_ср - w_eq)/(w_ref - w_eq))^b_w
              * ((T - T_база)/(T_ref - T_база))^b_dt          T_база = t_amb_ref (10 °C)
              * exp(b_amb * (T_нар - T_нар_ref) / 10)          холодное зерно часть тепла тратит на прогрев
              * exp(b_rh * (RH - RH_ref) / 30)

R_ref и показатели калибруются по журналу сушильщиков (calibration.py).
Из баланса и модели w_вых находится численно (метод половинного деления).

Вторая, физическая модель (physics.py) считает w_вых прохода по кинетике сушки зерна в тонком
слое (коэффициенты по умолчанию — Thompson et al., 1968) за время пребывания в горячей зоне с калибруемым КПД
слоя η.
Выбор модели — DryerParams.model_kind.

До v0.4 температура входила как (T − T_нар): при записанной наружной температуре холод ускорял бы
сушку, что физически неверно. Пока T_нар в журнале не было (подставлялась опорная), обе записи
совпадают, поэтому прежние калибровки остаются в силе.

Влажность в модели — «истинная» (как после охлаждения): w_истин = w_показание − поправка выходного влагомера
(meter_correction: a + k_w·(W − 20) + k_t·(T − 30)). Поправка — только внесённая в программу (пары проб, партии с
лабораторией, «Настройки»); по умолчанию 0.

Время прохода = масса выгруженного зерна / Q. Этого достаточно, чтобы предсказать длительность
реальных проходов: m_вых / Q плюс около часа на загрузку, прогрев и переключения.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from math import exp, log, sqrt
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np


@dataclass
class DryerParams:
    """Параметры сушилки и модели. Значения по умолчанию — по журналу октября 2026."""

    q100: float = 52.0        # т/ч выгруженного зерна при D = 100 (лист пересчёта, натура 0,65)
    q_table: Optional[List[List[float]]] = None  # [[D, Q], ...]; None — линейно от q100
    m_dryer: float = 60.0     # т, вместимость сушилки (время пребывания зерна)
    r_ref: float = 1.57       # т воды/ч в опорных условиях (калибруется по журналу)
    w_ref: float = 22.0       # опорная влажность, %
    t_ref: float = 60.0       # опорная температура процесса, °C
    t_amb_ref: float = 10.0   # опорная наружная температура, °C (в журнале не пишется)
    rh_ref: float = 70.0      # опорная наружная влажность воздуха, %
    w_eq: float = 8.0         # равновесная влажность (ниже не сушится), %
    b_w: float = 0.80         # показатель по (w - w_eq)
    b_dt: float = 0.92        # показатель по (T - T_нар)
    b_rh: float = -0.15       # коэффициент по RH (на 30 %); в журнале RH нет, параметр не калибруется
    temp_source: str = "block"  # "block": (блок 1 + блок 2)/2; "agent": температура зерна (низ + верх)/2
    overhead_h: float = 1.0   # ч накладных на каждый проход (загрузка, прогрев, переключение)
    # п.п.: насколько выходной влагомер показывает больше, чем та же проба после охлаждения. По умолчанию 0: поправка —
    # только та, что внесена в программу (пары проб, партии с лабораторией, «Настройки»). До 1.3 стояли +0,8 по четырём
    # пробам с фото журнала октября — ошибочно: влагомер откалиброван на сухое зерно, и его ошибка меняется от прохода
    # к проходу (с влажностью), это описывает k_w по парам проб, а не одно число
    out_bias: float = 0.0
    lag_k: float = 1.0        # доля времени пребывания (m_dryer/Q), на которую выход отстаёт от входа
    calibrated: bool = True
    calib_info: str = "стартовая: журнал 18–25.10.2026, 4 прохода, без поправки влагомера, ошибка w_вых ±0,43 п.п."
    # --- v0.4 ---
    b_amb: float = 0.03       # эмпир.: +3 % R на каждые +10 °C наружного воздуха (прогрев зерна; оценка по физ. модели)
    model_kind: str = "empirical"   # "empirical" | "physics"
    eta: float = 0.43         # физ.: КПД слоя — доля времени в горячей зоне, «работающая» как тонкий слой (калибруется)
    f_hot: float = 0.80       # физ.: доля объёма сушилки в горячей зоне (по ощущению 80–85 %)
    kappa: float = 1.0        # физ.: средняя по слою T воздуха = T_мокр + kappa·(T_агента − T_мокр); 1 — T агента
    phys_info: str = "стартовая: журнал 18–25.10.2026, η = 0,43, без поправки влагомера, ошибка w_вых ±0,63 п.п."
    tg_a: float = 5.1         # T зерна ≈ tg_a + tg_b·T_блоков + tg_c·(W_вх − 22), по журналу (±2,3 °C)
    tg_b: float = 0.42
    tg_c: float = -0.46
    t_agent_max: float = 0.0  # ограничение T агента (блоков), °C; 0 — не задано
    t_grain_max: float = 0.0  # ограничение T нагрева зерна, °C; 0 — не задано
    # --- v0.5 ---
    out_kw: float = 0.0       # поправка влагомера зависит от показания: + out_kw·(W_пок − 20)
    out_kt: float = 0.0       # ... и от T зерна по датчикам сушилки: + out_kt·(T − 30)
    rate_sigma: float = 0.05  # относит. ошибка скорости сушки (1σ) — по проверке прогноза на журнале
    rate_sigma_info: str = "стартовая: прогноз 3 проходов октября, ±5 %"
    rate_scale: float = 1.0   # множитель скорости сушки (только для расчёта допусков; в настройках всегда 1)
    # --- v0.8: честные допуски (заполняются калибровкой) ---
    unc_feats: Optional[List[str]] = None         # коэффициенты ln R, для которых известна ковариация («const», «b_w»…)
    unc_cov: Optional[List[List[float]]] = None   # их ковариация (с поправкой на зависимость строк внутри прохода)
    unc_lev: float = 0.0      # типичная дисперсия ln R по строкам журнала — её уже покрывает rate_sigma
    struct_base: float = 0.08  # типичное относит. расхождение эмпирической и физической моделей на проходах журнала
    explored: Optional[dict] = None   # изученные диапазоны журнала: {"d": [мин, макс], "t": [...], "w": [...]}

    @classmethod
    def from_dict(cls, d: dict) -> "DryerParams":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    def to_dict(self) -> dict:
        return asdict(self)


def _table(p: DryerParams) -> Optional[Tuple[List[float], List[float]]]:
    t = p.q_table
    if not t or len(t) < 2:
        return None
    pts = sorted((float(a), float(b)) for a, b in t)
    xs = [a for a, _ in pts]
    ys = [b for _, b in pts]
    if any(ys[i] >= ys[i + 1] for i in range(len(ys) - 1)):
        return None
    return xs, ys


def _interp(x: float, xs: Sequence[float], ys: Sequence[float]) -> float:
    if x <= xs[0]:
        # от последней точки таблицы к нулю линейно
        return ys[0] * x / xs[0] if xs[0] > 0 else ys[0]
    if x >= xs[-1]:
        if len(xs) >= 2 and xs[-1] > xs[-2]:
            slope = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
            return ys[-1] + slope * (x - xs[-1])
        return ys[-1]
    i = bisect_left(xs, x)
    x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def q_at(p: DryerParams, d: float) -> float:
    """Производительность выгрузки Q (т/ч) при D секундах из 100."""
    d = max(0.0, float(d))
    tb = _table(p)
    if tb is None:
        return p.q100 * d / 100.0
    return max(0.0, _interp(d, tb[0], tb[1]))


def d_at(p: DryerParams, q: float) -> float:
    """Обратное: какое D даёт производительность q (т/ч)."""
    tb = _table(p)
    if tb is None:
        return 100.0 * q / p.q100 if p.q100 > 0 else 0.0
    return _interp(q, tb[1], tb[0])


def q_full(p: DryerParams) -> float:
    return q_at(p, 100.0)


@dataclass
class Conditions:
    """Условия сушки."""

    t_proc: float = 60.0   # температура процесса (по выбранному источнику), °C
    t_amb: float = 10.0    # наружный воздух, °C
    rh: float = 70.0       # наружный воздух, %


@dataclass
class PassResult:
    k: int
    w_in: float
    w_out: float
    d: float
    q: float               # т/ч выгрузки
    m_in: float            # т на входе
    m_out: float           # т на выходе
    water: float           # т воды снято
    time_h: float          # ч (без накладных)
    rate: float            # т воды/ч
    residence_min: float   # мин в сушилке
    feasible: bool = True   # False: желаемый съём меньше минимального при D=100 (щадящее нельзя)
    note: str = ""
    t_proc: float = 0.0
    t_grain: float = 0.0   # ожидаемая T зерна, °C (0 — не считалась)

    @property
    def dw(self) -> float:
        return self.w_in - self.w_out


@dataclass
class Plan:
    name: str
    passes: List[PassResult]
    w_target: float
    overhead_h: float
    start: Optional[datetime] = None
    cool_h: float = 0.0          # финальное охлаждение на D=100 (горелки выкл.)
    bias: float = 0.0            # поправка выходного влагомера, п.п.

    @property
    def work_h(self) -> float:
        return sum(p.time_h for p in self.passes)

    @property
    def total_time_h(self) -> float:
        return self.work_h + self.overhead_h * len(self.passes) + self.cool_h

    @property
    def end(self) -> Optional[datetime]:
        return self.start + timedelta(hours=self.total_time_h) if self.start else None

    @property
    def water_total(self) -> float:
        return sum(p.water for p in self.passes)

    @property
    def m_final(self) -> float:
        return self.passes[-1].m_out

    @property
    def w_final(self) -> float:
        return self.passes[-1].w_out

    @property
    def max_dw(self) -> float:
        return max(p.dw for p in self.passes)

    @property
    def reaches_target(self) -> bool:
        return self.w_final <= self.w_target + 0.05

    @property
    def all_feasible(self) -> bool:
        return all(p.feasible for p in self.passes)

    def verdict(self, soft: float, hard: float) -> Tuple[int, str]:
        """(уровень 0 — в лимите, 1 — допустимо, 2 — плохо; текст)."""
        if not self.reaches_target:
            return 2, f"не достигает цели ({self.w_final:.1f} %)"
        mx = self.max_dw
        if mx <= soft + 1e-9:
            level, text = 0, f"в лимите (≤{soft:g} п.п.)"
        elif mx <= hard + 1e-9:
            level, text = 1, f"съём до {mx:.1f} п.п."
        else:
            return 2, f"съём {mx:.1f} п.п. > {hard:g}"
        if not self.all_feasible:
            level = max(level, 1)
            text += "; D=100 — предел"
        over = self.w_target - self.w_final
        if over > 0.5:
            level = max(level, 1)
            text += f"; пересушка до {self.w_final:.1f} %"
        return level, text

    def timeline(self) -> List[Tuple[float, float]]:
        """Точки (время от старта, ч; средняя влажность всей партии, %).

        Внутри прохода переработанная доля партии имеет w_вых, остальная — w_вх.
        Усреднение идёт по сухому веществу (через u = w / (100 - w)).
        """
        pts = [(0.0, self.passes[0].w_in)]
        t = 0.0
        last = len(self.passes) - 1
        for i, ps in enumerate(self.passes):
            u_in = ps.w_in / (100.0 - ps.w_in)
            u_out = ps.w_out / (100.0 - ps.w_out)
            steps = 10
            for j in range(1, steps + 1):
                f = j / steps
                u = f * u_out + (1.0 - f) * u_in
                pts.append((t + f * ps.time_h, 100.0 * u / (1.0 + u)))
            t += ps.time_h
            if i < last and self.overhead_h > 0:
                t += self.overhead_h
                pts.append((t, ps.w_out))
        if self.cool_h > 0:
            t += self.overhead_h + self.cool_h
            pts.append((t, self.passes[-1].w_out))
        return pts


# ----------------------------------------------------------------------------
# Модель расхода влаги и решатель прохода
# ----------------------------------------------------------------------------

def water_rate(p: DryerParams, w_avg: float, c: Conditions) -> float:
    """Расход влаги R по эмпирической модели, т воды/ч."""
    x_w = max(w_avg - p.w_eq, 0.05) / max(p.w_ref - p.w_eq, 0.1)
    x_t = max(c.t_proc - p.t_amb_ref, 1.0) / max(p.t_ref - p.t_amb_ref, 1.0)
    return (p.r_ref * p.rate_scale * (x_w ** p.b_w) * (x_t ** p.b_dt) * exp(p.b_amb * (c.t_amb - p.t_amb_ref) / 10.0)
            * exp(p.b_rh * (c.rh - p.rh_ref) / 30.0))


W_REF_METER = 20.0   # опорные точки поправки влагомера
T_REF_METER = 30.0


def meter_correction(p: DryerParams, w_read: float, t_grain: Optional[float] = None) -> float:
    """Насколько выходной влагомер завышает влажность, п.п.: a + k_w·(W − 20) + k_t·(T − 30)."""
    corr = p.out_bias + p.out_kw * (w_read - W_REF_METER)
    if t_grain is not None and p.out_kt:
        corr += p.out_kt * (t_grain - T_REF_METER)
    return corr


def true_from_read(p: DryerParams, w_read: float, t_grain: Optional[float] = None) -> float:
    """Показание выходного влагомера -> «истинная» влажность (как после охлаждения), %."""
    return w_read - meter_correction(p, w_read, t_grain)


def read_from_true(p: DryerParams, w_true: float, t_grain: Optional[float] = None) -> float:
    """Обратное: что покажет выходной влагомер при истинной влажности w_true."""
    tt = p.out_kt * (t_grain - T_REF_METER) if (t_grain is not None and p.out_kt) else 0.0
    k = 1.0 - p.out_kw
    if abs(k) < 1e-6:
        k = 1e-6
    return (w_true + p.out_bias - p.out_kw * W_REF_METER + tt) / k


def grain_temp(p: DryerParams, t_proc: float, w_in: float) -> float:
    """Ожидаемая температура зерна в сушилке (средняя по двум датчикам), °C — регрессия по журналу."""
    return p.tg_a + p.tg_b * t_proc + p.tg_c * (w_in - 22.0)


def _clamp_d(d: float) -> float:
    return min(100.0, max(1.0, d))


def solve_pass(p: DryerParams, c: Conditions, w_in: float, m_in: float, d: float, k: int = 1) -> PassResult:
    """Проход партии (массой m_in, влажностью w_in) при выгрузке D."""
    d = _clamp_d(d)
    q = q_at(p, d)

    if p.model_kind == "physics":
        from .physics import pass_out
        w_out = pass_out(p, c, w_in, d)
    elif w_in <= p.w_eq + 0.05:
        w_out = w_in
    else:
        def excess(w_out_: float) -> float:
            need = q * (w_in - w_out_) / (100.0 - w_in)       # сколько воды надо снять в час
            cap = water_rate(p, 0.5 * (w_in + w_out_), c)     # сколько сушилка может
            return need - cap

        lo, hi = p.w_eq, w_in
        if excess(lo) <= 0:
            w_out = lo
        else:
            for _ in range(70):
                mid = 0.5 * (lo + hi)
                if excess(mid) > 0:
                    lo = mid
                else:
                    hi = mid
            w_out = 0.5 * (lo + hi)

    m_out = m_in * (100.0 - w_in) / (100.0 - w_out)
    water = m_in - m_out
    time_h = m_out / q
    rate = water / time_h if time_h > 0 else 0.0
    return PassResult(
        k=k, w_in=w_in, w_out=w_out, d=d, q=q, m_in=m_in, m_out=m_out, water=water,
        time_h=time_h, rate=rate, residence_min=p.m_dryer / q * 60.0, t_proc=c.t_proc,
        t_grain=grain_temp(p, c.t_proc, w_in),
    )


# ----------------------------------------------------------------------------
# То же для массивов (numpy): калибровка и «прогноз против факта» считают проход для сотен–тысяч
# наблюдений сразу. Арифметика та же, что в solve_pass, результаты совпадают до округления double.
# ----------------------------------------------------------------------------

def q_at_arr(p: DryerParams, d) -> np.ndarray:
    """q_at для массива D."""
    d = np.maximum(np.asarray(d, dtype=float), 0.0)
    tb = _table(p)
    if tb is None:
        return p.q100 * d / 100.0
    xs, ys = np.asarray(tb[0]), np.asarray(tb[1])
    out = np.interp(d, xs, ys)
    out = np.where(d <= xs[0], ys[0] * d / xs[0] if xs[0] > 0 else ys[0], out)
    if len(xs) >= 2 and xs[-1] > xs[-2]:
        slope = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
        out = np.where(d >= xs[-1], ys[-1] + slope * (d - xs[-1]), out)
    else:
        out = np.where(d >= xs[-1], ys[-1], out)
    return np.maximum(out, 0.0)


def water_rate_arr(p: DryerParams, w_avg, t_proc, t_amb, rh) -> np.ndarray:
    """water_rate для массивов."""
    x_w = np.maximum(np.asarray(w_avg, dtype=float) - p.w_eq, 0.05) / max(p.w_ref - p.w_eq, 0.1)
    x_t = np.maximum(np.asarray(t_proc, dtype=float) - p.t_amb_ref, 1.0) / max(p.t_ref - p.t_amb_ref, 1.0)
    return (p.r_ref * p.rate_scale * (x_w ** p.b_w) * (x_t ** p.b_dt)
            * np.exp(p.b_amb * (np.asarray(t_amb, dtype=float) - p.t_amb_ref) / 10.0)
            * np.exp(p.b_rh * (np.asarray(rh, dtype=float) - p.rh_ref) / 30.0))


def solve_out_arr(p: DryerParams, w_in, d, t_proc, t_amb, rh) -> Tuple[np.ndarray, np.ndarray]:
    """Влажность на выходе прохода (истинная, %) и Q (т/ч) для массивов входа — как solve_pass(...).w_out."""
    w_in = np.asarray(w_in, dtype=float)
    d = np.clip(np.asarray(d, dtype=float), 1.0, 100.0)
    t_proc, t_amb, rh = (np.broadcast_to(np.asarray(x, dtype=float), w_in.shape) for x in (t_proc, t_amb, rh))
    q = q_at_arr(p, d)
    if p.model_kind == "physics":
        from .physics import pass_out_arr
        return pass_out_arr(p, w_in, d, t_proc, t_amb, rh), q
    active = w_in > p.w_eq + 0.05
    # множитель R, не зависящий от w (порядок умножений как в water_rate)
    x_t = np.maximum(t_proc - p.t_amb_ref, 1.0) / max(p.t_ref - p.t_amb_ref, 1.0)
    k_t = (x_t ** p.b_dt)
    k_amb = np.exp(p.b_amb * (t_amb - p.t_amb_ref) / 10.0)
    k_rh = np.exp(p.b_rh * (rh - p.rh_ref) / 30.0)
    w_den = max(p.w_ref - p.w_eq, 0.1)
    base = p.r_ref * p.rate_scale

    def excess(w_out_: np.ndarray) -> np.ndarray:
        need = q * (w_in - w_out_) / (100.0 - w_in)
        x_w = np.maximum(0.5 * (w_in + w_out_) - p.w_eq, 0.05) / w_den
        return need - base * (x_w ** p.b_w) * k_t * k_amb * k_rh

    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        lo = np.full_like(w_in, p.w_eq)
        hi = w_in.copy()
        at_eq = excess(lo) <= 0
        for _ in range(70):
            mid = 0.5 * (lo + hi)
            pos = excess(mid) > 0
            lo = np.where(pos, mid, lo)
            hi = np.where(pos, hi, mid)
        w_out = np.where(active, np.where(at_eq, p.w_eq, 0.5 * (lo + hi)), w_in)
    return w_out, q


def pass_rate_arr(w_in, w_out, q) -> np.ndarray:
    """Расход влаги прохода (т воды/ч) при m_вх = 1 т — как solve_pass(...).rate."""
    m_out = (100.0 - np.asarray(w_in, dtype=float)) / (100.0 - np.asarray(w_out, dtype=float))
    with np.errstate(divide="ignore", invalid="ignore"):
        time_h = m_out / q
        return np.where(time_h > 0, (1.0 - m_out) / time_h, 0.0)


def d_for_target(p: DryerParams, c: Conditions, w_in: float, w_target: float) -> float:
    """D (с), при котором проход снимает влажность с w_in до w_target.

    Может вернуть значение >100 (сушилка не успевает) или <1 (нужна скорость ниже минимальной).
    """
    if w_target >= w_in:
        return 0.0
    if p.model_kind == "physics":
        from .physics import d_for_target as phys_d
        return phys_d(p, c, w_in, w_target)
    w_target = max(w_target, p.w_eq + 0.1)
    if w_in <= w_target:
        return 0.0
    r = water_rate(p, 0.5 * (w_in + w_target), c)
    q = r * (100.0 - w_in) / (w_in - w_target)
    return d_at(p, q)


# ----------------------------------------------------------------------------
# Планы
# ----------------------------------------------------------------------------

def _cond_k(c: Conditions, t_list: Optional[Sequence[float]], k: int) -> Conditions:
    if not t_list:
        return c
    return Conditions(float(t_list[min(k, len(t_list)) - 1]), c.t_amb, c.rh)


def _cool_hours(p: DryerParams, m_final: float, cool: bool) -> float:
    return m_final / q_full(p) if cool and q_full(p) > 0 else 0.0


def plan_by_passes(p: DryerParams, c: Conditions, m0: float, w0: float, wk: float, n: int,
                   start: Optional[datetime] = None, t_list: Optional[Sequence[float]] = None,
                   cool: bool = False) -> Plan:
    """План из n проходов с равным съёмом; D по проходам подбирается и округляется до целых секунд.

    t_list — температура блоков по проходам (если короче n, последняя повторяется).
    """
    passes: List[PassResult] = []
    w, m = w0, m0
    for k in range(1, n + 1):
        if k > 1 and w <= wk + 0.05:
            break
        ck = _cond_k(c, t_list, k)
        target = wk if k == n else w - (w - wk) / (n - k + 1)
        d_req = d_for_target(p, ck, w, target)
        if k == n:
            # последний проход: D вниз до целых секунд, чтобы цель точно достигалась
            d = float(int(_clamp_d(d_req) + 1e-9))
        else:
            d = float(round(_clamp_d(d_req)))
        ps = solve_pass(p, ck, w, m, d, k)
        if d_req > 100.5:
            # желаемый съём меньше минимально возможного при D=100: щадящее не получится
            ps.feasible = False
            ps.note = f"предел D=100: меньше {ps.dw:.1f} п.п. за проход не снять"
        elif d_req < 1.0:
            ps.note = "D<1 с: минимальная скорость, возможна пересушка"
        passes.append(ps)
        w, m = ps.w_out, ps.m_out
    return Plan(name=f"{len(passes)} прох.", passes=passes, w_target=wk, overhead_h=p.overhead_h,
                start=start, cool_h=_cool_hours(p, m, cool), bias=p.out_bias)


def plan_manual(p: DryerParams, c: Conditions, m0: float, w0: float, wk: float,
                d_list: Sequence[float], start: Optional[datetime] = None, max_passes: int = 12,
                t_list: Optional[Sequence[float]] = None, cool: bool = False,
                name: Optional[str] = None) -> Plan:
    """План с заданным D по проходам. Если список кончился, а цель не достигнута, последнее D
    повторяется; последний проход подрезается, чтобы не пересушить."""
    d_list = [_clamp_d(float(x)) for x in d_list]
    passes: List[PassResult] = []
    w, m = w0, m0
    k = 0
    while k < max_passes:
        k += 1
        ck = _cond_k(c, t_list, k)
        explicit = k <= len(d_list)
        d = d_list[k - 1] if explicit else d_list[-1]
        ps = solve_pass(p, ck, w, m, d, k)
        if not explicit and ps.w_out < wk - 0.05:
            d = float(round(_clamp_d(d_for_target(p, ck, w, wk))))
            ps = solve_pass(p, ck, w, m, d, k)
            ps.note = "последний проход подрезан по цели"
        passes.append(ps)
        w, m = ps.w_out, ps.m_out
        if k >= len(d_list) and w <= wk + 0.05:
            break
    label = "/".join(f"{x:g}" for x in d_list)
    return Plan(name=name or f"Ручной D={label}", passes=passes, w_target=wk, overhead_h=p.overhead_h,
                start=start, cool_h=_cool_hours(p, m, cool), bias=p.out_bias)


def plan_simple(p: DryerParams, m0: float, w0: float, wk: float, q_list: Sequence[float], n: int,
                start: Optional[datetime] = None, cool: bool = False) -> Plan:
    """Простой расчёт по заданной производительности (без модели расхода влаги).

    Съём делится поровну на n проходов. Время прохода = масса выгруженного зерна / Q.
    q_list — производительность по проходам, т/ч; если список короче n, последнее значение повторяется.
    """
    n = max(1, int(n))
    qs = [max(float(x), 0.1) for x in q_list] or [q_full(p)]
    passes: List[PassResult] = []
    w, m = w0, m0
    for k in range(1, n + 1):
        q = qs[k - 1] if k <= len(qs) else qs[-1]
        w_out = wk if k == n else w - (w - wk) / (n - k + 1)
        m_out = m * (100.0 - w) / (100.0 - w_out)
        water = m - m_out
        time_h = m_out / q
        passes.append(PassResult(
            k=k, w_in=w, w_out=w_out, d=d_at(p, q), q=q,
            m_in=m, m_out=m_out, water=water, time_h=time_h,
            rate=water / time_h if time_h > 0 else 0.0, residence_min=p.m_dryer / q * 60.0))
        w, m = w_out, m_out
    q_txt = "/".join(f"{x:g}" for x in qs[:n])
    return Plan(name=f"{n} прох., Q={q_txt} т/ч", passes=passes, w_target=wk, overhead_h=p.overhead_h,
                start=start, cool_h=_cool_hours(p, m, cool), bias=p.out_bias)


def scenarios(p: DryerParams, c: Conditions, m0: float, w0: float, wk: float, n_max: int = 6,
              start: Optional[datetime] = None, t_list: Optional[Sequence[float]] = None,
              cool: bool = False) -> List[Plan]:
    return [plan_by_passes(p, c, m0, w0, wk, n, start, t_list, cool) for n in range(1, n_max + 1)]


def recommend(plans: Sequence[Plan], soft: float, hard: float) -> Optional[int]:
    """Индекс рекомендуемого плана: минимум проходов в мягком лимите, иначе в жёстком."""
    for level in (0, 1):
        for i, pl in enumerate(plans):
            if pl.verdict(soft, hard)[0] <= level:
                return i
    return None


# ----------------------------------------------------------------------------
# Допуск прогноза
# ----------------------------------------------------------------------------

Z90 = 1.645   # двусторонний 90 % интервал


def scaled(p: DryerParams, k: float) -> DryerParams:
    from dataclasses import replace
    return replace(p, rate_scale=max(k, 0.05))


# ----------------------------------------------------------------------------
# Честные допуски: σ скорости сушки растёт там, где модель опирается на экстраполяцию
# ----------------------------------------------------------------------------
#
#   σ² = σ_проверки² + σ_коэфф² + σ_структ²
#
# σ_проверки (rate_sigma) — относительная ошибка честных прогнозов съёма на журнале («прогноз против факта»):
#   так модель ошибается в режимах, похожих на изученные.
# σ_коэфф — разброс подобранных коэффициентов ln R (ковариация регрессии, с поправкой на то, что строки одного
#   прохода не независимы), но только СВЕРХ типичного для строк журнала: внутри изученного он уже входит в
#   σ_проверки, а при уходе W, T, погоды от изученного растёт.
# σ_структ — превышение расхождения эмпирической и физической моделей над типичным на проходах журнала (половина
#   превышения как 1σ). Эмпирическая модель не знает о времени пребывания зерна иначе как через баланс, физическая
#   знает через кинетику сушки: там, где они начинают расходиться сильнее обычного (D, T, W за пределами журнала),
#   ни одной из них нельзя верить так же, как внутри.

def _feat_vec(p: DryerParams, w_avg: float, t_proc: float, t_amb: float, rh: float) -> List[float]:
    out: List[float] = []
    for k in p.unc_feats or []:
        if k == "const":
            out.append(1.0)
        elif k == "b_w":
            out.append(log(max(w_avg - p.w_eq, 0.05) / max(p.w_ref - p.w_eq, 0.1)))
        elif k == "b_dt":
            out.append(log(max(t_proc - p.t_amb_ref, 1.0) / max(p.t_ref - p.t_amb_ref, 1.0)))
        elif k == "b_amb":
            out.append((t_amb - p.t_amb_ref) / 10.0)
        elif k == "b_rh":
            out.append((rh - p.rh_ref) / 30.0)
        else:
            out.append(0.0)
    return out


def param_sigma(p: DryerParams, w_avg: float, t_proc: float, t_amb: float, rh: float) -> float:
    """σ_коэфф: относительная неопределённость скорости сушки из-за разброса коэффициентов, сверх типичной."""
    if not p.unc_feats or not p.unc_cov:
        return 0.0
    f = np.asarray(_feat_vec(p, w_avg, t_proc, t_amb, rh))
    c = np.asarray(p.unc_cov, dtype=float)
    if c.shape != (len(f), len(f)):
        return 0.0
    v = float(f @ c @ f)
    return sqrt(max(v - p.unc_lev, 0.0))


def struct_sigma(p: DryerParams, dw_main: float, dw_other: float) -> float:
    """σ_структ: половина превышения относительного расхождения моделей над типичным на журнале."""
    if dw_main <= 0.05:
        return 0.0
    delta = abs(dw_other - dw_main) / max(dw_main, 0.5)
    return 0.5 * max(delta - p.struct_base, 0.0)


EXPLORED_MARGIN = {"d": 2.0, "t": 2.0, "w": 1.0}   # запас к изученным диапазонам: D, с; T, °C; W, п.п.


def explored_zone(p: DryerParams) -> Optional[Dict[str, Tuple[float, float]]]:
    """Проверенная журналом зона режима: {"d": (мин, макс), "t": (мин, макс)} — изученные калибровкой диапазоны D и
    T блоков с запасом EXPLORED_MARGIN. Внутри неё прогноз модели проверен журналом (вне обучения ±0,4–0,5 п.п. на
    ноябрьском журнале), за её пределами точность не проверена. None — модель не калибровалась по журналу."""
    ex = p.explored or {}
    try:
        d = (float(ex["d"][0]) - EXPLORED_MARGIN["d"], float(ex["d"][1]) + EXPLORED_MARGIN["d"])
        t = (float(ex["t"][0]) - EXPLORED_MARGIN["t"], float(ex["t"][1]) + EXPLORED_MARGIN["t"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return {"d": d, "t": t, "d_ex": (float(ex["d"][0]), float(ex["d"][1])),
            "t_ex": (float(ex["t"][0]), float(ex["t"][1]))}


def explored_flags(p: DryerParams, d_vals: Sequence[float], t_vals: Sequence[float], w_vals: Sequence[float]) -> List[str]:
    """Что выходит за изученные журналом диапазоны (с небольшим запасом)."""
    ex = p.explored or {}
    out: List[str] = []
    for key, vals, unit, margin, name in (("d", d_vals, " с", EXPLORED_MARGIN["d"], "D"),
                                          ("t", t_vals, " °C", EXPLORED_MARGIN["t"], "T блоков"),
                                          ("w", w_vals, " %", EXPLORED_MARGIN["w"], "W вх")):
        rng = ex.get(key)
        if not rng or not vals:
            continue
        lo, hi = float(rng[0]), float(rng[1])
        bad = [v for v in vals if v < lo - margin or v > hi + margin]
        if bad:
            shown = " / ".join(f"{v:.0f}" for v in bad[:5])
            out.append(f"{name} {shown}{unit} (изучено {lo:.0f}–{hi:.0f})")
    return out


def other_model_final(p: DryerParams, plan: "Plan", c: Conditions, t_list: Optional[Sequence[float]] = None) -> float:
    """Итоговая влажность того же плана (те же D по проходам) по второй модели."""
    from dataclasses import replace
    other = replace(p, model_kind="empirical" if p.model_kind == "physics" else "physics")
    w, m = plan.passes[0].w_in, plan.passes[0].m_in
    for k, ps in enumerate(plan.passes, start=1):
        r = solve_pass(other, _cond_k(c, t_list, k), w, m, ps.d, k)
        w, m = r.w_out, r.m_out
    return w


def plan_sigma(p: DryerParams, plan: "Plan", c: Conditions, w_other_final: Optional[float] = None
               ) -> Tuple[float, Dict]:
    """σ скорости сушки для плана: √(σ_проверки² + σ_коэфф² + σ_структ²) и из чего она сложилась."""
    num = den = 0.0
    for ps in plan.passes:
        s = param_sigma(p, 0.5 * (ps.w_in + ps.w_out), ps.t_proc, c.t_amb, c.rh)
        num += s * s * max(ps.time_h, 1e-6)
        den += max(ps.time_h, 1e-6)
    s_par = sqrt(num / den) if den else 0.0
    s_str = 0.0
    if w_other_final is not None and plan.passes:
        w0 = plan.passes[0].w_in
        s_str = struct_sigma(p, w0 - plan.w_final, w0 - w_other_final)
    s_tot = sqrt(max(p.rate_sigma, 0.0) ** 2 + s_par ** 2 + s_str ** 2)
    flags = explored_flags(p, [x.d for x in plan.passes], [x.t_proc for x in plan.passes], [x.w_in for x in plan.passes])
    return s_tot, {"check": p.rate_sigma, "param": s_par, "struct": s_str, "flags": flags}


def sigma_text(info: Dict) -> str:
    z = Z90 * 100
    tot = sqrt(info["check"] ** 2 + info["param"] ** 2 + info["struct"] ** 2)
    txt = (f"Допуск 90 %: скорость сушки ±{z * tot:.0f} % = √(проверка на журнале {z * info['check']:.0f}² + "
           f"коэффициенты {z * info['param']:.0f}² + расхождение моделей {z * info['struct']:.0f}²)")
    if info.get("flags"):
        txt += ". Вне изученного: " + "; ".join(info["flags"])
    return txt


def time_range(build, p: DryerParams, z: float = Z90) -> Tuple[float, float]:
    """Интервал общего времени плана: build(params) -> Plan, скорость сушки ×(1 ± z·σ).

    Быстрее сушка — короче время; возвращает (меньшее, большее)."""
    s = max(p.rate_sigma, 0.0)
    a = build(scaled(p, 1.0 + z * s)).total_time_h
    b = build(scaled(p, 1.0 - z * s)).total_time_h
    return min(a, b), max(a, b)


# ----------------------------------------------------------------------------
# Рециркуляция: сушилка заполнена партией (обычно ≈ вместимость), зерно ходит по кругу через шахту
# ----------------------------------------------------------------------------

@dataclass
class RecircResult:
    hours: Optional[float]          # время до цели (None — не достигается за max_h)
    points: List[Tuple[float, float]]
    w_end: float
    water: float
    m_end: float
    note: str = ""


def recirc(p: DryerParams, c: Conditions, m: float, w0: float, w_target: float, max_h: float = 48.0,
           dt_h: float = 0.05) -> RecircResult:
    """Сушка содержимого сушилки на рециркуляции.

    Эмпирическая модель: содержимое перемешано, его влажность w(t) общая; вода уходит со скоростью
    R(w) т/ч (та же модель, что для прохода, при w_ср = w). Сухое вещество DM постоянно:
        du/dt = −R(w) / DM,   u = w / (100 − w).
    Физическая: каждая зерновка находится в горячей зоне долю времени f_hot, кривая тонкого слоя по
    накопленному времени η·f_hot·t.
    """
    dm = m * (100.0 - w0) / 100.0
    pts: List[Tuple[float, float]] = [(0.0, w0)]
    hit: Optional[float] = None
    note = ""
    if m < 0.9 * p.m_dryer:
        note = (f"масса {m:g} т меньше вместимости ({p.m_dryer:g} т): шахта неполная, воздух идёт мимо зерна — "
                f"расчёт оптимистичен")
    if p.model_kind == "physics":
        from .physics import recirc_w
        t = 0.0
        while t < max_h - 1e-9:
            t += dt_h
            w = recirc_w(p, c, w0, t)
            pts.append((t, w))
            if hit is None and w <= w_target:
                hit = t
                break
    else:
        u = w0 / (100.0 - w0)
        t = 0.0
        w = w0
        while t < max_h - 1e-9:
            r = water_rate(p, w, c) if w > p.w_eq + 0.05 else 0.0
            u_new = max(u - r / dm * dt_h, p.w_eq / (100.0 - p.w_eq))
            t += dt_h
            w_new = 100.0 * u_new / (1.0 + u_new)
            pts.append((t, w_new))
            if w_new <= w_target < w:
                hit = t - dt_h * (w_target - w_new) / (w - w_new)   # линейно внутри шага
                pts[-1] = (hit, w_target)
                w, u = w_target, w_target / (100.0 - w_target)
                break
            w, u = w_new, u_new
    w_end = pts[-1][1]
    m_end = dm * 100.0 / (100.0 - w_end)
    return RecircResult(hours=hit, points=pts, w_end=w_end, water=m - m_end, m_end=m_end,
                        note=note or ("" if hit is not None else f"за {max_h:g} ч цель не достигается"))
