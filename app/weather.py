"""Наружная погода (T, RH) без интернета: ручные замеры и файлы, принесённые на флешке.

Поддерживаемые файлы (CSV, CSV.GZ, TXT, XLSX):
* архив rp5.ru («Скачать архив погоды», CSV, любая кодировка: UTF-8 / ANSI / Unicode) — строки-комментарии
  начинаются с «#», колонки «Местное время в …», «T», «U»;
* Open-Meteo (Historical / Forecast → Download CSV) — сначала строки с координатами, затем
  «time,temperature_2m (°C),relative_humidity_2m (%)»;
* выгрузка логгера температуры и влажности или своя таблица: колонки с датой-временем, температурой и
  относительной влажностью (названия распознаются по словам «время/дата/time», «T/температура/temperature»,
  «U/RH/влажность/humidity»). Дата и время могут быть в разных колонках.

Значения к строкам журнала подставляются по времени: линейная интерполяция между соседними замерами, если
они не дальше 6 ч друг от друга, иначе ближайший замер в пределах 3 ч, иначе ничего.
"""
from __future__ import annotations

import csv
import gzip
import re
from bisect import bisect_left
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

MAX_GAP_H = 6.0       # между соседними замерами для интерполяции
MAX_NEAR_H = 3.0      # до ближайшего замера, если интерполировать нельзя


# ----------------------------------------------------------------------------
# Разбор значений
# ----------------------------------------------------------------------------

_NUM = re.compile(r"^\s*[-+−]?\d+(?:[.,]\d+)?\s*$")


def _num(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = str(v).strip().strip('"').replace("−", "-")
    if not _NUM.match(s):
        return None
    return float(s.replace(",", "."))


_DT_PATTERNS = (
    re.compile(r"^(\d{1,2})[./](\d{1,2})[./](\d{2,4})[ T,]+(\d{1,2})[:.](\d{2})(?::\d{2})?"),
    re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::\d{2})?"),
)
_D_PATTERNS = (re.compile(r"^(\d{1,2})[./](\d{1,2})[./](\d{2,4})$"), re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"))
_T_PATTERN = re.compile(r"^(\d{1,2})[:.](\d{2})(?::\d{2})?$")


def parse_dt(v) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.replace(second=0, microsecond=0)
    s = str(v).strip().strip('"')
    m = _DT_PATTERNS[0].match(s)
    if m:
        d, mo, y, hh, mm = (int(x) for x in m.groups())
        y = y + 2000 if y < 100 else y
        try:
            return datetime(y, mo, d, hh, mm)
        except ValueError:
            return None
    m = _DT_PATTERNS[1].match(s)
    if m:
        y, mo, d, hh, mm = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d, hh, mm)
        except ValueError:
            return None
    return None


def _parse_date(v) -> Optional[date]:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v or "").strip().strip('"')
    m = _D_PATTERNS[0].match(s)
    if m:
        d, mo, y = (int(x) for x in m.groups())
        y = y + 2000 if y < 100 else y
    else:
        m = _D_PATTERNS[1].match(s)
        if not m:
            return None
        y, mo, d = (int(x) for x in m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _parse_time(v) -> Optional[dtime]:
    if isinstance(v, dtime):
        return v.replace(second=0, microsecond=0)
    if isinstance(v, datetime):
        return v.time().replace(second=0, microsecond=0)
    m = _T_PATTERN.match(str(v or "").strip().strip('"'))
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    return dtime(hh, mm) if hh < 24 and mm < 60 else None


# ----------------------------------------------------------------------------
# Заголовки
# ----------------------------------------------------------------------------

def _norm(h) -> str:
    return re.sub(r"[^0-9a-zа-яё]", "", str(h or "").lower())


def classify_header(h) -> Optional[str]:
    """'dt' — дата-время, 'date', 'time', 't' — температура воздуха, 'rh' — относительная влажность."""
    raw = str(h or "").strip().strip('"').lower()
    n = _norm(h)
    if not n:
        return None
    if n in ("t", "tc", "tair", "tнар", "tнаружная", "tвоздуха", "tamb") or raw in ("t, °c", "t,°c", "t (°c)", "t °c"):
        return "t"
    if n in ("u", "rh", "rhнар", "rhнаружная", "rhproc") or n.startswith("rh") or n.startswith("relativehumidity"):
        return "rh"
    if "dew" in n or "росы" in n or n in ("td", "tn", "tx", "tg", "tmin", "tmax") or "почв" in n:
        return None
    if "humidity" in n or "влажн" in n:
        return "rh"
    if "temperature" in n or "температур" in n or n.startswith("temp"):
        return "t"
    if "время" in n and "дата" in n:
        return "dt"
    if n in ("датавремя", "datetime", "timestamp", "datatime") or "местноевремя" in n or n.startswith("time") or "utc" in n:
        return "dt"
    if n in ("дата", "date", "day", "день"):
        return "date"
    if n in ("время", "часы", "hour", "clock"):
        return "time"
    if "время" in n:
        return "dt"
    return None


# ----------------------------------------------------------------------------
# Чтение файлов
# ----------------------------------------------------------------------------

def _decode(raw: bytes) -> str:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("Не удалось определить кодировку файла (UTF-8, Windows-1251 или Unicode)")


def _table_from_text(text: str) -> List[List[str]]:
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    best: Optional[Tuple[int, str]] = None
    for i, ln in enumerate(lines[:60]):           # строка заголовка: в ней есть время и температура/влажность
        for delim in (";", ",", "\t"):
            cells = next(csv.reader([ln], delimiter=delim))
            kinds = {classify_header(c) for c in cells}
            if ("dt" in kinds or "date" in kinds) and ("t" in kinds or "rh" in kinds):
                best = (i, delim)
                break
        if best:
            break
    if not best:
        raise ValueError("Не нашёл строку заголовка с колонками времени и температуры/влажности.")
    i0, delim = best
    return [r for r in csv.reader(lines[i0:], delimiter=delim)]


def _table_from_xlsx(path: str) -> List[List]:
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    finally:
        wb.close()
    for i, r in enumerate(rows[:60]):
        kinds = {classify_header(c) for c in r if c is not None}
        if ("dt" in kinds or "date" in kinds) and ("t" in kinds or "rh" in kinds):
            return rows[i:]
    raise ValueError("Не нашёл строку заголовка с колонками времени и температуры/влажности.")


def read_weather(path: str) -> Tuple[List[Dict], Dict]:
    """Файл погоды -> ([{dt, t, rh}], info). Колонки распознаются по названиям."""
    ext = "".join(Path(path).suffixes).lower()
    if ext.endswith(".xls"):
        raise ValueError("Старый формат .xls не читается: сохраните файл как CSV или XLSX (в rp5 выберите «CSV»).")
    table = _table_from_xlsx(path) if ext.endswith((".xlsx", ".xlsm")) else _table_from_text(_decode(Path(path).read_bytes()))
    header = table[0]
    cols: Dict[str, int] = {}
    for i, h in enumerate(header):
        k = classify_header(h)
        if k and k not in cols:
            cols[k] = i
    if "dt" not in cols and not ("date" in cols and "time" in cols):
        if "date" in cols:
            cols["dt"] = cols.pop("date")
        else:
            raise ValueError("Нет колонки даты-времени.")
    out: List[Dict] = []
    bad = 0
    for r in table[1:]:
        def cell(k):
            i = cols.get(k)
            return r[i] if i is not None and i < len(r) else None
        if "dt" in cols:
            dt = parse_dt(cell("dt"))
        else:
            d, t = _parse_date(cell("date")), _parse_time(cell("time"))
            dt = datetime.combine(d, t) if d and t else None
        t_val = _num(cell("t")) if "t" in cols else None
        rh_val = _num(cell("rh")) if "rh" in cols else None
        if dt is None or (t_val is None and rh_val is None):
            bad += 1
            continue
        if t_val is not None and not (-60 <= t_val <= 60):
            t_val = None
        if rh_val is not None and not (0 <= rh_val <= 100):
            rh_val = None
        if t_val is None and rh_val is None:
            bad += 1
            continue
        out.append({"dt": dt, "t": t_val, "rh": rh_val})
    out.sort(key=lambda x: x["dt"])
    names = {k: str(header[i]).strip() for k, i in cols.items()}
    return out, {"columns": names, "skipped": bad,
                 "first": out[0]["dt"] if out else None, "last": out[-1]["dt"] if out else None}


# ----------------------------------------------------------------------------
# Ряд погоды и подстановка в журнал
# ----------------------------------------------------------------------------

class WeatherSeries:
    def __init__(self, points: Sequence[Dict]):
        pts = sorted((p for p in points if p.get("dt") is not None), key=lambda p: p["dt"])
        self.t = [(p["dt"], float(p["t"])) for p in pts if p.get("t") is not None]
        self.rh = [(p["dt"], float(p["rh"])) for p in pts if p.get("rh") is not None]
        self._tk = [a for a, _ in self.t]
        self._rk = [a for a, _ in self.rh]

    def __len__(self) -> int:
        return max(len(self.t), len(self.rh))

    @staticmethod
    def _at(keys: List[datetime], ser: List[Tuple[datetime, float]], dt: datetime) -> Optional[float]:
        if not ser:
            return None
        i = bisect_left(keys, dt)
        if i < len(keys) and keys[i] == dt:
            return ser[i][1]
        before = ser[i - 1] if i > 0 else None
        after = ser[i] if i < len(ser) else None
        if before and after and (after[0] - before[0]).total_seconds() <= MAX_GAP_H * 3600:
            f = (dt - before[0]).total_seconds() / (after[0] - before[0]).total_seconds()
            return before[1] + f * (after[1] - before[1])
        near = min((x for x in (before, after) if x), key=lambda x: abs((x[0] - dt).total_seconds()))
        return near[1] if abs((near[0] - dt).total_seconds()) <= MAX_NEAR_H * 3600 else None

    def at(self, dt: datetime) -> Tuple[Optional[float], Optional[float]]:
        return self._at(self._tk, self.t, dt), self._at(self._rk, self.rh, dt)

    def mean_between(self, start: datetime, end: datetime) -> Tuple[Optional[float], Optional[float], int]:
        ts = [v for d, v in self.t if start <= d <= end]
        rs = [v for d, v in self.rh if start <= d <= end]
        return (sum(ts) / len(ts) if ts else None, sum(rs) / len(rs) if rs else None, max(len(ts), len(rs)))

    def last_dt(self) -> Optional[datetime]:
        cands = [x for x in (self._tk[-1] if self._tk else None, self._rk[-1] if self._rk else None) if x]
        return max(cands) if cands else None

    def fill_rows(self, rows: List[Dict]) -> List[Dict]:
        """Копия журнала, где пустые T/RH наружные заполнены по времени строки (если есть замер рядом)."""
        if not len(self):
            return rows
        from .calibration import assign_times
        times, reliable = assign_times(rows)
        if not reliable:
            return rows
        out = []
        for r, dt in zip(rows, times):
            if dt is None or (r.get("t_amb") is not None and r.get("rh") is not None):
                out.append(r)
                continue
            t, rh = self.at(dt)
            nr = dict(r)
            if nr.get("t_amb") is None and t is not None:
                nr["t_amb"] = round(t, 1)
            if nr.get("rh") is None and rh is not None:
                nr["rh"] = round(rh, 0)
            out.append(nr)
        return out

    def coverage(self, rows: List[Dict]) -> Tuple[int, int]:
        """(строк журнала с T/RH после подстановки, всего строк с данными)."""
        filled = self.fill_rows(rows)
        total = sum(1 for r in filled if r.get("w_in") is not None or r.get("w_out") is not None)
        have = sum(1 for r in filled if (r.get("w_in") is not None or r.get("w_out") is not None)
                   and r.get("t_amb") is not None)
        return have, total


def merge_points(old: Sequence[Dict], new: Sequence[Dict]) -> List[Dict]:
    """Объединение: новые замеры заменяют старые с тем же временем (до минуты)."""
    by = {p["dt"].replace(second=0, microsecond=0): dict(p) for p in old}
    for p in new:
        key = p["dt"].replace(second=0, microsecond=0)
        cur = by.get(key, {})
        merged = dict(cur)
        merged.update({k: v for k, v in p.items() if v is not None or k not in cur})
        merged["dt"] = key
        by[key] = merged
    return [by[k] for k in sorted(by)]
