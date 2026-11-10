"""Проверка того, что вводят руками: проба в журнал и пара проб влагомера.

Зачем. Ошибка модели на проходе — ±0,3–0,6 п.п., а опечатка во влажности — это 5–10 п.п. сразу. С версии 1.8.1 вход
в расчётах — среднее проб за 2 ч, поэтому опечатка «31 вместо 21» не уходит со следующей пробой, а сдвигает совет по
D ещё два часа. Дешевле всего поймать её в момент ввода: программа сравнивает новую пробу с последними пробами того же
потока и переспрашивает, если она далеко. Решает человек — «записать как есть» остаётся всегда.

* parse_moisture() — поле влажности быстрого ввода: одно число или две пробы подряд («31,2 30,6», «31,2/30,6») —
  в журнал идёт среднее. Две пробы — способ уменьшить шум у источника: проба сырого зерна (выше 27 %) шумит на
  ±0,7–0,9 п.п., среднее двух независимых проб — в √2 раз меньше.
* sample_doubt() — сомнения в новой пробе входа / выхода.
* pair_doubt() — сомнения в новой паре «выходной влагомер — после охлаждения» (поправка влагомера стоит в основе
  всех расчётов выхода; одна пара с опечаткой сдвигает её надолго).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from .calibration import parse_ts, to_float

# Сколько прежних проб смотреть и как далеко назад. Проба переспрашивается, если ушла от медианы последних проб
# больше чем на max(JUMP_MIN, JUMP_K · разброс). Порог проверен повтором журналов: ноябрь 2026 (576 проб, вход
# 17–33 %) — переспрошены 2 настоящие пробы, октябрь (309 проб) — тоже 2; все четыре действительно выбиваются из
# ряда (скачок на 3–4 п.п. за полчаса). Подставленные опечатки: на ±10 п.п. пойманы в 98–99 % случаев, на ±5 —
# 89–95 %, на ±3 — 68–75 % (мельче шума сырого зерна уже не отличить). У первых двух-трёх проб потока истории нет
# (≈ 15 % проб) — для них проверяется только диапазон влагомера.
HIST_N = 5
HIST_H = 3.0
JUMP_MIN = 2.5
JUMP_K = 5.0
W_RANGE = (4.0, 45.0)               # вне этого влагомер зерно не меряет — почти наверняка опечатка
WET_W = 27.0                        # выше — «сырое зерно»: шум пробы ±0,7–0,9 п.п., стоит брать две пробы
DOUBLE_GAP = 2.0                    # две пробы подряд разошлись сильнее — переспросить
_SPLIT = re.compile(r"\s*[/;+]\s*|\s+")
_REGIME_NOTE = re.compile(r"^\s*режим\s*:", re.I)

FIELD_NAMES = {"w_in": "входа", "w_out": "выхода"}


def parse_moisture(txt: str) -> Tuple[Optional[float], List[float]]:
    """Текст поля влажности → (значение для журнала, пробы). Одно число — оно и есть; два-три числа через пробел,
    «/», «;» или «+» — среднее, округлённое до 0,05. Не разобрано — (None, [])."""
    s = (txt or "").strip()
    if not s:
        return None, []
    parts = [x for x in _SPLIT.split(s) if x]
    vals = [to_float(x) for x in parts]
    if not vals or any(v is None for v in vals) or len(vals) > 3:
        return None, []
    if len(vals) == 1:
        return vals[0], vals
    return round(sum(vals) / len(vals) * 20.0) / 20.0, vals


def _row_dt(row: Dict) -> Optional[datetime]:
    d, t = parse_ts(row.get("ts"))
    return datetime.combine(d, t) if d is not None and t is not None else None


def _is_event(row: Dict) -> bool:
    """Строка с событием (остановка, запуск, рециркуляция, смена силоса…): до неё был другой поток зерна."""
    note = (row.get("note") or "").strip()
    return bool(note) and not _REGIME_NOTE.match(note)


def recent_values(rows: Sequence[Dict], field: str, before: Optional[datetime] = None, n: int = HIST_N,
                  hours: float = HIST_H) -> List[float]:
    """Последние пробы поля field перед моментом before — того же потока: назад не дальше hours и не через событие
    (примечание, кроме «Режим: …») и не через смену «Партии» / «Прохода»."""
    out: List[float] = []
    key = None
    for r in reversed(list(rows)):
        dt = _row_dt(r)
        if before is not None and dt is not None:
            if dt > before:
                continue
            if before - dt > timedelta(hours=hours):
                break
        k = ((r.get("batch") or "").strip(), r.get("pass_no"))
        if key is None:
            key = k
        elif k != key and (k[0] or k[1] is not None):
            break
        if r.get(field) is not None:
            out.append(float(r[field]))
            if len(out) >= n:
                break
        if _is_event(r):
            break
    return out[::-1]


def _spread(vals: Sequence[float]) -> float:
    med = sorted(vals)[len(vals) // 2]
    return 1.4826 * sorted(abs(v - med) for v in vals)[len(vals) // 2]


def value_doubt(v: float, hist: Sequence[float]) -> Optional[str]:
    """Чем подозрительно значение на фоне прежних проб (или None)."""
    if not W_RANGE[0] <= v <= W_RANGE[1]:
        return f"вне {W_RANGE[0]:g}–{W_RANGE[1]:g} %"
    if len(hist) < 3:
        return None
    med = sorted(hist)[len(hist) // 2]
    lim = max(JUMP_MIN, JUMP_K * _spread(hist))
    if abs(v - med) > lim:
        lo, hi = min(hist), max(hist)
        was = f"{lo:g}".replace(".", ",") if abs(hi - lo) < 0.05 else f"{lo:g}–{hi:g}".replace(".", ",")
        return f"последние {len(hist)} проб — {was} %"
    return None


def sample_doubt(rows: Sequence[Dict], row: Dict, parts: Optional[Dict[str, List[float]]] = None) -> List[str]:
    """Сомнения в новой строке row (ещё не в журнале rows). Пустой список — всё в порядке.

    Проба с событием в примечании («сушим с 1 с.», «Запуск») с прежними не сравнивается — поток сменился; диапазон
    влагомера проверяется всегда. parts — пробы, из которых получено среднее (parse_moisture), по полям."""
    out: List[str] = []
    when = _row_dt(row)
    event = _is_event(row)
    for f in ("w_in", "w_out"):
        v = row.get(f)
        if v is None:
            continue
        two = (parts or {}).get(f) or []
        if len(two) > 1 and max(two) - min(two) > DOUBLE_GAP:
            out.append(f"Две пробы {FIELD_NAMES[f]} разошлись на {max(two) - min(two):.1f} п.п. "
                       f"({', '.join(f'{x:g}' for x in two)}) — одна из них, похоже, с опечаткой.".replace(".", ",", 1))
            continue
        why = value_doubt(float(v), [] if event else recent_values(rows, f, when))
        if why:
            out.append(f"Проба {FIELD_NAMES[f]} {float(v):g} % — {why}.".replace(".", ",", 1))
    return out


def wet_hint(w_corn: Optional[float]) -> bool:
    """Зерно сырое (в пересчёте на кукурузу) — стоит брать две пробы."""
    return w_corn is not None and w_corn >= WET_W


def pair_doubt(pairs: Sequence[Dict], new: Dict) -> Optional[str]:
    """Сомнение в новой паре {reading, ref}: показание выходного влагомера и та же проба после охлаждения.

    Разность «показание − после охлаждения» сравнивается с остальными парами: дальше max(1,5 п.п.; 4 разброса) от
    их медианы — переспросить. Пока пар меньше четырёх — только грубая проверка: разность больше 4 п.п. или число
    вне диапазона влагомера."""
    a, b = new.get("reading"), new.get("ref")
    if a is None or b is None:
        return None
    for v, name in ((a, "показание влагомера"), (b, "влажность после охлаждения")):
        if not W_RANGE[0] <= v <= W_RANGE[1]:
            return f"{name} {v:g} % — вне {W_RANGE[0]:g}–{W_RANGE[1]:g} %".replace(".", ",", 1)
    d = a - b
    others = [p["reading"] - p["ref"] for p in pairs
              if p is not new and p.get("reading") is not None and p.get("ref") is not None]
    if len(others) < 4:
        return f"разность {d:+.1f} п.п. — больше 4 п.п.".replace(".", ",", 1) if abs(d) > 4.0 else None
    med = sorted(others)[len(others) // 2]
    lim = max(1.5, 4.0 * _spread(others))
    if abs(d - med) > lim:
        return (f"разность {d:+.1f} п.п., а у остальных {len(others)} пар — около {med:+.1f} "
                f"(от {min(others):+.1f} до {max(others):+.1f})").replace(".", ",")
    return None
