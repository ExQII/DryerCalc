"""Общие мелочи интерфейса: поля ввода, ячейки таблиц, форматирование."""
from __future__ import annotations

import re
from typing import List, Optional

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QDoubleSpinBox, QFrame, QHeaderView, QLabel, QScrollArea, QStyledItemDelegate,
                               QTableWidget, QTableWidgetItem, QWidget)

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


class _RowLine(QStyledItemDelegate):
    """Обычная отрисовка клетки (с её цветом фона — уровни сценариев, выделение) и тонкая черта под строкой."""

    def paint(self, p, option, index) -> None:
        super().paint(p, option, index)
        from . import design
        p.save()
        p.setPen(QColor(design.tokens_for(option.widget).line_soft))
        r = option.rect
        p.drawLine(r.left(), r.bottom(), r.right(), r.bottom())
        p.restore()


def clean_table(t: QTableWidget) -> QTableWidget:
    """Вид таблицы результатов: без номеров строк и вертикальных линий — строки разделяет тонкая черта.
    Таблицы для ввода (журнал, партии, погода) остаются с номерами строк и сеткой, как в Excel."""
    t.verticalHeader().setVisible(False)
    t.setShowGrid(False)
    t.setItemDelegate(_RowLine(t))
    t.setProperty("clean", True)
    return t


def table(headers: List[str], stretch_last: bool = True) -> QTableWidget:
    """Таблица результатов (clean_table)."""
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(stretch_last)
    return clean_table(t)


def set_role(w: QWidget, role: str) -> QWidget:
    """Роль виджета для общего QSS (design.stylesheet): muted, error, summary, num, link, primary, ghost, danger…
    Меняет вид сразу, без своего setStyleSheet — поэтому смена темы на ходу перекрашивает и эти надписи."""
    if w.property("role") != role:
        w.setProperty("role", role)
        if w.styleSheet():
            w.setStyleSheet("")
        st = w.style()
        st.unpolish(w)
        st.polish(w)
        w.update()
    return w


def menu_button(text: str, items, tip: str = ""):
    """Кнопка с меню для редких действий (отчёты, импорт): items — [(подпись, функция)].
    Возвращает (кнопка, {подпись: QAction})."""
    from PySide6.QtWidgets import QMenu, QPushButton
    b = QPushButton(text)
    b.setProperty("menu", True)
    m = QMenu(b)
    acts = {}
    for cap, slot in items:
        act = m.addAction(cap)
        act.triggered.connect(lambda _c=False, f=slot: f())
        acts[cap] = act
    b.setMenu(m)
    if tip:
        b.setToolTip(tip)
    return b, acts


def with_legend(plot, items=()) -> QWidget:
    """График с подписями серий над ним (widgets.Legend): цвет — значок, текст — обычным цветом. Вместо слов
    «красная — цель» в заголовке графика."""
    from PySide6.QtWidgets import QVBoxLayout
    from .widgets import Legend
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(4)
    lg = Legend(items)
    v.addWidget(lg)
    v.addWidget(plot, 1)
    plot.legend_widget = lg
    return w


def mute(w: QWidget) -> QWidget:
    """Приглушённая подсказка (цвет из темы)."""
    return set_role(w, "muted")


def note_label(text: str = "") -> QLabel:
    lb = QLabel(text)
    lb.setWordWrap(True)
    lb.setProperty("role", "muted")
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
    """«› Заголовок» — по щелчку раскрывает содержимое (пояснение или виджет). Длинные пояснения на страницах
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
        self.button.setAutoRaise(True)
        self.button.setCursor(Qt.PointingHandCursor)
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
        self._chevron()

    def _chevron(self) -> None:
        from . import design, icons
        t = design.tokens_for(self)
        sz = max(int(self.fontMetrics().height() * 0.9), 10)
        self.button.setIcon(icons.icon("chev_down" if self.button.isChecked() else "chev_right", t.ink2, sz))
        self.button.setIconSize(QSize(sz, sz))

    def changeEvent(self, ev) -> None:  # noqa: N802 — тема или шрифт сменились: перекрасить стрелку
        super().changeEvent(ev)
        if ev.type() in (QEvent.PaletteChange, QEvent.FontChange) and hasattr(self, "button"):
            self._chevron()

    def _toggled(self, on: bool) -> None:
        self._chevron()
        self.content.setVisible(on)

    def set_expanded(self, on: bool) -> None:
        self.button.setChecked(on)

    def is_expanded(self) -> bool:
        return self.button.isChecked()


class Tile(QFrame):
    """Плитка «подпись / крупное число / пояснение» для главных цифр страницы. Справа от числа можно поставить
    маленький график (set_aside), под числом — полосу прогресса (set_below). Вид — из общего QSS (QFrame#tile)."""

    def __init__(self, title: str, parent: Optional[QWidget] = None):
        from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout
        super().__init__(parent)
        self.setObjectName("tile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(1)
        self.lbl_title = QLabel(title)
        self.lbl_title.setProperty("role", "tileTitle")
        self.lbl_value = QLabel("—")
        self.lbl_value.setProperty("role", "tileValue")
        self.lbl_sub = QLabel("")
        self.lbl_sub.setProperty("role", "tileSub")
        self.lbl_sub.setWordWrap(True)
        self.lbl_title.setWordWrap(True)
        self.setMinimumWidth(10)
        lay.addWidget(self.lbl_title)
        self.value_row = QHBoxLayout()
        self.value_row.setSpacing(6)
        self.value_row.addWidget(self.lbl_value, 0, Qt.AlignBottom)
        self.value_row.addStretch(1)
        lay.addLayout(self.value_row)
        self.below = QVBoxLayout()
        self.below.setContentsMargins(0, 2, 0, 2)
        lay.addLayout(self.below)
        lay.addWidget(self.lbl_sub)
        lay.addStretch(1)

    def set_aside(self, w: QWidget) -> None:
        self.value_row.addWidget(w, 0, Qt.AlignBottom)

    def set_below(self, w: QWidget) -> None:
        self.below.addWidget(w)

    def set(self, value: str, sub: str = "", tip: str = "") -> None:
        self.lbl_value.setText(value)
        self.lbl_sub.setText(sub)
        self.lbl_sub.setVisible(bool(sub))
        self.setToolTip(tip)


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
