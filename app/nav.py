"""Боковое меню разделов (как «Параметры» Windows 11) и область страницы с заголовком.

Слева — стеклянная панель (widgets.Glass) над тёплым фоном окна: знак и название программы, разделы по смыслу
работы («Смена», «Планирование», «Модель и данные», «Справка») с иконками, внизу — переключатель режима, «Замечание…»
и оформление. Выбранный раздел — золотистая «пилюля» с короткой золотой чертой слева. Справа — заголовок страницы,
одна строка о её назначении и сама страница. Всё помещается на экран 1366 px, а сушильщик видит только свой раздел.

Интерфейс повторяет QTabWidget в той части, которой пользуется окно (addTab, count, tabText, widget,
currentIndex, setCurrentIndex, currentWidget, indexOf, isTabVisible, setTabVisible, currentChanged), поэтому
вкладки-страницы не знают, чем их показывают. Ctrl+Tab / Ctrl+Shift+Tab — следующий / предыдущий раздел.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import QModelIndex, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QSizePolicy,
                               QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QVBoxLayout, QWidget)

from . import design, icons
from .theme import Theme
from .widgets import Glass

PAGE_ROLE = Qt.UserRole + 1          # номер страницы у пункта меню (у заголовка раздела и отступа — -1)
ICON_ROLE = Qt.UserRole + 2          # имя иконки (icons.py)


class _NavDelegate(QStyledItemDelegate):
    """Рисует пункты меню целиком: заголовки разделов, отступ, страницы (иконка + название), выбранную «пилюлю»."""

    def _fonts(self, base: QFont):
        hdr = QFont(base)
        hdr.setPointSizeF(max(base.pointSizeF() - 0.5, 7.0))
        hdr.setWeight(QFont.DemiBold)
        return hdr

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        fm = QFontMetrics(option.font)
        page = index.data(PAGE_ROLE)
        text = index.data(Qt.DisplayRole) or ""
        if page is None or page < 0:
            if not text:
                return QSize(10, max(6, fm.height() // 2))
            return QSize(10, int(QFontMetrics(self._fonts(option.font)).height() + fm.height() * 0.9))
        return QSize(10, int(fm.height() * 2.2))

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        t = design.tokens_for(option.widget) if option.widget is not None else design.tokens()
        page = index.data(PAGE_ROLE)
        text = index.data(Qt.DisplayRole) or ""
        r = QRectF(option.rect)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        fm = QFontMetrics(option.font)
        pad = fm.height() * 0.6
        if page is None or page < 0:
            if text:
                f = self._fonts(option.font)
                p.setFont(f)
                p.setPen(QColor(t.muted))
                p.drawText(r.adjusted(pad, 0, -pad, -fm.height() * 0.25), Qt.AlignLeft | Qt.AlignBottom, text)
            p.restore()
            return
        sel = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver)
        pill = r.adjusted(1.5, 1.5, -1.5, -1.5)
        if sel:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(t.accent_tint))
            p.drawRoundedRect(pill, 8, 8)
            bar_h = pill.height() * 0.42
            p.setBrush(QColor(t.accent_edge if not t.dark else t.accent))
            p.drawRoundedRect(QRectF(pill.left() + 1.5, pill.center().y() - bar_h / 2, 3.0, bar_h), 1.5, 1.5)
        elif hover:
            hv = QColor(t.ink)
            hv.setAlphaF(0.07 if not t.dark else 0.09)
            p.setPen(Qt.NoPen)
            p.setBrush(hv)
            p.drawRoundedRect(pill, 8, 8)
        if (option.state & QStyle.State_HasFocus) and option.widget is not None and option.widget.hasFocus():
            p.setPen(QColor(t.steel))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(pill.adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        isz = int(round(fm.height() * 1.15))
        name = index.data(ICON_ROLE)
        x = pill.left() + pad + 2
        if name:
            col = (t.accent_deep if not t.dark else t.accent) if sel else t.ink2
            pm = icons.pixmap(name, col, isz, p.device().devicePixelRatioF() if p.device() else 1.0)
            p.drawPixmap(int(x), int(pill.center().y() - isz / 2), pm)
            x += isz + pad * 0.9
        p.setFont(option.font)
        p.setPen(QColor(t.ink))
        p.drawText(QRectF(x, pill.top(), pill.right() - x - 4, pill.height()), Qt.AlignVCenter | Qt.AlignLeft,
                   fm.elidedText(text, Qt.ElideRight, int(pill.right() - x - 4)))
        p.restore()


def _logo(size: int, dpr: float):
    """Знак программы — тот же, что у ярлыка и в панели задач (assets/dryer.png), уменьшенный под меню."""
    from PySide6.QtGui import QPixmap
    from .resources import resource_path
    src = resource_path("assets/dryer.png")
    if not src.exists():
        return None
    pm = QPixmap(str(src))
    if pm.isNull():
        return None
    pm = pm.scaled(int(size * dpr), int(size * dpr), Qt.KeepAspectRatio, Qt.SmoothTransformation)
    pm.setDevicePixelRatio(dpr)
    return pm


class _Root(QWidget):
    """Фон под стеклом: тёплый градиент со свечением (widgets.paint_backdrop). В окне программы фон рисует само
    окно (на всю высоту, со строкой состояния) — тогда backdrop = False."""

    backdrop = True

    def paintEvent(self, _e) -> None:  # noqa: N802
        if not self.backdrop:
            return
        from .widgets import paint_backdrop
        p = QPainter(self)
        paint_backdrop(p, QRectF(self.rect()), design.tokens_for(self))
        p.end()


class NavPanel(_Root):
    currentChanged = Signal(int)

    def __init__(self, app_title: str = "DryerCalc", app_subtitle: str = ""):
        super().__init__()
        self._pages: List[Dict] = []
        self._headers: Dict[str, QListWidgetItem] = {}
        self._cur = -1
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.side = Glass(radius=16, blur=12, dy=3)
        self.side.setObjectName("navSide")
        sv = QVBoxLayout(self.side)
        sv.setContentsMargins(10, 14, 10, 10)
        sv.setSpacing(4)
        brand = QHBoxLayout()
        brand.setSpacing(8)
        self.lbl_logo = QLabel()
        self.lbl_logo.setObjectName("navLogo")
        self.lbl_app = QLabel(app_title)
        self.lbl_app.setObjectName("navApp")
        brand.addWidget(self.lbl_logo, 0, Qt.AlignVCenter)
        brand.addWidget(self.lbl_app, 1, Qt.AlignVCenter)
        sv.addLayout(brand)
        self.lbl_app_sub = QLabel(app_subtitle)
        self.lbl_app_sub.setObjectName("navAppSub")
        self.lbl_app_sub.setWordWrap(True)
        if app_subtitle:
            sv.addWidget(self.lbl_app_sub)
        sv.addSpacing(8)
        self.list = QListWidget()
        self.list.setObjectName("navList")
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setFocusPolicy(Qt.StrongFocus)
        self.list.setMouseTracking(True)
        self.list.viewport().setAutoFillBackground(False)
        self.list.setItemDelegate(_NavDelegate(self.list))
        self.list.currentItemChanged.connect(self._item_changed)
        sv.addWidget(self.list, 1)
        self.bottom = QVBoxLayout()
        self.bottom.setSpacing(6)
        sv.addLayout(self.bottom)
        lay.addWidget(self.side, 0)

        right = QWidget()
        right.setObjectName("navPage")
        rv = QVBoxLayout(right)
        rv.setContentsMargins(10, 16, 16, 4)
        rv.setSpacing(2)
        self.lbl_title = QLabel()
        self.lbl_title.setObjectName("navTitle")
        self.lbl_sub = QLabel()
        self.lbl_sub.setObjectName("navSub")
        self.lbl_sub.setWordWrap(True)
        rv.addWidget(self.lbl_title)
        rv.addWidget(self.lbl_sub)
        rv.addSpacing(8)
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
               tip: str = "", icon: Optional[str] = None) -> int:
        if section and section not in self._headers:
            hdr = QListWidgetItem(section)
            hdr.setFlags(Qt.NoItemFlags)
            hdr.setData(PAGE_ROLE, -1)
            self.list.addItem(hdr)
            self._headers[section] = hdr
        elif not section and self._pages:
            sp = QListWidgetItem("")                  # отступ перед страницей без раздела («Справка»)
            sp.setFlags(Qt.NoItemFlags)
            sp.setData(PAGE_ROLE, -1)
            self.list.addItem(sp)
        idx = len(self._pages)
        it = QListWidgetItem(text)
        it.setData(PAGE_ROLE, idx)
        if icon:
            it.setData(ICON_ROLE, icon)
        if tip or subtitle:
            it.setToolTip(tip or subtitle)
        self.list.addItem(it)
        self.stack.addWidget(widget)
        self._pages.append({"w": widget, "text": text, "section": section, "sub": subtitle, "item": it,
                            "visible": True})
        if self._cur < 0:
            self.setCurrentIndex(0)
        self._fit_width()
        return idx

    def setTabLabel(self, i: int, label: str) -> None:  # noqa: N802
        """Подпись пункта в меню (например, «Культура: рапс»); название страницы и tabText не меняются."""
        if 0 <= i < len(self._pages):
            self._pages[i]["label"] = label
            self._pages[i]["item"].setText(label)
            self._fit_width()

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

    def addBottomLayout(self, lay) -> None:  # noqa: N802
        self.bottom.addLayout(lay)

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

    def _fit_width(self) -> None:
        """Ширина меню — по самому длинному названию раздела (+ иконка и поля) и по нижним кнопкам."""
        fm = QFontMetrics(self.list.font())
        text_w = max((fm.horizontalAdvance(pg.get("label") or pg["text"]) for pg in self._pages), default=150)
        inner = text_w + fm.height() * 1.15 + fm.height() * 2.2 + 12
        bottom = self.bottom.minimumSize().width() if self.bottom.count() else 0
        l, _t, r, _b = self.side.shadow_margins()
        m = self.side.layout().contentsMargins()
        self.side.setFixedWidth(int(max(inner, bottom, 150) + m.left() + m.right() + l + r))

    def apply_theme(self, th: Optional[Theme] = None) -> None:  # noqa: ARG002 — цвета берутся из design
        """Знак программы под тему, ширина меню — под размер шрифта (в режиме сушильщика он крупнее)."""
        t = design.tokens_for(self)
        fm = QFontMetrics(QApplication.font())
        sz = int(round(fm.height() * 1.55))
        dpr = max(self.devicePixelRatioF() if self.isVisible() else 2.0, 1.0)
        self.lbl_logo.setPixmap(_logo(sz, dpr) or icons.pixmap("kernel", t.accent, sz, dpr))
        self.lbl_logo.setFixedSize(sz, sz)
        self.list.doItemsLayout()
        self._fit_width()
        self.list.viewport().update()
        self.update()
