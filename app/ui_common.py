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


class Collapsible(QWidget):
    """«▸ Заголовок» — по щелчку раскрывает содержимое (пояснение или виджет). Длинные пояснения на страницах
    свёрнуты, чтобы не заслонять главное; состояние раскрытия меняет только сам человек."""

    def __init__(self, title: str, content=None, expanded: bool = False, parent: Optional[QWidget] = None):
        from PySide6.QtWidgets import QToolButton, QVBoxLayout
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.button = QToolButton()
        self.button.setText(title)
        self.button.setCheckable(True)
        self.button.setChecked(expanded)
        self.button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.button.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.button.setAutoRaise(True)
        self.button.toggled.connect(self._toggled)
        lay.addWidget(self.button, 0, Qt.AlignLeft)
        if content is None or isinstance(content, str):
            lb = QLabel(content or "")
            lb.setWordWrap(True)
            lb.setTextFormat(Qt.RichText)
            lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
            content = lb
        self.content = content
        lay.addWidget(content)
        content.setVisible(expanded)

    def _toggled(self, on: bool) -> None:
        self.button.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self.content.setVisible(on)

    def set_expanded(self, on: bool) -> None:
        self.button.setChecked(on)

    def is_expanded(self) -> bool:
        return self.button.isChecked()


class Tile(QFrame):
    """Плитка «подпись / крупное число / пояснение» для главных цифр страницы."""

    def __init__(self, title: str, parent: Optional[QWidget] = None):
        from PySide6.QtWidgets import QVBoxLayout
        super().__init__(parent)
        self.setObjectName("tile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 8)
        lay.setSpacing(0)
        self.lbl_title = QLabel(title)
        self.lbl_value = QLabel("—")
        self.lbl_sub = QLabel("")
        self.lbl_sub.setWordWrap(True)
        self.lbl_title.setWordWrap(True)
        self.setMinimumWidth(10)
        for w in (self.lbl_title, self.lbl_value, self.lbl_sub):
            lay.addWidget(w)
        lay.addStretch(1)

    def set(self, value: str, sub: str = "", tip: str = "") -> None:
        self.lbl_value.setText(value)
        self.lbl_sub.setText(sub)
        self.setToolTip(tip)

    def style_for(self, th, pt: float) -> None:
        from .theme import mix
        border = mix(th.window, th.text, 0.18).name()
        self.setStyleSheet(f"#tile {{ background:{th.base.name()}; border:1px solid {border}; border-radius:6px; }}")
        self.lbl_title.setStyleSheet(f"color:{th.muted};")
        self.lbl_value.setStyleSheet(f"font-size:{pt + 4:.1f}pt; font-weight:600; color:{th.base_text.name()};")
        self.lbl_sub.setStyleSheet(f"color:{th.muted};")


def size_by_current(tabs) -> None:
    """Вложенные вкладки (QTabWidget): минимальный размер — только по открытой вкладке, а не по самой широкой из
    скрытых (у скрытых он ещё и не обновляется после смены шрифта в режиме сушильщика)."""
    from PySide6.QtWidgets import QSizePolicy

    def upd(i: int) -> None:
        for k in range(tabs.count()):
            pol = QSizePolicy.Preferred if k == i else QSizePolicy.Ignored
            tabs.widget(k).setSizePolicy(pol, pol)
    tabs.currentChanged.connect(upd)
    upd(tabs.currentIndex())
