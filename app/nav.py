"""Боковое меню разделов (как «Параметры» Windows 10) и область страницы с заголовком.

Слева — название программы, разделы по смыслу работы («Смена», «Планирование», «Модель и данные», «Справка»),
внизу — переключатель режима и «Замечание…». Справа — заголовок страницы, одна строка о её назначении и сама
страница. В отличие от строки вкладок всё помещается на экран 1366 px, а сушильщик видит только свой раздел.

Интерфейс повторяет QTabWidget в той части, которой пользуется окно (addTab, count, tabText, widget,
currentIndex, setCurrentIndex, currentWidget, indexOf, isTabVisible, setTabVisible, currentChanged), поэтому
вкладки-страницы не знают, чем их показывают. Ctrl+Tab / Ctrl+Shift+Tab — следующий / предыдущий раздел.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QBrush, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QSizePolicy,
                               QStackedWidget, QVBoxLayout, QWidget)

from .theme import Theme, mix, theme_for

PAGE_ROLE = Qt.UserRole + 1          # номер страницы у пункта меню (у заголовка раздела — -1)


class NavPanel(QWidget):
    currentChanged = Signal(int)

    def __init__(self, app_title: str = "DryerCalc", app_subtitle: str = ""):
        super().__init__()
        self._pages: List[Dict] = []
        self._headers: Dict[str, QListWidgetItem] = {}
        self._cur = -1
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.side = QFrame()
        self.side.setObjectName("navSide")
        sv = QVBoxLayout(self.side)
        sv.setContentsMargins(10, 12, 10, 10)
        sv.setSpacing(6)
        self.lbl_app = QLabel(app_title)
        self.lbl_app.setObjectName("navApp")
        self.lbl_app_sub = QLabel(app_subtitle)
        self.lbl_app_sub.setObjectName("navAppSub")
        self.lbl_app_sub.setWordWrap(True)
        sv.addWidget(self.lbl_app)
        if app_subtitle:
            sv.addWidget(self.lbl_app_sub)
        sv.addSpacing(6)
        self.list = QListWidget()
        self.list.setObjectName("navList")
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setFocusPolicy(Qt.StrongFocus)
        self.list.currentItemChanged.connect(self._item_changed)
        sv.addWidget(self.list, 1)
        self.bottom = QVBoxLayout()
        self.bottom.setSpacing(6)
        sv.addLayout(self.bottom)
        lay.addWidget(self.side, 0)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(14, 10, 10, 4)
        rv.setSpacing(2)
        self.lbl_title = QLabel()
        self.lbl_title.setObjectName("navTitle")
        self.lbl_sub = QLabel()
        self.lbl_sub.setObjectName("navSub")
        self.lbl_sub.setWordWrap(True)
        rv.addWidget(self.lbl_title)
        rv.addWidget(self.lbl_sub)
        rv.addSpacing(4)
        self.stack = QStackedWidget()
        rv.addWidget(self.stack, 1)
        lay.addWidget(right, 1)

        for seq, step in ((QKeySequence("Ctrl+Tab"), 1), (QKeySequence("Ctrl+Shift+Tab"), -1),
                          (QKeySequence("Ctrl+PgDown"), 1), (QKeySequence("Ctrl+PgUp"), -1)):
            sc = QShortcut(seq, self)
            sc.setContext(Qt.WindowShortcut)
            sc.activated.connect(lambda s=step: self._step(s))
        self.apply_theme()

    # --- как у QTabWidget -------------------------------------------------------------------------------------
    def addTab(self, widget: QWidget, text: str, section: Optional[str] = None, subtitle: str = "",  # noqa: N802
               tip: str = "") -> int:
        if section and section not in self._headers:
            hdr = QListWidgetItem(section.upper())
            hdr.setFlags(Qt.ItemIsEnabled)
            hdr.setData(PAGE_ROLE, -1)
            self.list.addItem(hdr)
            self._headers[section] = hdr
        elif not section and self._pages:
            sp = QListWidgetItem("")                  # отступ перед страницей без раздела («Справка»)
            sp.setFlags(Qt.NoItemFlags)
            sp.setData(PAGE_ROLE, -1)
            sp.setSizeHint(QSize(10, 10))
            self.list.addItem(sp)
        idx = len(self._pages)
        it = QListWidgetItem(text)
        it.setData(PAGE_ROLE, idx)
        if tip or subtitle:
            it.setToolTip(tip or subtitle)
        self.list.addItem(it)
        self.stack.addWidget(widget)
        self._pages.append({"w": widget, "text": text, "section": section, "sub": subtitle, "item": it,
                            "visible": True})
        if self._cur < 0:
            self.setCurrentIndex(0)
        return idx

    def count(self) -> int:
        return len(self._pages)

    def tabText(self, i: int) -> str:  # noqa: N802
        return self._pages[i]["text"] if 0 <= i < len(self._pages) else ""

    def widget(self, i: int) -> Optional[QWidget]:
        return self._pages[i]["w"] if 0 <= i < len(self._pages) else None

    def indexOf(self, w: QWidget) -> int:  # noqa: N802
        return next((i for i, pg in enumerate(self._pages) if pg["w"] is w), -1)

    def indexOfText(self, text: str) -> int:  # noqa: N802
        return next((i for i, pg in enumerate(self._pages) if pg["text"] == text), -1)

    def currentIndex(self) -> int:  # noqa: N802
        return self._cur

    def currentWidget(self) -> Optional[QWidget]:  # noqa: N802
        return self.widget(self._cur)

    def setCurrentWidget(self, w: QWidget) -> None:  # noqa: N802
        i = self.indexOf(w)
        if i >= 0:
            self.setCurrentIndex(i)

    def setCurrentIndex(self, i: int) -> None:  # noqa: N802
        if not 0 <= i < len(self._pages):
            return
        pg = self._pages[i]
        # только текущая страница задаёт минимальный размер окна: скрытые страницы со своими (иногда устаревшими после
        # смены шрифта) размерами не должны раздувать окно шире экрана
        for k, other in enumerate(self._pages):
            pol = QSizePolicy.Preferred if k == i else QSizePolicy.Ignored
            other["w"].setSizePolicy(pol, pol)
        self.stack.setCurrentWidget(pg["w"])
        self.lbl_title.setText(pg["text"])
        self.lbl_sub.setText(pg["sub"])
        self.lbl_sub.setVisible(bool(pg["sub"]))
        if self.list.currentItem() is not pg["item"]:
            self.list.blockSignals(True)
            self.list.setCurrentItem(pg["item"])
            self.list.blockSignals(False)
        if i != self._cur:
            self._cur = i
            self.currentChanged.emit(i)

    def isTabVisible(self, i: int) -> bool:  # noqa: N802
        return bool(self._pages[i]["visible"]) if 0 <= i < len(self._pages) else False

    def setTabVisible(self, i: int, vis: bool) -> None:  # noqa: N802
        if not 0 <= i < len(self._pages):
            return
        self._pages[i]["visible"] = vis
        self._pages[i]["item"].setHidden(not vis)
        for sec, hdr in self._headers.items():     # заголовок раздела — если в нём есть видимые страницы
            hdr.setHidden(not any(pg["visible"] for pg in self._pages if pg["section"] == sec))
        if not vis and i == self._cur:
            j = next((k for k in range(len(self._pages)) if self._pages[k]["visible"]), None)
            if j is not None:
                self.setCurrentIndex(j)

    def addBottomWidget(self, w: QWidget) -> None:  # noqa: N802
        self.bottom.addWidget(w)

    # --- внутреннее -------------------------------------------------------------------------------------------
    def _item_changed(self, cur: Optional[QListWidgetItem], prev: Optional[QListWidgetItem]) -> None:
        if cur is None:
            return
        i = cur.data(PAGE_ROLE)
        if i is None or i < 0:                          # заголовок раздела выбрать нельзя — вернуть выбор
            self.list.blockSignals(True)
            self.list.setCurrentItem(prev if prev is not None else self._pages[self._cur]["item"])
            self.list.blockSignals(False)
            return
        self.setCurrentIndex(int(i))

    def _step(self, step: int) -> None:
        vis = [k for k in range(len(self._pages)) if self._pages[k]["visible"]]
        if not vis:
            return
        pos = vis.index(self._cur) if self._cur in vis else 0
        self.setCurrentIndex(vis[(pos + step) % len(vis)])

    def apply_theme(self, th: Optional[Theme] = None) -> None:
        """Цвета и размеры шрифтов от текущей темы и базового шрифта (в режиме сушильщика он крупнее)."""
        th = th or theme_for(self)
        pt = QApplication.font().pointSizeF()
        side_bg = mix(th.window, th.text, 0.06 if th.dark else 0.035).name()
        hover = mix(th.window, th.text, 0.12 if th.dark else 0.08).name()
        sel_bg, sel_fg = th.highlight
        accent = "#4ea1ff" if th.dark else "#0067c0"
        line = mix(th.window, th.text, 0.18).name()
        self.side.setStyleSheet(
            f"#navSide {{ background:{side_bg}; border-right:1px solid {line}; }}"
            f"#navApp {{ font-size:{pt + 5:.1f}pt; font-weight:600; }}"
            f"#navAppSub {{ color:{th.muted}; }}"
            f"#navList {{ background:transparent; outline:0; }}"
            f"#navList::item {{ padding:7px 10px; border-left:3px solid transparent; border-radius:3px; }}"
            f"#navList::item:hover {{ background:{hover}; }}"
            f"#navList::item:selected {{ background:{sel_bg}; color:{sel_fg}; border-left:3px solid {accent}; }}")
        self.lbl_title.setStyleSheet(f"font-size:{pt + 6:.1f}pt; font-weight:600;")
        self.lbl_sub.setStyleSheet(f"color:{th.muted};")
        f = QFont(QApplication.font())
        f.setPointSizeF(max(pt - 1.0, 7.0))
        f.setBold(True)
        for hdr in self._headers.values():
            hdr.setFont(f)
            hdr.setForeground(QBrush(mix(th.text, th.window, 0.45)))
        fm = self.list.fontMetrics()
        width = max(fm.horizontalAdvance(pg["text"]) for pg in self._pages) if self._pages else 150
        self.side.setFixedWidth(int(width + 70))
