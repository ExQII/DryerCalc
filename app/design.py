"""Оформление DryerCalc: «пульт сушилки в тёплом свете уборочной».

Тёплый минимализм — основа: соломенно-каменный фон, тёплый графит текста, спокойные сплошные карточки, на которых
хорошо читаются цифры. «Стекло» — только у плавающих слоёв (боковое меню и главный совет «Текущего прохода»),
см. widgets.Glass. Цвета взяты из предмета:
  * золото — действие и выбранное (главная кнопка, выбранный раздел, советуемое D) и больше нигде;
  * сталь силоса — измеренные данные (выходной влагомер), фокус, ссылки;
  * зелень поля — «всё в порядке, D не менять»; ржавчина — предупреждения.
Шрифты: интерфейс — Segoe UI (родной для Windows 10); заголовки и крупные цифры — Bahnschrift (встроен в Windows
10 с 1709: DIN — шрифт промышленных табличек и приборов). Если их нет (Linux, Wine) — ближайшая замена.

Скругления — по иерархии: главный совет 18, стеклянное меню 16, плитки 14, карточки 12, таблицы 10, поля и кнопки 8,
вкладки-сегменты — «пилюли». Тени — только у стекла.

apply(app, mode) ставит стиль Fusion, палитру, шрифт и общий QSS; tokens() — текущие цвета для рисования.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, fields
from typing import Dict, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication

MODES = ("system", "light", "dark")
MODE_NAMES = {"system": "Как в Windows", "light": "Светлое", "dark": "Тёмное (ночная смена)"}


@dataclass(frozen=True)
class Tokens:
    dark: bool
    paper: str          # фон окна (верх градиента)
    paper2: str         # фон окна (низ градиента)
    surface: str        # карточки, таблицы
    surface2: str       # второстепенные поверхности: кнопки, заголовки таблиц, отключённое
    field: str          # поля ввода
    line: str
    line_soft: str
    line_strong: str
    ink: str            # основной текст
    ink2: str           # второстепенный текст, подписи групп
    muted: str          # подсказки
    accent: str         # золото: заливка главной кнопки, флажков
    accent_hover: str
    accent_edge: str
    accent_ink: str     # текст на золоте
    accent_deep: str    # золото для тонких линий и текста на светлом
    accent_tint: str    # выбранное: пункт меню, строка таблицы, вкладка
    steel: str          # данные, фокус, ссылки
    steel_tint: str     # сводки
    good: str
    good_tint: str
    good_ink: str
    rust: str
    rust_tint: str
    rust_ink: str
    amber_ink: str      # текст на золотистой плашке
    hover: str
    pressed: str
    scroll: str
    scroll_hover: str
    glass: str          # rgba стекла
    glass_hi: str       # верхний блик кромки стекла
    glass_lo: str       # нижняя кромка
    shadow: str         # тень стекла
    glow: str           # тёплое свечение фона за стеклом
    # графики — проверены на различимость (в т. ч. при дальтонизме): синий, ягодный, золотой
    series1: str
    series2: str
    target: str
    ref: str


LIGHT = Tokens(
    dark=False, paper="#EEEBE4", paper2="#E6E1D7", surface="#FBFAF7", surface2="#F3F0E9", field="#FFFFFF",
    line="#DDD6C9", line_soft="#ECE7DE", line_strong="#C6BEAF",
    ink="#23201B", ink2="#4D4840", muted="#6B645A",
    accent="#E8A317", accent_hover="#F0B231", accent_edge="#C98A0A", accent_ink="#2B2005", accent_deep="#8F6300",
    accent_tint="#F6E5BB",
    steel="#2F6DB0", steel_tint="#E4ECF5",
    good="#3D7A39", good_tint="#E1EEDB", good_ink="#1F4A1C",
    rust="#B23A1A", rust_tint="#F8E1D8", rust_ink="#7A2410", amber_ink="#5A4100",
    hover="#EFEBE3", pressed="#E5DFD3", scroll="#CEC6B8", scroll_hover="#B5AC9C",
    glass="rgba(255, 255, 255, 150)", glass_hi="rgba(255, 255, 255, 240)", glass_lo="rgba(35, 32, 27, 30)",
    shadow="rgba(70, 52, 20, 34)", glow="rgba(232, 163, 23, 26)",
    series1="#2F6DB0", series2="#B3417A", target="#A97200", ref="#8E877C",
)

DARK = Tokens(
    dark=True, paper="#171512", paper2="#1F1B17", surface="#221F1B", surface2="#2A2621", field="#2C2823",
    line="#3A352E", line_soft="#2F2B26", line_strong="#4C463D",
    ink="#F2EDE4", ink2="#D3CBBE", muted="#A79E90",
    accent="#F2B33D", accent_hover="#F6C25E", accent_edge="#D99A1E", accent_ink="#1E1606", accent_deep="#F2B33D",
    accent_tint="#4A3A16",
    steel="#6FA6E6", steel_tint="#1F2C3B",
    good="#8CC47C", good_tint="#1E3320", good_ink="#B9E3AB",
    rust="#FF8F6B", rust_tint="#3D1F17", rust_ink="#FFC2AE", amber_ink="#FFE1A0",
    hover="#2E2A24", pressed="#37322B", scroll="#4A443B", scroll_hover="#5C554B",
    glass="rgba(255, 248, 236, 16)", glass_hi="rgba(255, 255, 255, 38)", glass_lo="rgba(0, 0, 0, 90)",
    shadow="rgba(0, 0, 0, 110)", glow="rgba(242, 179, 61, 20)",
    series1="#5592DC", series2="#D35C90", target="#BD8820", ref="#8A8276",
)

_state: Dict = {"tokens": None, "mode": "system", "ui": None, "num": None, "num_weight": 600, "base_pt": None,
                "cache": None}


def qcolor(spec: str) -> QColor:
    """'#rrggbb' или 'rgba(r, g, b, a)' (a — 0…255) → QColor."""
    s = spec.strip()
    if s.startswith("rgba"):
        r, g, b, a = (int(float(x)) for x in s[s.index("(") + 1:s.index(")")].split(","))
        return QColor(r, g, b, a)
    return QColor(s)


def tokens() -> Tokens:
    return _state["tokens"] or LIGHT


def window_palette(widget=None) -> QPalette:
    """Палитра окна, в котором виджет. Не самого виджета: у виджета с прозрачным фоном в QSS (меню, надписи) цвет
    фона в палитре — прозрачный (светлота 0), и по нему тема ошибочно выглядела бы тёмной."""
    app = QApplication.instance()
    try:
        if widget is not None:
            top = widget.window()
            pal = top.palette() if top is not None else widget.palette()
            if pal.color(QPalette.Window).alpha() == 255:
                return pal
    except Exception:  # noqa: BLE001
        pass
    return app.palette() if app is not None else QPalette()


def tokens_for(widget) -> Tokens:
    """Токены по палитре окна виджета (тёмный фон окна — тёмные токены): так рисуют свои виджеты."""
    return DARK if window_palette(widget).color(QPalette.Window).lightness() < 128 else LIGHT


def tint_color(t: Tokens, kind: Optional[str]) -> Optional[str]:
    """Цвет смысла: gold — действие, green — «всё в порядке», steel — сведения, rust — предупреждение."""
    return {"gold": t.accent, "green": t.good, "steel": t.steel, "rust": t.rust}.get(kind or "")


def mode() -> str:
    return _state["mode"]


def system_dark() -> bool:
    """Тёмная тема Windows (Параметры → Персонализация → Цвета). Без Qt 6.5+ — по палитре."""
    app = QGuiApplication.instance()
    try:
        return app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:
        return app.palette().color(QPalette.Window).lightness() < 128 if app else False


def _pick(families, wanted):
    have = {f.lower(): f for f in families}
    for w in wanted:
        if w.lower() in have:
            return have[w.lower()]
    return None


def fonts() -> Dict:
    """Шрифты: ui — интерфейс, num — заголовки и цифры (+ его толщина)."""
    if _state["ui"] is None:
        fams = QFontDatabase.families()
        _state["ui"] = _pick(fams, ["Segoe UI", "Inter", "Noto Sans", "DejaVu Sans"]) or QApplication.font().family()
        semi = _pick(fams, ["Bahnschrift SemiBold"])
        if semi:
            _state["num"], _state["num_weight"] = semi, 400
        else:
            _state["num"] = _pick(fams, ["Bahnschrift", "Inter Display", "Segoe UI Semibold", "Segoe UI", "Inter",
                                         "DejaVu Sans"]) or _state["ui"]
            _state["num_weight"] = 400 if _state["num"].lower().endswith("semibold") else 600
    return {"ui": _state["ui"], "num": _state["num"], "num_weight": _state["num_weight"]}


def num_font(pt: float, bold: bool = True) -> QFont:
    """Шрифт для крупных цифр и заголовков (Bahnschrift на Windows)."""
    f = fonts()
    font = QFont(f["num"])
    font.setPointSizeF(pt)
    if bold and f["num_weight"] >= 600:
        font.setWeight(QFont.DemiBold)
    return font


# ----------------------------------------------------------------------------------------------------------------
# Картинки для QSS (стрелки полей, галочка флажка): рисуются под цвет темы, кладутся во временную папку
# ----------------------------------------------------------------------------------------------------------------

def _cache_dir() -> str:
    if _state["cache"] is None:
        d = os.path.join(tempfile.gettempdir(), "DryerCalc_ui")
        os.makedirs(d, exist_ok=True)
        _state["cache"] = d
    return _state["cache"]


def _draw_png(name: str, color: str, painter_fn, size: int = 24) -> str:
    path = os.path.join(_cache_dir(), f"{name}_{color.strip('#').replace(' ', '').replace(',', '_')}.png")
    if not os.path.exists(path):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(qcolor(color), size / 9.0)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        painter_fn(p, size)
        p.end()
        pm.save(path, "PNG")
    return path.replace("\\", "/")


def _chev_down(p: QPainter, s: int) -> None:
    path = QPainterPath(QPointF(s * 0.25, s * 0.38))
    path.lineTo(s * 0.5, s * 0.63)
    path.lineTo(s * 0.75, s * 0.38)
    p.drawPath(path)


def _chev_up(p: QPainter, s: int) -> None:
    path = QPainterPath(QPointF(s * 0.25, s * 0.62))
    path.lineTo(s * 0.5, s * 0.37)
    path.lineTo(s * 0.75, s * 0.62)
    p.drawPath(path)


def _check(p: QPainter, s: int) -> None:
    pen = p.pen()
    pen.setWidthF(s / 7.0)
    p.setPen(pen)
    path = QPainterPath(QPointF(s * 0.24, s * 0.52))
    path.lineTo(s * 0.43, s * 0.70)
    path.lineTo(s * 0.77, s * 0.32)
    p.drawPath(path)


def _dot(p: QPainter, s: int) -> None:
    p.setBrush(p.pen().color())
    p.drawEllipse(QRectF(s * 0.32, s * 0.32, s * 0.36, s * 0.36))


# ----------------------------------------------------------------------------------------------------------------
# Палитра и общий QSS
# ----------------------------------------------------------------------------------------------------------------

def palette(t: Tokens) -> QPalette:
    pal = QPalette()
    roles = {
        QPalette.Window: t.paper, QPalette.WindowText: t.ink, QPalette.Base: t.field, QPalette.AlternateBase: t.surface2,
        QPalette.Text: t.ink, QPalette.Button: t.surface, QPalette.ButtonText: t.ink, QPalette.Highlight: t.accent_tint,
        QPalette.HighlightedText: t.ink, QPalette.ToolTipBase: t.ink, QPalette.ToolTipText: t.paper,
        QPalette.PlaceholderText: t.muted, QPalette.Link: t.steel, QPalette.BrightText: t.rust,
        QPalette.Light: t.surface, QPalette.Midlight: t.line_soft, QPalette.Mid: t.line, QPalette.Dark: t.line_strong,
        QPalette.Shadow: t.line_strong,
    }
    for group in (QPalette.Active, QPalette.Inactive):
        for role, c in roles.items():
            pal.setColor(group, role, QColor(c))
    for role, c in roles.items():
        pal.setColor(QPalette.Disabled, role, QColor(c))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor(t.muted))
    pal.setColor(QPalette.Disabled, QPalette.Base, QColor(t.surface2))
    pal.setColor(QPalette.Disabled, QPalette.Button, QColor(t.surface2))
    return pal


def stylesheet(t: Tokens, base_pt: float) -> str:
    f = fonts()
    up = _draw_png("chev_up", t.ink2, _chev_up)
    down = _draw_png("chev_down", t.ink2, _chev_down)
    up_dis = _draw_png("chev_up", t.line_strong, _chev_up)
    down_dis = _draw_png("chev_down", t.line_strong, _chev_down)
    check = _draw_png("check", t.accent_ink, _check)
    radio = _draw_png("dot", t.accent_ink, _dot)
    num = f'font-family: "{f["num"]}"; font-weight: {f["num_weight"]};'
    return f"""
QWidget {{ color: {t.ink}; }}
QMainWindow, QDialog, QMessageBox {{ background: {t.paper}; }}
QLabel {{ background: transparent; }}
QToolTip {{ color: {t.paper}; background: {t.ink}; border: none; padding: 6px 8px; }}
QStatusBar {{ background: transparent; color: {t.muted}; }}
QStatusBar::item {{ border: none; }}
QStackedWidget, QSplitter, QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget, QScrollArea > QWidget > QWidget {{ background: transparent; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:hover {{ background: {t.line}; }}

/* карточки: подпись группы — над карточкой, обычным регистром */
QGroupBox {{ background: {t.surface}; border: 1px solid {t.line}; border-radius: 12px;
            margin-top: 1.55em; padding: 10px 10px 8px 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 4px; top: 0px;
                   padding: 0 2px; color: {t.ink2}; font-weight: 600; }}

/* кнопки: обычная — тихая, главная — золото, «призрачная» — без рамки, опасная — ржавый текст */
QPushButton {{ background: {t.surface}; color: {t.ink}; border: 1px solid {t.line}; border-radius: 8px;
              padding: 5px 12px; min-height: 18px; }}
QPushButton:hover {{ background: {t.hover}; border-color: {t.line_strong}; }}
QPushButton:pressed {{ background: {t.pressed}; }}
QPushButton:focus {{ border-color: {t.steel}; }}
QPushButton:disabled {{ color: {t.muted}; background: {t.surface2}; border-color: {t.line_soft}; }}
QPushButton[role="primary"] {{ background: {t.accent}; color: {t.accent_ink}; border: 1px solid {t.accent_edge};
                              font-weight: 600; }}
QPushButton[role="primary"]:hover {{ background: {t.accent_hover}; }}
QPushButton[role="primary"]:pressed {{ background: {t.accent_edge}; }}
QPushButton[role="primary"]:focus {{ border: 1px solid {t.steel}; }}
QPushButton[role="ghost"] {{ background: transparent; border: 1px solid transparent; color: {t.ink2}; }}
QPushButton[role="ghost"]:hover {{ background: {t.hover}; color: {t.ink}; }}
QPushButton[role="danger"] {{ color: {t.rust}; }}
QPushButton[menu="true"] {{ padding-right: 26px; }}
QPushButton::menu-indicator {{ image: url("{down}"); width: 10px; height: 10px; subcontrol-origin: padding;
    subcontrol-position: center right; right: 9px; }}
QAbstractScrollArea::corner {{ background: transparent; border: none; }}
QToolButton {{ background: transparent; border: none; color: {t.ink2}; font-weight: 600; padding: 3px 4px;
              border-radius: 6px; }}
QToolButton:hover {{ color: {t.ink}; background: {t.hover}; }}

/* поля ввода */
QLineEdit, QAbstractSpinBox, QComboBox {{ background: {t.field}; color: {t.ink}; border: 1px solid {t.line};
    border-radius: 8px; padding: 3px 8px; min-height: 20px; selection-background-color: {t.accent_tint};
    selection-color: {t.ink}; }}
QPlainTextEdit, QTextEdit, QTextBrowser {{ background: {t.field}; color: {t.ink}; border: 1px solid {t.line};
    border-radius: 10px; padding: 6px; selection-background-color: {t.accent_tint}; selection-color: {t.ink}; }}
QLineEdit:hover, QAbstractSpinBox:hover, QComboBox:hover {{ border-color: {t.line_strong}; }}
QLineEdit:focus, QAbstractSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border: 1px solid {t.steel}; }}
QLineEdit:disabled, QAbstractSpinBox:disabled, QComboBox:disabled {{ background: {t.surface2}; color: {t.muted};
    border-color: {t.line_soft}; }}
QAbstractSpinBox {{ padding-right: 20px; }}
QAbstractSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 18px;
    border: none; background: transparent; margin: 2px 3px 0 0; border-radius: 4px; }}
QAbstractSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: 18px;
    border: none; background: transparent; margin: 0 3px 2px 0; border-radius: 4px; }}
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{ background: {t.hover}; }}
QAbstractSpinBox::up-arrow {{ image: url("{up}"); width: 9px; height: 9px; }}
QAbstractSpinBox::down-arrow {{ image: url("{down}"); width: 9px; height: 9px; }}
QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::up-arrow:off {{ image: url("{up_dis}"); }}
QAbstractSpinBox::down-arrow:disabled, QAbstractSpinBox::down-arrow:off {{ image: url("{down_dis}"); }}
QComboBox {{ padding-right: 24px; }}
QComboBox::drop-down, QDateTimeEdit::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right;
    width: 22px; border: none; background: transparent; }}
QComboBox::down-arrow, QDateTimeEdit::down-arrow {{ image: url("{down}"); width: 10px; height: 10px; }}
QComboBox QAbstractItemView {{ background: {t.field}; border: 1px solid {t.line}; padding: 4px; outline: 0;
    selection-background-color: {t.accent_tint}; selection-color: {t.ink}; }}

/* флажки и переключатели: золото = выбрано */
QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px; border: 1px solid {t.line_strong};
    background: {t.field}; }}
QCheckBox::indicator {{ border-radius: 5px; }}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {t.steel}; }}
QCheckBox::indicator:checked {{ background: {t.accent}; border-color: {t.accent_edge}; image: url("{check}"); }}
QRadioButton::indicator:checked {{ background: {t.accent}; border-color: {t.accent_edge}; image: url("{radio}"); }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ background: {t.surface2};
    border-color: {t.line_soft}; }}

/* таблицы: без вертикальных линий, тихая шапка, выбранная строка — золотистая */
QTableView, QTableWidget, QListView, QTreeView {{ background: {t.surface}; alternate-background-color: {t.surface2};
    border: 1px solid {t.line}; border-radius: 10px; gridline-color: {t.line_soft};
    selection-background-color: {t.accent_tint}; selection-color: {t.ink}; outline: 0; }}
QHeaderView {{ background: transparent; border: none; }}
QHeaderView::section {{ background: {t.surface}; color: {t.ink2}; border: none; border-bottom: 1px solid {t.line};
    padding: 5px 6px; font-weight: 600; }}
QHeaderView::section:vertical {{ border-bottom: none; border-right: 1px solid {t.line_soft}; color: {t.muted};
    font-weight: 400; padding: 0 6px; }}
QTableCornerButton::section {{ background: {t.surface}; border: none; border-bottom: 1px solid {t.line}; }}


/* вложенные вкладки — сегменты-«пилюли» */
QTabWidget::pane {{ border: none; top: 2px; }}
QTabBar {{ background: transparent; qproperty-drawBase: 0; }}
QTabBar::tab {{ background: transparent; color: {t.ink2}; border: 1px solid {t.line}; padding: 5px 14px;
    margin: 0 6px 6px 0; border-radius: 14px; }}
QTabBar::tab:hover {{ background: {t.hover}; color: {t.ink}; }}
QTabBar::tab:selected {{ background: {t.accent_tint}; color: {t.ink}; border-color: {t.accent_edge}; }}

/* полосы прокрутки — тонкие */
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t.scroll}; border-radius: 3px; min-height: 28px; margin: 0 2px; }}
QScrollBar::handle:horizontal {{ background: {t.scroll}; border-radius: 3px; min-width: 28px; margin: 2px 0; }}
QScrollBar::handle:hover {{ background: {t.scroll_hover}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QMenu {{ background: {t.surface}; border: 1px solid {t.line}; padding: 4px; }}
QMenu::item {{ padding: 5px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {t.accent_tint}; color: {t.ink}; }}
QMenu::separator {{ height: 1px; background: {t.line}; margin: 4px 6px; }}

QCalendarWidget QWidget#qt_calendar_navigationbar {{ background: {t.surface2}; }}
QCalendarWidget QToolButton {{ color: {t.ink}; }}
QCalendarWidget QAbstractItemView {{ selection-background-color: {t.accent_tint}; selection-color: {t.ink}; }}

/* боковое меню: стекло рисует widgets.Glass, пункты — nav._NavDelegate */
QListWidget#navList {{ background: transparent; border: none; outline: 0; }}
QListWidget#navList::item {{ background: transparent; border: none; }}
QLabel#navApp {{ {num} font-size: {base_pt + 5:.1f}pt; color: {t.ink}; }}
QLabel#navAppSub {{ color: {t.muted}; }}
QLabel#navTitle {{ {num} font-size: {base_pt + 9:.1f}pt; color: {t.ink}; }}
QLabel#navSub {{ color: {t.muted}; }}
QToolButton#themeButton {{ padding: 4px; border-radius: 8px; }}
QToolButton#themeButton::menu-indicator {{ image: none; width: 0; }}

/* плитки и главный совет «Текущего прохода» */
QFrame#tile {{ background: {t.surface}; border: 1px solid {t.line}; border-radius: 14px; }}
QLabel[role="tileTitle"] {{ color: {t.ink2}; }}
QLabel[role="tileValue"] {{ {num} font-size: {base_pt + 7:.1f}pt; color: {t.ink}; }}
QLabel[role="tileSub"] {{ color: {t.muted}; }}
QLabel[role="tileAside"] {{ color: {t.ink2}; padding-bottom: 3px; }}
QLabel[role="gaugeCap"] {{ color: {t.ink2}; }}
QLabel[role="adviceHead"] {{ {num} font-size: {base_pt + 10:.1f}pt; color: {t.ink}; }}
QLabel[role="adviceSub"] {{ color: {t.ink2}; font-size: {base_pt + 0.5:.1f}pt; }}
QLabel[role="warn"] {{ color: {t.rust_ink if not t.dark else t.rust}; font-weight: 600; }}
QLabel[role="num"] {{ {num} }}
QLabel[role="muted"] {{ color: {t.muted}; }}
QLabel[role="error"] {{ color: {t.rust}; font-weight: 600; }}
QLabel[role="factKey"] {{ color: {t.muted}; }}
QLabel[role="factVal"] {{ color: {t.ink}; font-weight: 600; }}
QLabel[role="summary"] {{ background: {t.steel_tint}; color: {t.ink}; border-radius: 10px; padding: 10px 12px; }}
QPushButton[role="link"] {{ background: transparent; border: none; color: {t.steel}; padding: 2px 0;
                           text-align: left; }}
QPushButton[role="link"]:hover {{ color: {t.ink}; text-decoration: underline; }}

/* «Справка»: документ — на карточке */
QTextBrowser#doc {{ background: {t.surface}; border: 1px solid {t.line}; border-radius: 12px; padding: 8px 14px; }}

/* графики — на карточке */
SimplePlot {{ background: {t.surface}; border: 1px solid {t.line}; border-radius: 12px; padding: 6px; }}
"""


def style_name() -> str:
    """Имя стиля под QSS («fusion» после apply)."""
    return "fusion" if _state.get("fusion") else (QApplication.style().name() if QApplication.instance() else "")


def base_pt() -> Optional[float]:
    """Базовый размер шрифта последнего apply (в режиме сушильщика — увеличенный)."""
    return _state["base_pt"]


def _request_scheme(app: QApplication, mode_: str) -> None:
    """Попросить Windows рисовать рамку окна в той же теме (Qt 6.8+): тёмное оформление — тёмный заголовок окна.
    «Как в Windows» — снять просьбу, тогда colorScheme() снова сообщает тему системы."""
    try:
        want = {"light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}.get(mode_, Qt.ColorScheme.Unknown)
        hints = app.styleHints()
        if _state.get("requested") != want:
            hints.setColorScheme(want)
            _state["requested"] = want
    except (AttributeError, TypeError):
        pass


def apply(app: QApplication, mode_: str = "system", base_pt: Optional[float] = None) -> Tokens:
    """Поставить оформление: Fusion + палитра + шрифт + QSS. mode_: system / light / dark."""
    mode_ = mode_ if mode_ in MODES else "system"
    _request_scheme(app, mode_)
    dark = system_dark() if mode_ == "system" else mode_ == "dark"
    t = DARK if dark else LIGHT
    if base_pt is None:
        base_pt = _state["base_pt"] or app.font().pointSizeF()
    if not _state.get("fusion"):
        # стиль Windows рисует поля и вкладки средствами системы и тёмную палитру не понимает — Fusion рисует всё по
        # палитре и QSS. После setStyleSheet app.style() — обёртка QStyleSheetStyle с пустым name(), поэтому —
        # флаг, а не проверка имени
        app.setStyle("Fusion")
        _state["fusion"] = True
    f = fonts()
    font = QFont(f["ui"])
    font.setPointSizeF(base_pt)
    app.setFont(font)
    app.setPalette(palette(t))
    app.setStyleSheet(stylesheet(t, base_pt))
    _state.update(tokens=t, mode=mode_, base_pt=base_pt)
    return t


def token_names():
    return [f.name for f in fields(Tokens)]
