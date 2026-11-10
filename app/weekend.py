"""Масса на выходные: сколько сырого зерна намолотить, чтобы сушилка работала до выхода хозяйства на работу.

Задача, обратная плану партии. План партии: масса → время. Здесь: окно работы сушилки → масса. Окно — от начала
сушки выходной партии до выхода хозяйства, минус плановые остановки (зачистка, ТО). Масса — сколько сырого зерна
сушилка успеет высушить в этом окне до цели Wк со всеми проходами и финальным охлаждением.

Почему решение прямое. Время партии линейно по массе:  T(M) = a·M + b.
  a — часы на тонну сырого зерна: Σ по проходам (масса на выходе прохода / Q прохода) + охлаждение (масса / Q при D = 100);
      масса на выходе прохода пропорциональна исходной, Q задаёт D, а D — только съём за проход (не масса);
  b — накладные: overhead_h на каждый проход (загрузка, прогрев, переключение).
Число проходов и D по проходам от массы не зависят (их выбирает съём: минимум проходов в лимите, как в «План сушки
партии»), поэтому  M = (H − b) / a.  Решение проверяется пересчётом плана; если план окажется нелинейным по массе
(другая модель), масса ищется делением пополам.

Допуск 90 %: та же схема (то же число проходов) при скорости сушки ×(1 ± 1,645·σ), σ — честный допуск плана
(model.plan_sigma: проверка на журнале + разброс коэффициентов + расхождение моделей). Быстрее сушка — больше масса
успеет высохнуть. Отсюда три варианта: «точно успеть к выходу» (масса при медленной сушке), расчётная и «без простоя»
(масса при быстрой сушке) — и для каждого, когда сушилка закончит при обычной, быстрой и медленной сушке.

Сырое зерно до 1-го прохода лежит и портится. Ориентир — допустимый срок хранения зерна (потеря 0,5 % сухого
вещества, примерно одна ступень класса) по таблице University of Arkansas FSA1058 «Safe Grain Storage Period»
(Sadaka, Atungulu, Olatunde, 2016): дни по температуре зерна и влажности; таблица для зерна, которое держат
холодным (вентилируемое хранение) — без вентиляции сырое зерно греется и портится быстрее.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Sequence, Tuple

from . import model as mdl

# ----------------------------------------------------------------------------
# Допустимый срок хранения сырого зерна (дни): University of Arkansas FSA1058, табл. 1, 0,5 % потери сухого вещества
# (профиль культуры по умолчанию; для другой культуры — своя таблица)
# ----------------------------------------------------------------------------

STORAGE_MC = (15.0, 17.0, 19.0, 21.0, 23.0, 25.0, 30.0)               # влажность, % (на общую массу)
STORAGE_TF = (35.0, 40.0, 45.0, 50.0, 55.0, 60.0, 65.0, 70.0, 75.0)   # температура зерна, °F
STORAGE_DAYS = (
    (2126, 671, 295, 161, 102, 72, 41),     # 35 °F (1,7 °C)
    (1413, 448, 197, 107, 68, 48, 27),      # 40 °F (4,4 °C)
    (931, 299, 131, 72, 45, 32, 18),        # 45 °F (7,2 °C)
    (621, 199, 88, 48, 30, 21, 12),         # 50 °F (10 °C)
    (414, 133, 58, 32, 21, 14, 8),          # 55 °F (12,8 °C)
    (275, 88, 39, 22, 14, 10, 6),           # 60 °F (15,6 °C)
    (206, 66, 29, 16, 11, 8, 5),            # 65 °F (18,3 °C)
    (154, 49, 22, 12, 8, 6, 4),             # 70 °F (21,1 °C)
    (115, 37, 16, 9, 6, 5, 3),              # 75 °F (23,9 °C)
)
STORAGE_SOURCE = ("University of Arkansas, FSA1058 «Safe Grain Storage Period» (2016), табл. 1, "
                  "потеря 0,5 % сухого вещества")


def _lerp_log(x: float, xs: Sequence[float], ys: Sequence[float]) -> float:
    """ln(y) линейно по x; за краями — продолжение крайнего отрезка."""
    if x <= xs[0]:
        i = 0
    elif x >= xs[-1]:
        i = len(xs) - 2
    else:
        i = max(k for k in range(len(xs) - 1) if xs[k] <= x)
    x0, x1 = xs[i], xs[i + 1]
    l0, l1 = math.log(ys[i]), math.log(ys[i + 1])
    return math.exp(l0 + (l1 - l0) * (x - x0) / (x1 - x0))


def allowable_storage_days(w: float, t_c: float) -> Tuple[Optional[float], str]:
    """Допустимый срок хранения сырого зерна, сут, при влажности w % и температуре зерна t_c °C (0,5 % потери
    сухого вещества) и пометка, если точка вне таблицы. Интерполяция ln(сут) по влажности и температуре.

    Ниже 1,7 °C берётся значение при 1,7 °C (холоднее — срок ещё больше: оценка с запасом); выше 23,9 °C и влажнее
    30 % — продолжение крайнего отрезка таблицы (пометка «вне таблицы»). Суше 15 % — None: хранится месяцами."""
    if w < STORAGE_MC[0]:
        return None, "суше 15 % — сырым не считается"
    t_f = t_c * 9.0 / 5.0 + 32.0
    flags = []
    if t_f > STORAGE_TF[-1]:
        flags.append(f"T выше {(STORAGE_TF[-1] - 32) * 5 / 9:.0f} °C")
    if w > STORAGE_MC[-1]:
        flags.append("влажность выше 30 %")
    t_use = max(t_f, STORAGE_TF[0])
    col = [_lerp_log(w, STORAGE_MC, row) for row in STORAGE_DAYS]       # по влажности в каждой строке температуры
    days = _lerp_log(t_use, STORAGE_TF, col)
    return days, ("вне таблицы: " + ", ".join(flags)) if flags else ""


# ----------------------------------------------------------------------------
# Масса на выходные
# ----------------------------------------------------------------------------

@dataclass
class WeekendInput:
    start: datetime                      # начало сушки выходной партии
    end: datetime                        # хозяйство выходит на работу — к этому времени партия высушена и охлаждена
    w0: float                            # влажность с поля, %
    wk: float                            # цель партии (после охлаждения), %
    stops_h: float = 0.0                 # плановые остановки за выходные (зачистка, ТО), ч
    already: float = 0.0                 # уже намолочено для этой партии, т
    capacity: float = 0.0                # оперативные ёмкости под партию, т (0 — не проверять)
    method: str = "model"                # "model" — по модели сушилки; "q" — по производительности и лимиту съёма
    q: float = 18.2                      # т/ч выгрузки (метод «q»)
    lim: float = 6.0                     # лимит съёма за проход, п.п. (метод «q»)
    soft: float = 6.0                    # желаемый / допустимый съём за проход (метод «model»), п.п.
    hard: float = 10.0
    cool: bool = True
    t_list: Optional[List[float]] = None
    passes: int = 0                      # число проходов задано (0 — авто: по лимиту съёма)


@dataclass
class WeekendOption:
    key: str                             # "safe" | "calc" | "full" | "bins"
    title: str
    mass: float
    end: datetime                        # окончание при обычной (расчётной) скорости сушки
    end_fast: Optional[datetime] = None  # при быстрой (верх допуска 90 %)
    end_slow: Optional[datetime] = None  # при медленной
    plan: Optional[mdl.Plan] = None
    what: str = ""                       # что будет — одной фразой


@dataclass
class WeekendResult:
    ok: bool
    message: str = ""
    hours: float = 0.0                   # часов работы сушилки в окне (без остановок)
    mass: float = 0.0                    # расчётная масса сырого зерна, т
    mass_lo: Optional[float] = None      # масса при медленной сушке (точно успеть)
    mass_hi: Optional[float] = None      # при быстрой (без простоя)
    plan: Optional[mdl.Plan] = None      # план расчётной массы
    n_passes: int = 0
    h_per_t: float = 0.0                 # ч работы на тонну сырого зерна (a)
    fixed_h: float = 0.0                 # накладные на проходы, ч (b)
    sigma: float = 0.0
    sigma_info: Optional[dict] = None
    options: List[WeekendOption] = field(default_factory=list)
    pass1_end: Optional[datetime] = None
    wet_wait_h: float = 0.0              # сырое зерно ждёт 1-го прохода (от начала партии), ч
    storage_days: Optional[float] = None
    storage_flag: str = ""
    storage_level: int = 0               # 0 — с запасом, 1 — больше половины срока, 2 — дольше срока
    harvest_not_before: Optional[datetime] = None   # убранное раньше этого рискует испортиться до 1-го прохода
    capacity_short: float = 0.0          # не хватает ёмкостей, т
    notes: List[str] = field(default_factory=list)


def _builder(p: mdl.DryerParams, c: mdl.Conditions, inp: WeekendInput
             ) -> Tuple[int, Callable[[mdl.DryerParams, float], mdl.Plan], str]:
    """(число проходов, план(параметры, масса), пометка). Число проходов выбирается один раз — от массы не зависит:
    заданное (inp.passes) или наименьшее в лимите съёма."""
    note = ""
    if inp.passes > 0:
        n = int(inp.passes)
        dw = (inp.w0 - inp.wk) / n
        lim = inp.lim if inp.method == "q" else inp.hard
        if dw > lim + 1e-9:
            note = (f"задано {n} прох.: съём по {dw:.1f} п.п. за проход больше "
                    f"{'лимита' if inp.method == 'q' else 'допустимого'} {lim:g} п.п.")
    elif inp.method == "q":
        n = max(1, int(math.ceil((inp.w0 - inp.wk) / max(inp.lim, 0.1) - 1e-9)))
    else:
        plans = mdl.scenarios(p, c, 100.0, inp.w0, inp.wk, 6, inp.start, inp.t_list, inp.cool)
        k = mdl.recommend(plans, inp.soft, inp.hard)
        note = "" if k is not None else f"в допустимый съём {inp.hard:g} п.п. за проход не уложиться — взято 6 проходов"
        n = len((plans[k] if k is not None else plans[-1]).passes)
    if inp.method == "q":
        return n, (lambda pp, m: mdl.plan_simple(pp, m, inp.w0, inp.wk, [inp.q], n, inp.start, inp.cool)), note
    return n, (lambda pp, m: mdl.plan_by_passes(pp, c, m, inp.w0, inp.wk, n, inp.start, inp.t_list, inp.cool)), note


def mass_for_hours(build: Callable[[float], mdl.Plan], hours: float) -> Tuple[float, float, float]:
    """Масса, которую план build(масса) высушит ровно за hours часов, и прямая T(M) = a·M + b: (M, a, b).
    hours ≤ b — времени не хватает даже на накладные: M = 0."""
    t1, t2 = build(100.0).total_time_h, build(1100.0).total_time_h
    a = (t2 - t1) / 1000.0
    b = t1 - 100.0 * a
    if a <= 0:
        return 0.0, a, b
    if hours <= b:
        return 0.0, a, b
    m = (hours - b) / a
    if abs(build(m).total_time_h - hours) > 0.01:      # план нелинеен по массе — деление пополам
        lo, hi = 0.0, max(2.0 * m, 200.0)
        while build(hi).total_time_h < hours and hi < 1e6:
            hi *= 2.0
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            if build(mid).total_time_h < hours:
                lo = mid
            else:
                hi = mid
        m = 0.5 * (lo + hi)
    return m, a, b


def weekend_mass(p: mdl.DryerParams, c: mdl.Conditions, inp: WeekendInput) -> WeekendResult:
    """Сколько сырого зерна высушит сушилка от inp.start до inp.end (минус остановки) — см. описание модуля."""
    if inp.wk <= p.w_eq + 0.5:
        return WeekendResult(False, f"Целевая влажность должна быть выше равновесной ({p.w_eq:g} %) хотя бы на 0,5 п.п.")
    if inp.w0 <= inp.wk:
        return WeekendResult(False, "Влажность с поля не выше целевой: сушка не нужна.")
    window = (inp.end - inp.start).total_seconds() / 3600.0
    hours = window - max(inp.stops_h, 0.0)
    if window <= 0:
        return WeekendResult(False, "Выход хозяйства раньше начала сушки — проверьте даты.")
    res = WeekendResult(True, hours=hours)
    n, build, note = _builder(p, c, inp)
    if note:
        res.notes.append(note)
    res.n_passes = n
    m, a, b = mass_for_hours(lambda mm: build(p, mm), hours)
    res.h_per_t, res.fixed_h = a, b
    if m <= 0:
        res.ok = False
        res.message = (f"Окно {hours:.1f} ч не больше накладных на {n} прох. ({b:.1f} ч): партию начать и закончить "
                       f"до выхода не успеть.")
        return res
    res.mass = m
    res.plan = build(p, m)
    shift = timedelta(hours=max(inp.stops_h, 0.0))

    def end_of(pl: mdl.Plan) -> datetime:
        return inp.start + timedelta(hours=pl.total_time_h) + shift

    if inp.method != "q":
        sig, res.sigma_info = mdl.plan_sigma(p, res.plan, c, mdl.other_model_final(p, res.plan, c, inp.t_list))
        res.sigma = sig
        fast_p, slow_p = mdl.scaled(p, 1.0 + mdl.Z90 * sig), mdl.scaled(p, max(1.0 - mdl.Z90 * sig, 0.05))
        res.mass_hi = mass_for_hours(lambda mm: build(fast_p, mm), hours)[0]
        res.mass_lo = mass_for_hours(lambda mm: build(slow_p, mm), hours)[0]
    else:
        fast_p = slow_p = None

    def option(key: str, title: str, mass: float) -> WeekendOption:
        pl = build(p, mass)
        o = WeekendOption(key, title, mass, end_of(pl), plan=pl)
        if fast_p is not None:
            o.end_fast, o.end_slow = end_of(build(fast_p, mass)), end_of(build(slow_p, mass))
        return o

    calc = option("calc", "Расчётная", m)
    calc.what = "закончит к выходу при обычной скорости сушки"
    if res.mass_lo is not None and res.mass_hi is not None and res.mass_hi - res.mass_lo > 1.0:
        safe = option("safe", "Точно успеть к выходу", res.mass_lo)
        idle = (inp.end - safe.end).total_seconds() / 3600.0
        safe.what = (f"при обычной сушке сушилка освободится на {idle:.1f} ч раньше выхода; даже при медленной — "
                     f"к выходу")
        full = option("full", "Без простоя", res.mass_hi)
        late = (full.end - inp.end).total_seconds() / 3600.0
        full.what = (f"сушилка не встанет даже при быстрой сушке; при обычной закончит на {late:.1f} ч позже выхода — "
                     f"свежее зерно подождёт в силосе")
        res.options = [safe, calc, full]
    else:
        res.options = [calc]
    # ёмкости под партию
    if inp.capacity > 0 and m > inp.capacity + 0.5:
        res.capacity_short = m - inp.capacity
        bins = option("bins", "Сколько вместят ёмкости", inp.capacity)
        idle = (inp.end - bins.end).total_seconds() / 3600.0
        bins.what = f"ёмкостей {inp.capacity:.0f} т: сушилка освободится на {idle:.1f} ч раньше выхода и встанет"
        res.options.append(bins)
    # сырое зерно до 1-го прохода
    if res.plan.passes:
        res.wet_wait_h = res.plan.passes[0].time_h + max(inp.stops_h, 0.0)   # остановки — с запасом внутри 1-го
        res.pass1_end = inp.start + timedelta(hours=res.wet_wait_h)
        # таблица — для кукурузы; другая культура — по влажности кукурузы с той же равновесной влажностью
        # воздуха (порчу определяет она, а не процент влажности): рапс 10 % хранится как кукуруза 15 %
        days, flag = allowable_storage_days(mdl.corn_equiv(p, inp.w0), c.t_amb)
        if not mdl.is_corn_scale(p):
            flag = (flag + "; " if flag else "") + "оценка по таблице кукурузы для равной равновесной влажности воздуха"
        res.storage_days, res.storage_flag = days, flag
        if days is not None:
            frac = res.wet_wait_h / 24.0 / days
            res.storage_level = 0 if frac <= 0.5 else 1 if frac <= 1.0 else 2
            res.harvest_not_before = res.pass1_end - timedelta(days=days)
    return res


def next_weekday_time(after: datetime, weekday: int, hour: int, minute: int = 0) -> datetime:
    """Ближайшие после after день недели weekday (0 — понедельник) и время hour:minute."""
    d = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    if d <= after:
        d += timedelta(days=7)
    return d
