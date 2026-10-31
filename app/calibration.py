"""Импорт журнала сушильщиков и калибровка модели расхода влаги.

Формат бумажного журнала (колонки): время пробы | влага вход | влага выход |
температура зерна (нижний датчик – верхний) | температура топочных блоков (пишется только при
изменении, дальше прочерк) | разгрузка D | примечание.

Что делает код
--------------
* Разбирает время вида «08:30» (без даты; переход через полночь определяется по строкам) и
  «21.10.26 08:30», пары вида «26°-24°», прочерки в колонке блоков (значение переносится вперёд).
* Делит журнал на участки (проходы) по примечаниям («Остановка», «Запуск», «Закончился …»,
  «открыли с …»), смене партии/номера прохода и пропускам больше 1,5 ч.
* Выход сушилки отстаёт от входа на время пребывания зерна τ = lag_k · m_dryer / Q(D) (при D = 35
  около 3,3 ч): выход в момент t относится к зерну, которое вошло в t − τ. Поэтому наблюдаемый
  расход влаги считается по паре (вход в t − τ, выход в t):

      R_obs = Q(D) · (w_вх(t − τ) − w_вых(t) + out_bias) / (100 − w_вх(t − τ))

  Первые τ часов каждого участка (на выходе ещё старое зерно) в калибровку не попадают.
* Подбирает ln R = ln R_ref + b_w·f_w + b_dt·f_dt + b_rh·f_rh регуляризованной регрессией.
  Показатели степеней меняются только при ≥ 12 строках и достаточном разбросе.
* Выдаёт отчёт: ошибка прогноза w_вых по строкам и сводка по проходам.
"""
from __future__ import annotations

import csv
import math
import re
from dataclasses import replace
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .model import DryerParams, pass_rate_arr, q_at, solve_out_arr, true_from_read

FIELDS = ["ts", "w_in", "w_out", "t_a1", "t_a2", "t_up", "t_low", "d", "note", "batch", "pass_no", "t_amb", "rh"]
TEXT_FIELDS = {"ts", "batch", "note"}
HEADERS_RU = {
    "ts": "Дата-время", "w_in": "Влага вход, %", "w_out": "Влага выход, %",
    "t_a1": "T зерна низ, °C", "t_a2": "T зерна верх, °C",
    "t_up": "T блока 1, °C", "t_low": "T блока 2, °C", "d": "Разгрузка D, с",
    "note": "Примечание", "batch": "Партия", "pass_no": "Проход",
    "t_amb": "T наружная, °C", "rh": "RH наружная, %",
}

BREAK_WORDS = ("остановк", "запуск", "открыли", "открыл", "набираем", "снимаем", "закончил",
               "загрузка", "зачистка", "переключ")
RECIRC_WORDS = ("рецирк",)
BURNER_OFF_WORDS = ("охлажд",)          # плюс «откл…» вместе с «т.б.» / «блок» / «горел» / «топ»
BURNER_ON_WORDS = ("запуск", "вкл. т", "вкл т", "включ")


def _burner_off_note(note: str) -> bool:
    n = note.lower()
    if any(w in n for w in BURNER_OFF_WORDS):
        return True
    return "откл" in n and any(w in n for w in ("т.б", "т. б", "тб", "блок", "горел", "топ"))


def _burner_on_note(note: str) -> bool:
    n = note.lower()
    return any(w in n for w in BURNER_ON_WORDS) and "откл" not in n


# ----------------------------------------------------------------------------
# Разбор значений
# ----------------------------------------------------------------------------

_NUM = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
_N = r"([-+]?\d+(?:[.,]\d+)?)"
_PAIR = re.compile(rf"^\s*{_N}\s*°?\s*[-–—/]\s*{_N}\s*°?\s*(?:[CcСс])?\s*$")


def to_float(x) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return None if (isinstance(x, float) and math.isnan(x)) else float(x)
    s = str(x).strip()
    if s in ("", "-", "–", "—", "−"):
        return None
    m = _NUM.search(s)
    return float(m.group().replace(",", ".")) if m else None


def parse_pair(x) -> Optional[Tuple[float, float]]:
    """'26°-24°' -> (26.0, 24.0) в порядке записи (первое, второе)."""
    if x is None:
        return None
    m = _PAIR.match(str(x))
    if not m:
        return None
    a, b = (float(g.replace(",", ".")) for g in m.groups())
    return a, b


_TS_FULL = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?[\s,T]+(\d{1,2})[:.\-](\d{2})")
_TS_TIME = re.compile(r"^\s*(\d{1,2})[:\-](\d{2})\s*$|^\s*(\d{1,2})\.(\d{2})\s*$")


def parse_ts(x) -> Tuple[Optional[date], Optional[dtime]]:
    """Возвращает (дата | None, время | None)."""
    if x is None or x == "":
        return None, None
    if isinstance(x, datetime):
        return x.date(), x.time().replace(second=0, microsecond=0)
    if isinstance(x, dtime):
        return None, x.replace(second=0, microsecond=0)
    s = str(x).strip()
    m = _TS_FULL.match(s)
    if m:
        d, mo, y, hh, mm = m.groups()
        yy = int(y) if y else 2000
        if y and len(y) == 2:
            yy += 2000
        try:
            return date(yy, int(mo), int(d)), dtime(int(hh), int(mm))
        except ValueError:
            return None, None
    m = _TS_TIME.match(s)
    if m:
        hh, mm = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        if int(hh) < 24 and int(mm) < 60:
            return None, dtime(int(hh), int(mm))
    return None, None


def _norm(h) -> str:
    return re.sub(r"[^0-9a-zа-яё]", "", str(h).lower())


_CANON = {"win": "w_in", "wout": "w_out", "d": "d", "ta1": "t_a1", "ta2": "t_a2",
          "tup": "t_up", "tlow": "t_low", "tamb": "t_amb", "rh": "rh"}


def map_header(h) -> Optional[str]:
    """Название колонки журнала -> поле. 'grain_pair' и 'block_pair' — одна колонка с парой '26-24'."""
    n = _norm(h)
    if not n:
        return None
    if n in _CANON:
        return _CANON[n]
    if n in ("датавремя", "datetime", "ts", "timestamp"):
        return "ts"
    if n in ("дата", "date"):
        return "date"
    if n in ("время", "времяпробы", "time", "времяотбора"):
        return "ts"
    if n.startswith("парти") or n == "batch":
        return "batch"
    if n in ("проход", "круг", "pass", "passno", "номерпрохода", "номеркруга"):
        return "pass_no"
    if n.startswith("примеч") or n in ("note", "notes", "комментарий", "заметки"):
        return "note"
    if "rh" in n or ("влажн" in n and ("возд" in n or "нар" in n)):
        return "rh"
    if "влаг" in n or "влажн" in n or n in ("вход", "выход", "входнаясушилку", "навход", "навыход"):
        if "вых" in n:
            return "w_out"
        if "вх" in n or "вход" in n:
            return "w_in"
    if n in ("вход", "входвсушилку"):
        return "w_in"
    if n in ("выход", "выходизсушилки"):
        return "w_out"
    if n.startswith("выгруз") or n.startswith("загруз") or n.startswith("разгруз") or n in ("dвыгрузки",):
        return "d"
    if "блок" in n or "топоч" in n:
        if "верх" in n:
            return "t_up"
        if "низ" in n:
            return "t_low"
        if "2" in n and "1" not in n:
            return "t_low"
        if "1" in n and "2" not in n:
            return "t_up"
        return "block_pair"
    if "агент" in n or "зерн" in n or "кукуруз" in n:
        if "верх" in n:
            return "t_a2"
        if "низ" in n:
            return "t_a1"
        if n.endswith("2"):
            return "t_a2"
        if n.endswith("1"):
            return "t_a1"
        return "grain_pair"
    if "нар" in n or "улиц" in n or "amb" in n:
        return "t_amb"
    return None


# ----------------------------------------------------------------------------
# Чтение и шаблон
# ----------------------------------------------------------------------------

def _read_csv(path: str) -> List[list]:
    raw = Path(path).read_bytes()
    text = None
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("Не удалось определить кодировку файла (ожидаю UTF-8 или Windows-1251)")
    lines = text.splitlines()
    if not lines:
        return []
    delim = max(";,\t", key=lambda d: lines[0].count(d))
    return [r for r in csv.reader(lines, delimiter=delim)]


def _read_xlsx(path: str) -> List[list]:
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()


def _cell_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%d.%m.%Y %H:%M")
    if isinstance(v, dtime):
        return v.strftime("%H:%M")
    if isinstance(v, date):
        return v.strftime("%d.%m.%Y")
    return str(v)


def read_journal(path: str) -> Tuple[List[Dict], Dict]:
    """Читает CSV/XLSX. Возвращает (строки, info)."""
    ext = Path(path).suffix.lower()
    table = _read_xlsx(path) if ext in (".xlsx", ".xlsm") else _read_csv(path)
    table = [r for r in table if any(c not in (None, "") for c in r)]
    if not table:
        raise ValueError("Файл пуст")
    header = table[0]
    mapping: Dict[int, str] = {}
    mapped_names: Dict[str, str] = {}
    unmapped: List[str] = []
    for i, h in enumerate(header):
        f = map_header(h)
        if f and f not in mapping.values():
            mapping[i] = f
            mapped_names[f] = str(h)
        elif h not in (None, ""):
            unmapped.append(str(h))
    missing = {"w_in", "w_out", "d"} - set(mapping.values())
    if missing:
        found = ", ".join(str(h) for h in header if h not in (None, ""))
        raise ValueError(
            "Не нашёл обязательные колонки: " + ", ".join(sorted(missing)) +
            f".\nЗаголовки в файле: {found}.\nПереименуйте их по шаблону (кнопка «Сохранить шаблон»).")
    out: List[Dict] = []
    skipped = 0
    for r in table[1:]:
        row: Dict = {f: None for f in FIELDS}
        for f in TEXT_FIELDS:
            row[f] = ""
        row_date = ""
        for i, f in mapping.items():
            v = r[i] if i < len(r) else None
            if f in ("block_pair", "grain_pair"):
                pr = parse_pair(_cell_text(v)) if v is not None else None
                a, b = ("t_up", "t_low") if f == "block_pair" else ("t_a1", "t_a2")
                if pr:
                    row[a], row[b] = pr
                else:
                    single = to_float(v)
                    if single is not None:
                        row[a] = row[b] = single
            elif f == "date":
                row_date = _cell_text(v)
            elif f in TEXT_FIELDS:
                row[f] = _cell_text(v)
            else:
                row[f] = to_float(v)
        if row_date and row["ts"] and not re.search(r"\d{1,2}\.\d{1,2}", row["ts"]):
            row["ts"] = f"{row_date} {row['ts']}"
        elif row_date and not row["ts"]:
            row["ts"] = row_date
        has_data = any(row[k] is not None for k in ("w_in", "w_out", "d"))
        if not has_data and not row["note"].strip():
            skipped += 1
            continue
        out.append(row)
    return out, {"mapped": mapped_names, "unmapped": unmapped, "skipped": skipped}


def write_template(path: str) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow([HEADERS_RU[k] for k in FIELDS])
        w.writerow(["21.10.26 08:30", "17,6", "17,6", "25", "23", "70", "70", "35", "Запуск т.б.", "A", "3", "", ""])
        w.writerow(["21.10.26 09:00", "18,3", "17,3", "26", "25", "", "", "35", "", "A", "3", "", ""])


# ----------------------------------------------------------------------------
# Подготовка: время, участки, перенос значений
# ----------------------------------------------------------------------------

def assign_times(rows: List[Dict]) -> Tuple[List[Optional[datetime]], bool]:
    """Абсолютное время каждой строки. Если в журнале только время суток, дата растёт после полуночи.

    Возвращает (время, надёжно ли оно: распознано не меньше 80 % строк)."""
    out: List[Optional[datetime]] = []
    cur: Optional[date] = None
    prev: Optional[datetime] = None
    ok = 0
    for r in rows:
        d, t = parse_ts(r.get("ts"))
        if t is None:
            out.append(None)
            continue
        ok += 1
        if d is not None:
            cur = d
        elif cur is None:
            cur = date(2000, 1, 1)
        dt = datetime.combine(cur, t)
        if d is None and prev is not None and dt < prev - timedelta(hours=1):
            cur = cur + timedelta(days=1)
            dt = datetime.combine(cur, t)
        elif d is not None and prev is not None and dt < prev - timedelta(hours=1):
            pass
        out.append(dt)
        prev = dt
    reliable = bool(rows) and ok >= 0.8 * len(rows)
    # строкам без времени (например, примечаниям) ставим время предыдущей строки
    last: Optional[datetime] = None
    for i, dt in enumerate(out):
        if dt is None:
            out[i] = last
        else:
            last = dt
    return out, reliable


def prepare(rows: List[Dict]) -> Tuple[List[Dict], bool]:
    """Добавляет к строкам: dt, t (часы от начала), seg, recirc, burner_off; переносит блоки и D вперёд.

    recirc — после примечания «рециркуляция» до конца участка (зерно ходит по кругу, из силоса не берётся);
    burner_off — после «охлаждение» / «откл. т.б.» до «запуск»/«вкл.» (горелки выключены, сушки нет)."""
    times, reliable = assign_times(rows)
    prep: List[Dict] = []
    seg = 0
    t0 = next((t for t in times if t is not None), None)
    last_up = last_low = last_d = None
    recirc = burner_off = False
    prev_key = None
    prev_dt: Optional[datetime] = None
    for i, r in enumerate(rows):
        p = dict(r)
        dt = times[i]
        if reliable and dt is not None and t0 is not None:
            p["t"] = (dt - t0).total_seconds() / 3600.0
        else:
            p["t"] = 0.5 * i
        note = (r.get("note") or "").lower()
        key = (r.get("batch") or "", r.get("pass_no"))
        brk = False
        if i > 0:
            if any(w in note for w in BREAK_WORDS):
                brk = True
            if key != prev_key:
                brk = True
            if reliable and dt is not None and prev_dt is not None and (dt - prev_dt).total_seconds() > 1.5 * 3600:
                brk = True
        if brk:
            seg += 1
            recirc = burner_off = False
        if any(w in note for w in RECIRC_WORDS):
            recirc = True
        if _burner_off_note(note):
            burner_off = True
        elif _burner_on_note(note):
            burner_off = False
        p["recirc"], p["burner_off"] = recirc, burner_off
        prev_key = key
        if dt is not None:
            prev_dt = dt
        if r.get("t_up") is not None or r.get("t_low") is not None:
            last_up = r.get("t_up") if r.get("t_up") is not None else last_up
            last_low = r.get("t_low") if r.get("t_low") is not None else last_low
        p["t_up_f"], p["t_low_f"] = last_up, last_low
        if r.get("d") is not None:
            last_d = r.get("d")
        p["d_f"] = last_d
        p["seg"] = seg
        p["dt"] = dt
        prep.append(p)
    return prep, reliable


STOP_STEMS = ("остановк",)
# слова, после которых начинается именно НОВЫЙ проход (смена силоса, окончание), а не продолжение после остановки
PASS_END_STEMS = ("закончил", "открыл", "переключ", "загрузка", "зачистка", "набираем", "снимаем") + RECIRC_WORDS


def pass_of_segment(prep: List[Dict]) -> Dict[int, int]:
    """Участок журнала -> номер первого участка его прохода.

    Участки (prepare) режутся и остановкой/пуском, и перерывом > 1,5 ч — для калибровки это правильно (после
    остановки связь «вход − τ / выход» начинается заново). Но для учёта «выгружено», напоминаний и сводок по
    партиям проход один: участок B продолжает проход участка A, если у первой строки B те же непустые «Партия» и
    «Проход», что у A, и её примечание не начинает новый проход («Закончился…», «открыли с…», «переключ…»,
    «рециркуляция» и т. п.). Без заполненных «Партия»/«Проход» участки не объединяются."""
    first: Dict[int, Dict] = {}
    last: Dict[int, Dict] = {}
    order: List[int] = []
    for r in prep:
        s = r["seg"]
        if s not in first:
            first[s] = r
            order.append(s)
        last[s] = r
    out: Dict[int, int] = {}
    for i, s in enumerate(order):
        out[s] = s
        if i == 0:
            continue
        a, b = last[order[i - 1]], first[s]
        key_b = ((b.get("batch") or "").strip(), b.get("pass_no"))
        key_a = ((a.get("batch") or "").strip(), a.get("pass_no"))
        note = (b.get("note") or "").lower()
        if (key_b[0] and key_b[1] is not None and key_a == key_b and not a.get("recirc")
                and not any(w in note for w in PASS_END_STEMS)):
            out[s] = out[order[i - 1]]
    return out


def chronological_issues(rows: List[Dict]) -> List[int]:
    """Номера строк с явной датой, которые стоят в журнале раньше более поздних по времени строк
    (строка «из прошлого», дописанная в конец). Строки без даты не проверяются: их дата выводится по порядку."""
    times, _ = assign_times(rows)
    bad: List[int] = []
    latest: Optional[datetime] = None
    for i, (r, dt) in enumerate(zip(rows, times)):
        if dt is None:
            continue
        d, _t = parse_ts(r.get("ts"))
        if d is not None and latest is not None and dt < latest - timedelta(minutes=1):
            bad.append(i)
        latest = dt if latest is None or dt > latest else latest
    return bad


def chronological_order(rows: List[Dict]) -> List[int]:
    """Порядок строк по времени (устойчивый: равные и строки без времени остаются за своими соседями)."""
    times, _ = assign_times(rows)
    return sorted(range(len(rows)), key=lambda i: (times[i] or datetime.min, i))


def insert_position(rows: List[Dict], ts) -> int:
    """Куда вставить строку со временем ts (с датой), чтобы журнал остался по порядку времени."""
    d, t = parse_ts(ts)
    if d is None or t is None or not rows:
        return len(rows)
    new_dt = datetime.combine(d, t)
    times, _ = assign_times(rows)
    pos = len(rows)
    for i in range(len(rows) - 1, -1, -1):
        if times[i] is not None and times[i] <= new_dt:
            return i + 1
        if times[i] is not None:
            pos = i
    return pos


# ----------------------------------------------------------------------------
# Слова-ловушки в примечании: программа ищет основы слов («охлажд», «рецирк», «открыл»…) где угодно в тексте,
# поэтому обычный комментарий может нечаянно стать командой. Явные события — по образцам ниже; на всё остальное
# со словами-командами программа переспрашивает.
# ----------------------------------------------------------------------------

_EVENT_PATTERNS = [re.compile(x, re.I) for x in (
    r"^\s*остановк\w*\b(?!.*(рецирк|охлажд|включ|вкл\.?\s*т))",
    r"^\s*запуск\w*\b(?!.*(рецирк|охлажд|откл))",
    r"^\s*закончил\w*\b(?!.*(рецирк|охлажд|включ|вкл\.?\s*т))",
    r"^\s*открыли?\s+(с\b|с\.|силос|из\s+силос)",
    r"^\s*переключ\w*\s+(на\s+)?(с\b|с\.|силос|подач)",
    r"^\s*(загрузка|зачистка)(\s+(сушилки|шахты|силоса?)\b.*)?\s*[.,;:!]?\s*$",
    r"^\s*(набираем|снимаем)\s+(с\b|с\.|в\b|из\b|силос)",
    r"^\s*(начали\s+)?рециркуляци\w*\b(?!.*(воздух|закончил|конец|стоп|откл))",
    r"^\s*(начали\s+)?охлаждение\b",
    r"^\s*откл\w*\.?\s*(т\.?\s*б\.?|блоки|горелк\w*|топк\w*)(\s*[.,;:!]|\s*$)",
    r"^\s*(вкл\w*\.?\s*т\.?\s*б\.?|включил\w*\s+(т\.?\s*б|блоки|горелк|топк))",
)]

_STEM_MEANING = (
    (("рецирк",), "рециркуляция: дальше зерно ходит по кругу, из силоса не подаётся, «выгружено» не растёт"),
    (("охлажд",), "горелки выключены (охлаждение): строки до «Запуск» не идут в калибровку, «Сейчас» пишет "
                  "«идёт охлаждение»"),
    (("остановк",), "остановка сушилки: новый участок журнала, напоминания затихают"),
    (("запуск",), "пуск: новый участок журнала, горелки работают"),
    (("закончил", "открыл", "переключ", "загрузка", "зачистка", "набираем", "снимаем"),
     "новый участок журнала — для программы это начало нового прохода"),
    (("включ", "вкл. т", "вкл т"), "горелки снова включены"),
)


def note_meaning(note: str) -> List[str]:
    """Что программа поймёт из примечания (по основам слов)."""
    n = (note or "").lower()
    out = [text for stems, text in _STEM_MEANING if any(w in n for w in stems)]
    if "откл" in n and any(w in n for w in ("т.б", "т. б", "тб", "блок", "горел", "топ")) and not any(
            "охлаждение" in t for t in out):
        out.append("горелки выключены: строки до «Запуск» не идут в калибровку")
    return out


def note_trap(note: str) -> Optional[List[str]]:
    """None — примечание без слов-команд или явное событие по образцу; иначе — что поймёт программа
    (повод переспросить: возможно, это обычный комментарий со словом-ловушкой)."""
    meaning = note_meaning(note)
    if not meaning:
        return None
    if any(p.search(note or "") for p in _EVENT_PATTERNS):
        return None
    return meaning


def _t_proc(row: Dict, source: str) -> Optional[float]:
    if source == "block":
        vals = [x for x in (row.get("t_up_f"), row.get("t_low_f")) if x is not None]
    else:
        vals = [x for x in (row.get("t_a1"), row.get("t_a2")) if x is not None]
    return sum(vals) / len(vals) if vals else None


def _inlet_series(prep: List[Dict]) -> Dict[int, List[Tuple[float, float]]]:
    ser: Dict[int, List[Tuple[float, float]]] = {}
    for r in prep:
        if r.get("w_in") is not None:
            ser.setdefault(r["seg"], []).append((r["t"], r["w_in"]))
    return ser


def _interp_inlet(points: List[Tuple[float, float]], t: float) -> Optional[float]:
    if not points:
        return None
    if t < points[0][0] - 1e-9:
        return None
    if t >= points[-1][0]:
        return points[-1][1] if t - points[-1][0] <= 1.0 else None
    for (t0, w0), (t1, w1) in zip(points, points[1:]):
        if t0 <= t <= t1:
            return w0 if t1 == t0 else w0 + (w1 - w0) * (t - t0) / (t1 - t0)
    return None


def observe_all(rows: List[Dict], p: DryerParams, use_lag: Optional[bool] = None) -> Tuple[List[Dict], Dict[str, int], bool]:
    """Журнал -> список наблюдений. Возвращает (наблюдения, отброшено по причинам, использован ли лаг)."""
    prep, reliable = prepare(rows)
    lag = reliable if use_lag is None else (use_lag and reliable)
    series = _inlet_series(prep)
    obs: List[Dict] = []
    skipped: Dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for r in prep:
        w_out, d = r.get("w_out"), r.get("d_f")
        if w_out is None or d is None:
            if r.get("w_in") is not None or r.get("w_out") is not None:
                skip("нет данных")
            continue
        if r.get("burner_off"):
            skip("горелки выключены (охлаждение)")
            continue
        if not (0 < d <= 100):
            skip("вне диапазона")
            continue
        q = q_at(p, d)
        tau = p.lag_k * p.m_dryer / q if lag and q > 0 else 0.0
        if lag:
            w_in = _interp_inlet(series.get(r["seg"], []), r["t"] - tau)
        else:
            w_in = r.get("w_in")
        if w_in is None:
            skip("ещё идёт прежнее зерно" if lag else "нет входа")
            continue
        t_proc = _t_proc(r, p.temp_source)
        if t_proc is None:
            skip("нет температуры")
            continue
        tg = 0.5 * (r["t_a1"] + r["t_a2"]) if r.get("t_a1") is not None and r.get("t_a2") is not None else None
        w_true = true_from_read(p, w_out, tg)
        dw = w_in - w_true
        if not (8 < w_in < 50 and 3 <= w_true < w_in and 0.3 <= dw <= 20):
            skip("вне диапазона")
            continue
        t_amb, rh = r.get("t_amb"), r.get("rh")
        obs.append({
            "r": q * dw / (100.0 - w_in), "w_in": w_in, "w_out": w_true, "w_out_read": w_out, "d": d, "q": q,
            "w_avg": 0.5 * (w_in + w_true), "t_proc": t_proc,
            "t_amb": p.t_amb_ref if t_amb is None else t_amb,
            "rh": p.rh_ref if rh is None else rh,
            "has_amb": t_amb is not None, "has_rh": rh is not None,
            "seg": r["seg"], "dt": r["dt"], "t": r["t"],
        })
    return obs, skipped, lag


# ----------------------------------------------------------------------------
# Небольшая математика
# ----------------------------------------------------------------------------

def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _std(xs: Sequence[float]) -> float:
    """Стандартное отклонение (генеральное)."""
    return float(np.std(np.asarray(xs, dtype=float)))


def _solve(a, b) -> List[float]:
    """Решение A·x = b (LU-разложение с выбором главного элемента, LAPACK)."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    try:
        if np.linalg.cond(a) > 1e14:
            raise np.linalg.LinAlgError
        return np.linalg.solve(a, b).tolist()
    except np.linalg.LinAlgError:
        raise ValueError("вырожденная система при подборе параметров") from None


def _obs_arr(obs: Sequence[Dict]) -> Dict[str, np.ndarray]:
    from .physics import obs_arrays
    return obs_arrays(obs)


def predict_out(p: DryerParams, obs: Sequence[Dict], arr: Optional[Dict[str, np.ndarray]] = None
                ) -> Tuple[np.ndarray, np.ndarray]:
    """Модельная влажность на выходе (истинная) и Q для всех наблюдений сразу: как solve_pass(...)."""
    a = arr if arr is not None else _obs_arr(obs)
    return solve_out_arr(p, a["w_in"], a["d"], a["t_proc"], a["t_amb"], a["rh"])


# ----------------------------------------------------------------------------
# Калибровка
# ----------------------------------------------------------------------------

def _features(obs: List[Dict], p: DryerParams) -> Dict[str, np.ndarray]:
    def col(k: str) -> np.ndarray:
        return np.fromiter((o[k] for o in obs), dtype=float, count=len(obs))
    fw = np.log(np.maximum(col("w_avg") - p.w_eq, 0.05) / max(p.w_ref - p.w_eq, 0.1))
    ft = np.log(np.maximum(col("t_proc") - p.t_amb_ref, 1.0) / max(p.t_ref - p.t_amb_ref, 1.0))
    famb = (col("t_amb") - p.t_amb_ref) / 10.0
    frh = (col("rh") - p.rh_ref) / 30.0
    return {"b_w": fw, "b_dt": ft, "b_amb": famb, "b_rh": frh}


def recency_weights(obs: Sequence[Dict], half_life_days: float) -> List[float]:
    """Вес наблюдения 0,5^(возраст / полураспад): свежие проходы важнее старых. 0 — все веса 1."""
    if not half_life_days or half_life_days <= 0:
        return [1.0] * len(obs)
    dts = [o.get("dt") for o in obs if o.get("dt") is not None]
    if not dts:
        return [1.0] * len(obs)
    last = max(dts)
    out = []
    for o in obs:
        dt = o.get("dt")
        age = (last - dt).total_seconds() / 86400.0 if dt is not None else 0.0
        out.append(0.5 ** (age / half_life_days))
    return out


def segment_summary(obs: List[Dict], p: DryerParams, arr: Optional[Dict[str, np.ndarray]] = None) -> List[Dict]:
    """Сводка по участкам журнала: наблюдаемый и модельный съём за проход."""
    out: List[Dict] = []
    segs: Dict[int, List[int]] = {}
    for i, o in enumerate(obs):
        segs.setdefault(o["seg"], []).append(i)
    if not any(len(ii) >= 4 for ii in segs.values()):
        return out
    a = arr if arr is not None else _obs_arr(obs)
    dw_pred_all = a["w_in"] - predict_out(p, obs, a)[0]          # модель — один раз для всех наблюдений
    for s, ii in sorted(segs.items()):
        if len(ii) < 4:
            continue
        lst = [obs[i] for i in ii]
        pred = dw_pred_all[np.asarray(ii)].tolist()
        dts = [o["dt"] for o in lst if o["dt"] is not None]
        out.append({
            "seg": s, "n": len(lst),
            "start": min(dts) if dts else None, "end": max(dts) if dts else None,
            "w_in": _mean([o["w_in"] for o in lst]),
            "w_out": _mean([o["w_out"] for o in lst]),
            "d": _mean([o["d"] for o in lst]),
            "t": _mean([o["t_proc"] for o in lst]),
            "dw_obs": _mean([o["w_in"] - o["w_out"] for o in lst]),
            "dw_pred": _mean(pred),
            "r_obs": _mean([o["r"] for o in lst]),
        })
    return out


def calibrate(rows: List[Dict], p: DryerParams, lam: float = 3.0, half_life_days: float = 0.0,
              weather=None) -> Tuple[DryerParams, Dict]:
    """Подбирает эмпирическую модель (R_ref и при достаточных данных показатели) и КПД слоя η физической.

    half_life_days > 0 — свежие наблюдения весят больше (вес 0,5^(возраст/полураспад)).
    weather — WeatherSeries: пустые T/RH наружные в журнале заполняются из таблицы погоды.
    """
    if weather is not None:
        rows = weather.fill_rows(rows)
    obs, skipped, lag = observe_all(rows, p)
    return calibrate_obs(obs, p, lam, half_life_days, skipped=skipped, lag=lag, n_rows=len(rows))


# Априорная неопределённость (1σ) коэффициентов, которые не подобраны по журналу: b_нар и b_rh оценены по
# физической модели (прогрев холодного зерна, равновесная влажность), а не измерены; b_w, b_dt — если строк мало.
PRIOR_SD = {"b_w": 0.3, "b_dt": 0.4, "b_amb": 0.03, "b_rh": 0.10}


def _param_cov(x: np.ndarray, wt: np.ndarray, a: np.ndarray, res: np.ndarray, groups: np.ndarray
               ) -> Tuple[np.ndarray, float]:
    """Ковариация коэффициентов взвешенной регрессии с регуляризацией (сэндвич A⁻¹·XᵀWX·A⁻¹·s²), умноженная на
    эффект плана: строки одного прохода не независимы (общие партия, погода, состояние сушилки), поэтому информации в
    них меньше, чем строк. Эффект плана 1 + (m̄ − 1)·ρ, ρ — внутриклассовая корреляция остатков по проходам."""
    n, k = x.shape
    sw = float(wt.sum())
    s2 = float(np.sum(wt * res * res) / max(sw - k, 1.0))
    a_inv = np.linalg.inv(a)
    cov = s2 * a_inv @ (x.T @ (wt[:, None] * x)) @ a_inv
    ids = np.unique(groups)
    g = len(ids)
    if g >= 2 and n > g:
        sizes = np.array([np.sum(groups == i) for i in ids], dtype=float)
        means = np.array([res[groups == i].mean() for i in ids])
        msb = float(np.sum(sizes * (means - res.mean()) ** 2) / (g - 1))
        msw = float(sum(np.sum((res[groups == i] - m) ** 2) for i, m in zip(ids, means)) / (n - g))
        n0 = (n - float(np.sum(sizes ** 2)) / n) / (g - 1)
        den = msb + (n0 - 1.0) * msw
        rho = min(max((msb - msw) / den, 0.0), 1.0) if den > 0 else 0.0
        deff = 1.0 + (n / g - 1.0) * rho
    else:
        deff = float(n)                    # один проход: строки почти ничего не добавляют друг к другу
    return cov * deff, deff


def calibrate_obs(obs: List[Dict], p: DryerParams, lam: float = 3.0, half_life_days: float = 0.0,
                  skipped: Optional[Dict[str, int]] = None, lag: bool = True, n_rows: Optional[int] = None,
                  report: bool = True) -> Tuple[DryerParams, Dict]:
    """Калибровка по готовым наблюдениям (observe_all). report=False — без сводок и текста (для проверки
    модели, где калибровка повторяется для каждого прохода)."""
    from .physics import calibrate_physics
    skipped = skipped or {}
    n = len(obs)
    n_rows = n if n_rows is None else n_rows
    if n < 3:
        raise ValueError(
            f"Пригодных строк {n} из {n_rows} (нужно хотя бы 3). Отброшено: {skipped or 'нет'}. "
            f"Проверьте: время пробы, влага вход/выход, разгрузка D и температура выбранного источника "
            f"({'блоки' if p.temp_source == 'block' else 'зерно'}); выход отстаёт от входа на "
            f"{p.lag_k * p.m_dryer / max(q_at(p, 35), 1e-9):.1f} ч при D=35, эти часы в начале каждого прохода не используются.")

    wts_all = np.asarray(recency_weights(obs, half_life_days), dtype=float)
    y = np.log(np.fromiter((o["r"] for o in obs), dtype=float, count=n))
    feats = _features(obs, p)
    prior = {"b_w": p.b_w, "b_dt": p.b_dt, "b_amb": p.b_amb, "b_rh": p.b_rh}
    has_rh = sum(o["has_rh"] for o in obs) >= 0.5 * n
    has_amb = sum(o["has_amb"] for o in obs) >= 0.5 * n

    def fit(idx: List[int]):
        """Взвешенная регрессия ln R с регуляризацией к априорным показателям (ridge к prior)."""
        ii = np.asarray(idx, dtype=int)
        m = len(ii)
        use: List[str] = []
        if m >= 12:
            for k in feats:
                if float(np.std(feats[k][ii])) > 0.02:
                    use.append(k)
        if "b_rh" in use and not has_rh:
            use.remove("b_rh")
        if "b_amb" in use and not has_amb:
            use.remove("b_amb")
        wt = wts_all[ii]
        y_adj = y[ii].copy()
        for k in feats:
            if k not in use:
                y_adj -= prior[k] * feats[k][ii]
        x = np.column_stack([np.ones(m)] + [feats[k][ii] for k in use])      # матрица X
        pen = np.array([0.0] + [lam] * len(use))
        b_prior = np.array([0.0] + [prior[k] for k in use])
        a = x.T @ (wt[:, None] * x) + np.diag(pen)
        rhs = x.T @ (wt * y_adj) + pen * b_prior
        beta = _solve(a, rhs)
        coef = dict(prior)
        for k, v in zip(use, beta[1:]):
            coef[k] = float(v)
        res = (y_adj - x @ np.asarray(beta)).tolist()
        fit.last = (x, wt, a, ii)        # для оценки разброса коэффициентов
        return float(beta[0]), coef, res, use

    idx = list(range(n))
    b0, coef, res, use = fit(idx)
    n_out = 0
    if n >= 15:
        med = _median(res)
        mad = _median([abs(r - med) for r in res]) * 1.4826
        if mad > 1e-9:
            keep = [abs(r - med) <= 3.0 * mad for r in res]
            n_keep = sum(keep)
            if 0.7 * n <= n_keep < n:
                n_out = n - n_keep
                idx = [i for i, kp in zip(idx, keep) if kp]
                b0, coef, res, use = fit(idx)

    used = [obs[i] for i in idx]
    used_w = wts_all[np.asarray(idx, dtype=int)].tolist()
    used_arr = _obs_arr(used)
    emp = replace(p, model_kind="empirical", r_ref=math.exp(b0), b_w=coef["b_w"], b_dt=coef["b_dt"],
                  b_amb=coef["b_amb"], b_rh=coef["b_rh"])
    # κ не подбирается: на журнале октября подбор κ улучшал описание (±0,48 против ±0,51 п.п. по проходам),
    # но ухудшал прогноз исключённого прохода (±1,1 против ±0,64) — переобучение. κ задаётся вручную.
    eta, kappa, phys_rep = calibrate_physics(used, replace(p, model_kind="physics"), used_w, fit_kappa=False)
    phys = replace(p, model_kind="physics", eta=eta, kappa=kappa)

    def errors(par: DryerParams) -> Tuple[float, float, float]:
        errs = predict_out(par, used, used_arr)[0] - used_arr["w_out"]
        return float(np.mean(np.abs(errs))), float(np.mean(errs)), float(np.max(np.abs(errs)))

    mae, bias, mx = errors(emp)
    stamp = f"{datetime.now():%d.%m.%Y}"
    w_txt = f", вес свежих: полураспад {half_life_days:g} сут" if half_life_days and half_life_days > 0 else ""
    new_p = replace(emp, model_kind=p.model_kind, eta=eta, kappa=kappa, calibrated=True,
                    calib_info=f"{stamp}, строк {len(used)}, ошибка w_вых ±{mae:.2f} п.п.{w_txt}",
                    phys_info=f"{stamp}, строк {len(used)}, η = {eta:.2f}, κ = {kappa:.2f}, ошибка w_вых ±{phys_rep['mae']:.2f} п.п.")
    if not report:
        return new_p, {"n_used": len(used), "mae": mae, "bias": bias, "phys_mae": phys_rep["mae"], "eta": eta,
                       "kappa": kappa}
    mae_old, bias_old, _ = errors(replace(p, model_kind="empirical"))
    rmse_ln = math.sqrt(_mean([r * r for r in res]))
    segs = segment_summary(used, emp, used_arr)
    segs_phys = segment_summary(used, phys, used_arr)
    # --- v0.8: данные для честных допусков ---
    x_m, wt_m, a_m, ii_m = fit.last
    cov_fit, deff = _param_cov(x_m, wt_m, a_m, np.asarray(res), np.asarray([obs[i]["seg"] for i in ii_m]))
    # коэффициенты, не подобранные по журналу (нет данных или разброса), известны только приблизительно —
    # их априорная неопределённость тоже входит в допуск
    fixed = [k for k in feats if k not in use]
    unc_feats = ["const"] + list(use) + fixed
    cov = np.zeros((len(unc_feats), len(unc_feats)))
    cov[:len(cov_fit), :len(cov_fit)] = cov_fit
    for j, k in enumerate(fixed, start=len(cov_fit)):
        cov[j, j] = PRIOR_SD[k] ** 2
    f_full = np.column_stack([np.ones(len(ii_m))] + [feats[k][ii_m] for k in unc_feats[1:]])
    lev = float(np.mean(np.einsum("ij,jk,ik->i", f_full, cov, f_full))) if len(ii_m) else 0.0
    phys_by = {sp["seg"]: sp for sp in segs_phys}
    rel = [abs(phys_by[sg["seg"]]["dw_pred"] - sg["dw_pred"]) / max(sg["dw_pred"], 0.5) for sg in segs if sg["seg"] in phys_by]
    struct_base = float(np.median(rel)) if rel else p.struct_base
    explored = {"d": [float(used_arr["d"].min()), float(used_arr["d"].max())],
                "t": [float(used_arr["t_proc"].min()), float(used_arr["t_proc"].max())],
                "w": [float(used_arr["w_in"].min()), float(used_arr["w_in"].max())]}
    new_p = replace(new_p, unc_feats=unc_feats, unc_cov=cov.tolist(), unc_lev=lev,
                    struct_base=struct_base, explored=explored)
    seg_mae = _mean([abs(s["dw_pred"] - s["dw_obs"]) for s in segs]) if segs else float("nan")
    seg_mae_phys = _mean([abs(s["dw_pred"] - s["dw_obs"]) for s in segs_phys]) if segs_phys else float("nan")
    w_pred, q_pred = predict_out(new_p, used, used_arr)
    pairs = list(zip(used_arr["r"].tolist(), pass_rate_arr(used_arr["w_in"], w_pred, q_pred).tolist()))

    skipped_txt = f" ({', '.join(f'{k}: {v}' for k, v in skipped.items())})" if skipped else ""
    n_amb = sum(o["has_amb"] for o in used)
    lines = [
        f"Строк в журнале: {n_rows}; использовано: {len(used)}; выбросов: {n_out}; "
        f"отброшено: {sum(skipped.values())}{skipped_txt}",
        ("Лаг выхода от входа учтён: τ = %.1f ч при D=35." % (p.lag_k * p.m_dryer / max(q_at(p, 35), 1e-9)))
        if lag else "Времени в журнале нет или оно не распознано: лаг не учитывается (вход и выход берутся из одной строки).",
        (f"Наружная T/RH есть у {n_amb} из {len(used)} строк (журнал + таблица погоды)." if n_amb else
         "Наружной T/RH нет ни в журнале, ни в таблице погоды: взяты опорные 10 °C / 70 %."),
        ("Свежие данные весят больше: полураспад %g сут." % half_life_days) if half_life_days and half_life_days > 0
        else "Все строки журнала с одинаковым весом.",
        "",
        "ЭМПИРИЧЕСКАЯ МОДЕЛЬ" + (" (используется в расчётах)" if p.model_kind != "physics" else ""),
        f"R_ref: {p.r_ref:.2f} → {emp.r_ref:.2f} т воды/ч (при w={p.w_ref:g} %, T={p.t_ref:g} °C, T_нар={p.t_amb_ref:g} °C)",
        f"Показатели: b_w={emp.b_w:.2f}, b_dt={emp.b_dt:.2f}, b_нар={emp.b_amb:.2f}, b_rh={emp.b_rh:.2f}" +
        ("" if use else "  (оставлены по умолчанию: данных мало или нет разброса)"),
        f"Ошибка прогноза w_вых по строкам: было ±{mae_old:.2f} п.п. (смещение {bias_old:+.2f}) → стало ±{mae:.2f} п.п. "
        f"(смещение {bias:+.2f}, максимум {mx:.2f})",
        f"Разброс R: ±{(math.exp(rmse_ln) - 1) * 100:.0f} %",
        "",
        "ФИЗИЧЕСКАЯ МОДЕЛЬ (тонкий слой Thompson + КПД слоя)" + (" (используется в расчётах)" if p.model_kind == "physics" else ""),
        f"КПД слоя η: {p.eta:.2f} → {eta:.2f} (κ = {kappa:.2f} задан вручную)"
        + ("  — η на границе диапазона, проверьте данные" if phys_rep["at_edge"] else ""),
        f"Ошибка прогноза w_вых по строкам: ±{phys_rep['mae']:.2f} п.п. (смещение {phys_rep['bias']:+.2f}, максимум {phys_rep['max']:.2f})",
    ]
    lines.append("")
    lines.append(f"ДОПУСКИ: разброс коэффициентов ±{100 * math.sqrt(lev):.1f} % скорости сушки в изученных режимах "
                 f"(с поправкой на зависимость строк внутри прохода ×{deff:.1f}); вне их он растёт. Типичное расхождение "
                 f"моделей на проходах журнала {100 * struct_base:.0f} %. Изучено: D {explored['d'][0]:.0f}–"
                 f"{explored['d'][1]:.0f} с, T {explored['t'][0]:.0f}–{explored['t'][1]:.0f} °C, W вх "
                 f"{explored['w'][0]:.0f}–{explored['w'][1]:.0f} %.")
    if segs:
        lines.append("")
        lines.append(f"Съём за проход, средняя ошибка по {len(segs)} участкам: эмпирическая ±{seg_mae:.2f} п.п., "
                     f"физическая ±{seg_mae_phys:.2f} п.п.")
        lines.append("Участок                 строк  W вх  W вых   D    T   Δ факт  Δ эмп.  Δ физ.")
        phys_by_seg = {sp["seg"]: sp for sp in segs_phys}
        for sg in segs:
            a = f"{sg['start']:%d.%m %H:%M}" if sg["start"] else "?"
            b = f"{sg['end']:%d.%m %H:%M}" if sg["end"] else "?"
            sp = phys_by_seg.get(sg["seg"])
            lines.append(f"{a}–{b}   {sg['n']:4d}  {sg['w_in']:5.1f} {sg['w_out']:5.1f} {sg['d']:5.1f} {sg['t']:4.0f}  "
                         f"{sg['dw_obs']:5.1f}   {sg['dw_pred']:5.1f}   {sp['dw_pred'] if sp else float('nan'):5.1f}")
    if len(used) < 12:
        lines.append("Данных мало (<12 строк): подобран только R_ref, показатели степеней не менялись.")
    if min(mae, phys_rep["mae"]) > 0.5:
        lines.append("Ошибка выше 0,5 п.п.: проверьте Q(D), вместимость сушилки (лаг), источник температуры "
                     "и поправку влагомера на выходе.")
    return new_p, {"text": "\n".join(lines), "n": n_rows, "n_used": len(used), "mae": mae,
                   "bias": bias, "pairs": pairs, "segments": segs, "seg_mae": seg_mae, "lag": lag,
                   "phys_mae": phys_rep["mae"], "phys_bias": phys_rep["bias"], "segments_phys": segs_phys,
                   "seg_mae_phys": seg_mae_phys, "eta": eta, "kappa": kappa, "used": used}
