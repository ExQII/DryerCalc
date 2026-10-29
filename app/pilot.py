"""Пилотный запуск: режимы «Сушильщик / Инженер», PIN режима инженера, статистика использования и замечания.

Что считается в отчёте пилота
-----------------------------
* Покрытие проб: в каждом проходе журнала (горелки включены, не рециркуляция) ожидается проба каждые
  30 мин; покрытие = фактических проб / ожидаемых. Интервалы между пробами: медиана и доля интервалов
  длиннее 40 мин («опоздания»).
* Быстрый ввод: сколько проб внесено строкой быстрого ввода (события «sample»), а сколько — иначе
  (импорт, правка таблицы).
* Напоминания: сколько каких показано; реакция на «пора внести пробу» — сколько минут прошло до
  следующей пробы.
* Запуски программы по дням, переключения режима, замечания пользователей.
Задача отчёта — понять, ведут ли журнал в программе и где людям неудобно, а не контролировать людей.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence

OPERATOR = "operator"
ENGINEER = "engineer"
OPERATOR_TABS = ("Сейчас (по факту)", "Рециркуляция", "Журнал", "Справка")


# ----------------------------------------------------------------------------
# PIN режима инженера (защита от случайных изменений, а не от злоумышленника)
# ----------------------------------------------------------------------------

def pin_hash(pin: str, salt: Optional[str] = None) -> str:
    salt = salt or os.urandom(8).hex()
    h = hashlib.sha256((salt + ":" + pin.strip()).encode("utf-8")).hexdigest()
    return f"{salt}${h}"


def pin_ok(pin: str, stored: Optional[str]) -> bool:
    if not stored:
        return True
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return pin_hash(pin, salt) == stored


# ----------------------------------------------------------------------------
# Статистика
# ----------------------------------------------------------------------------

def _median(xs: Sequence[float]) -> Optional[float]:
    s = sorted(xs)
    if not s:
        return None
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def sample_coverage(rows: List[Dict], start: Optional[datetime] = None, end: Optional[datetime] = None,
                    step_min: float = 30.0, late_min: float = 40.0) -> Dict:
    """Пробы в проходах журнала между start и end: ожидаемые, фактические, интервалы."""
    from .calibration import prepare
    prep, reliable = prepare(rows)
    if not reliable:
        return {"expected": 0, "actual": 0, "intervals": [], "coverage": None, "late_share": None,
                "median_interval": None, "passes": 0}
    segs: Dict[int, List[datetime]] = {}
    for r in prep:
        dt = r.get("dt")
        if dt is None or r.get("burner_off") or r.get("recirc"):
            continue
        if start and dt < start or end and dt > end:
            continue
        if r.get("w_in") is None and r.get("w_out") is None:
            continue
        segs.setdefault(r["seg"], []).append(dt)
    intervals: List[float] = []
    expected = actual = 0
    for times in segs.values():
        times.sort()
        actual += len(times)
        span = (times[-1] - times[0]).total_seconds() / 60.0
        expected += int(span // step_min) + 1
        intervals += [(b - a).total_seconds() / 60.0 for a, b in zip(times, times[1:])]
    late = [x for x in intervals if x > late_min]
    return {"expected": expected, "actual": actual, "intervals": intervals,
            "coverage": actual / expected if expected else None,
            "late_share": len(late) / len(intervals) if intervals else None,
            "median_interval": _median(intervals), "passes": len(segs)}


def pilot_stats(events: List[Dict], rows: List[Dict], since: Optional[datetime] = None) -> Dict:
    ev = [e for e in events if since is None or e["ts"] >= since]
    start = min((e["ts"] for e in ev), default=None)
    end = max((e["ts"] for e in ev), default=None)
    by_kind: Dict[str, int] = {}
    for e in ev:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
    reminders: Dict[str, int] = {}
    for e in ev:
        if e["kind"] == "reminder" and isinstance(e["detail"], dict):
            k = e["detail"].get("key", "?")
            reminders[k] = reminders.get(k, 0) + 1
    # реакция на «пора внести пробу»: минуты до следующего события «sample»
    samples = sorted(e["ts"] for e in ev if e["kind"] == "sample")
    reactions = []
    for e in ev:
        if e["kind"] == "reminder" and isinstance(e["detail"], dict) and e["detail"].get("key") == "sample":
            nxt = next((t for t in samples if t >= e["ts"]), None)
            if nxt is not None and nxt - e["ts"] <= timedelta(hours=3):
                reactions.append((nxt - e["ts"]).total_seconds() / 60.0)
    days = sorted({e["ts"].date() for e in ev if e["kind"] == "start"})
    modes: Dict[str, int] = {}
    for e in ev:
        if e["kind"] == "start" and isinstance(e["detail"], dict):
            m = e["detail"].get("mode", "?")
            modes[m] = modes.get(m, 0) + 1
    cov = sample_coverage(rows, start, end + timedelta(minutes=1) if end else None) if start else sample_coverage(rows)
    feedback = [e for e in ev if e["kind"] == "feedback"]
    return {"start": start, "end": end, "by_kind": by_kind, "reminders": reminders,
            "reaction_median": _median(reactions), "reactions": len(reactions), "days": days, "modes": modes,
            "coverage": cov, "feedback": feedback, "quick_samples": by_kind.get("sample", 0)}


def stats_text(st: Dict) -> str:
    if st["start"] is None:
        return "Событий пока нет: статистика появится после работы с программой."
    cov = st["coverage"]
    lines = [f"Период: {st['start']:%d.%m.%Y %H:%M} — {st['end']:%d.%m.%Y %H:%M}; дней с запусками: {len(st['days'])}.",
             "Запуски по режимам: " + (", ".join(f"{'сушильщик' if k == OPERATOR else 'инженер' if k == ENGINEER else k} "
                                                 f"{v}" for k, v in st["modes"].items()) or "—") + "."]
    if cov["expected"]:
        lines.append(f"Пробы в проходах: {cov['actual']} из ожидаемых {cov['expected']} "
                     f"(покрытие {100 * cov['coverage']:.0f} %), проходов {cov['passes']}; медианный интервал "
                     f"{cov['median_interval']:.0f} мин, интервалов длиннее 40 мин — {100 * (cov['late_share'] or 0):.0f} %.")
    else:
        lines.append("Проходов с пробами за период нет.")
    lines.append(f"Внесено строкой быстрого ввода: {st['quick_samples']}.")
    if st["reminders"]:
        names = {"sample": "«пора внести пробу»", "pass_end": "«проход скоро закончится»",
                 "deviation": "«выход ушёл от цели»"}
        lines.append("Напоминания: " + ", ".join(f"{names.get(k, k)} — {v}" for k, v in st["reminders"].items()) + ".")
        if st["reaction_median"] is not None:
            lines.append(f"После «пора внести пробу» проба вносилась в среднем через {st['reaction_median']:.0f} мин "
                         f"(по {st['reactions']} случаям).")
    lines.append(f"Замечаний пользователей: {len(st['feedback'])}.")
    return "\n".join(lines)


def export_xlsx(path: str, st: Dict, events: List[Dict]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Сводка"
    for line in stats_text(st).split("\n"):
        ws.append([line])
    ws.column_dimensions["A"].width = 140
    ws2 = wb.create_sheet("Замечания")
    ws2.append(["Когда", "Кто", "Режим", "Вкладка", "Замечание"])
    for c in ws2[1]:
        c.font = Font(bold=True)
    for e in st["feedback"]:
        d = e["detail"] if isinstance(e["detail"], dict) else {"text": str(e["detail"])}
        ws2.append([e["ts"], d.get("who", ""), d.get("mode", ""), d.get("tab", ""), d.get("text", "")])
    for col, w in zip("ABCDE", (17, 18, 11, 22, 100)):
        ws2.column_dimensions[col].width = w
    for row in ws2.iter_rows(min_row=2):
        row[0].number_format = "DD.MM.YYYY HH:MM"
        row[4].alignment = Alignment(wrap_text=True, vertical="top")
    ws3 = wb.create_sheet("События")
    ws3.append(["Когда", "Событие", "Подробности"])
    for c in ws3[1]:
        c.font = Font(bold=True)
    for e in events:
        ws3.append([e["ts"], e["kind"], "" if e["detail"] is None else str(e["detail"])])
    for col, w in zip("ABC", (17, 14, 90)):
        ws3.column_dimensions[col].width = w
    for row in ws3.iter_rows(min_row=2):
        row[0].number_format = "DD.MM.YYYY HH:MM:SS"
    ws4 = wb.create_sheet("Интервалы проб")
    ws4.append(["Интервал между пробами, мин"])
    for x in st["coverage"]["intervals"]:
        ws4.append([round(x, 1)])
    wb.save(path)
