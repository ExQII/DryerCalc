"""Общие мелочи интерфейса: поля ввода, ячейки таблиц, форматирование."""
from __future__ import annotations

import re
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDoubleSpinBox, QFrame, QHeaderView, QLabel, QScrollArea, QTableWidget,
                               QTableWidgetItem, QWidget)

SEP = re.compile(r"[;/\s]+")


def dspin(lo: float, hi: float, val: float, dec: int = 1, step: Optional[float] = None, suffix: str = "") -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setValue(val)
    if step:
        s.setSingleStep(step)
    if suffix:
        s.setSuffix(suffix)
    return s


def fmt_hours(h: float) -> str:
    if h >= 24:
        return f"{h:.1f} ч ({int(h // 24)} д {h % 24:.0f} ч)"
    return f"{h:.1f} ч"


def fnum(v, nd: int = 1, unit: str = "") -> str:
    if v is None:
        return "—"
    s = f"{v:,.{nd}f}".replace(",", " ")
    return s + (f" {unit}" if unit else "")


def item(text: str, align_right: bool = False) -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    if align_right:
        it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return it


def edit_item(text: str) -> QTableWidgetItem:
    return QTableWidgetItem(text)


def parse_ints(txt: str, lo: float = 0, hi: float = 100) -> List[float]:
    """'35' / '35,40,40' / '70 70 80' -> числа; для D (целые секунды)."""
    vals = [float(x) for x in re.findall(r"\d+", txt)]
    return [v for v in vals if lo < v <= hi]


def parse_decimals(txt: str, lo: float = 0, hi: float = 1000) -> Optional[List[float]]:
    """'52,6' / '55; 50; 45' -> числа (запятая — десятичная). None, если есть мусор."""
    out: List[float] = []
    for tok in SEP.split(txt.strip()):
        if not tok:
            continue
        try:
            out.append(float(tok.replace(",", ".")))
        except ValueError:
            return None
    return [x for x in out if lo < x <= hi]


def to_num(txt) -> Optional[float]:
    if txt is None:
        return None
    s = str(txt).strip().replace(" ", "").replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def table(headers: List[str], stretch_last: bool = True) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(stretch_last)
    return t


def note_label(text: str = "") -> QLabel:
    lb = QLabel(text)
    lb.setWordWrap(True)
    lb.setProperty("muted", True)
    return lb


def scroll_wrap(w: QWidget, max_w: Optional[int] = None) -> QScrollArea:
    """Левая панель с полями — в прокрутку: на маленьком экране или крупном шрифте окно не вылезает за экран."""
    sc = QScrollArea()
    sc.setWidget(w)
    sc.setWidgetResizable(True)
    sc.setFrameShape(QFrame.NoFrame)
    sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    sc.setMinimumWidth(min(w.minimumSizeHint().width() + 20, max_w or 10000))
    if max_w:
        sc.setMaximumWidth(max_w)
    return sc
