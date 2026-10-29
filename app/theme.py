"""Цвета интерфейса под светлую и тёмную системные темы.

Тема определяется по палитре окна (светлота фона < 50 % — тёмная), поэтому программа одинаково читается при любой
настройке Windows и перерисовывается при переключении темы на ходу.
"""
from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget


def mix(a: QColor, b: QColor, t: float) -> QColor:
    """Смесь цветов: t = 0 даёт a, t = 1 даёт b."""
    return QColor(int(a.red() + (b.red() - a.red()) * t), int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t))


def _lum(c: QColor) -> float:
    def ch(v: int) -> float:
        s = v / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c.red()) + 0.7152 * ch(c.green()) + 0.0722 * ch(c.blue())


def contrast(a, b) -> float:
    """Контраст по WCAG 2: 1 — нет различия, 21 — чёрное на белом; текст читается от ≈ 4,5."""
    la, lb = _lum(QColor(a)), _lum(QColor(b))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def dark_palette() -> QPalette:
    """Тёмная палитра (как у Windows 11 в тёмном режиме) — для самопроверки."""
    p = QPalette()
    win, base, txt = QColor("#202020"), QColor("#2b2b2b"), QColor("#ffffff")
    for group in (QPalette.Active, QPalette.Inactive):
        p.setColor(group, QPalette.Window, win)
        p.setColor(group, QPalette.WindowText, txt)
        p.setColor(group, QPalette.Base, base)
        p.setColor(group, QPalette.AlternateBase, QColor("#323232"))
        p.setColor(group, QPalette.Text, txt)
        p.setColor(group, QPalette.Button, QColor("#2d2d2d"))
        p.setColor(group, QPalette.ButtonText, txt)
        p.setColor(group, QPalette.Highlight, QColor("#0078d4"))
        p.setColor(group, QPalette.HighlightedText, txt)
        p.setColor(group, QPalette.ToolTipBase, base)
        p.setColor(group, QPalette.ToolTipText, txt)
        p.setColor(group, QPalette.PlaceholderText, QColor("#9a9a9a"))
    p.setColor(QPalette.Disabled, QPalette.Window, win)
    p.setColor(QPalette.Disabled, QPalette.Base, QColor("#262626"))
    p.setColor(QPalette.Disabled, QPalette.Button, QColor("#2a2a2a"))
    p.setColor(QPalette.Disabled, QPalette.WindowText, QColor("#7a7a7a"))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor("#7a7a7a"))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#7a7a7a"))
    return p


class Theme:
    def __init__(self, palette: QPalette):
        window = palette.color(QPalette.Window)
        text = palette.color(QPalette.WindowText)
        self.dark = window.lightness() < 128
        self.window = window
        self.text = text
        self.base = palette.color(QPalette.Base)
        self.base_text = palette.color(QPalette.Text)
        d = self.dark
        # приглушённый текст (подсказки): смесь текста и фона
        self.muted = mix(text, window, 0.38).name()
        self.error = "#ff8a80" if d else "#b00020"
        self.summary_bg = "#16283a" if d else "#eef4ff"
        self.summary_fg = "#e8f0f8" if d else "#0b1f33"
        self._banner = {
            "amber": (("#4a3b0a", "#ffe08a") if d else ("#fff3cd", "#664d03")),
            "green": (("#163a1e", "#9be3a8") if d else ("#dff5df", "#124d12")),
            "red": (("#4a1c1c", "#ffb4b4") if d else ("#fde8e8", "#8a1c1c")),
        }
        self._level = {
            0: (("#1f4d2b", "#f2f2f2") if d else ("#dff5df", "#000000")),
            1: (("#5a4a14", "#f2f2f2") if d else ("#fff3cd", "#000000")),
            2: (("#5e2530", "#f2f2f2") if d else ("#f8d7da", "#000000")),
        }
        self.highlight = ("#24476b", "#ffffff") if d else ("#dfeeff", "#000000")
        # цвета графика
        self.plot_bg = self.base if d else QColor("#ffffff")
        self.plot_ink = self.base_text if d else QColor("#222222")
        self.plot_grid = mix(self.plot_bg, self.plot_ink, 0.16)
        self._roles = {
            "line": "#58a6ff" if d else "#1f6feb",
            "target": "#ff6b6b" if d else "#cc0000",
            "ref": "#8b949e" if d else "#999999",
            "dot": "#58a6ff" if d else "#1f6feb",
        }

    def banner(self, kind: str) -> Tuple[str, str]:
        return self._banner[kind]

    def level(self, level: int) -> Tuple[QColor, QColor]:
        bg, fg = self._level[level]
        return QColor(bg), QColor(fg)

    def role(self, name_or_hex: str) -> QColor:
        """«line», «target», «ref», «dot» — цвета по смыслу, иначе обычная запись цвета (#rrggbb)."""
        return QColor(self._roles.get(name_or_hex, name_or_hex))

    def contrast_report(self):
        """Пары «текст / фон» этой темы и их контраст: [(название, контраст, минимум)]."""
        pairs = [
            ("сводка", self.summary_fg, self.summary_bg, 7.0),
            ("подсказки", self.muted, self.window, 3.0),
            ("ошибки", self.error, self.window, 4.5),
            ("выделение строки", self.highlight[1], self.highlight[0], 4.5),
            ("график: подписи", self.plot_ink, self.plot_bg, 4.5),
            ("график: линия", self._roles["line"], self.plot_bg, 3.0),
            ("график: цель", self._roles["target"], self.plot_bg, 3.0),
        ]
        pairs += [(f"баннер {k}", fg, bg, 4.5) for k, (bg, fg) in self._banner.items()]
        pairs += [(f"уровень {k}", fg, bg, 4.5) for k, (bg, fg) in self._level.items()]
        return [(name, contrast(fg, bg), need) for name, fg, bg, need in pairs]


_NO_DARK_STYLES = ("windowsvista", "windows")


def ensure_style_for_palette(app: Optional[QApplication] = None) -> bool:
    """Стиль «windowsvista» рисует фон полей и вкладок средствами Windows и тёмную палитру не понимает:
    получается светлый фон со светлым текстом. Если палитра тёмная, а стиль такой — переходим на Fusion,
    который рисует всё по палитре. Возвращает True, если стиль сменён."""
    app = app or QApplication.instance()
    if app is None:
        return False
    style = app.style()
    name = style.name().lower() if style is not None else ""
    if name in _NO_DARK_STYLES and Theme(app.palette()).dark:
        app.setStyle("Fusion")
        return True
    return False


def theme_for(widget: Optional[QWidget] = None) -> Theme:
    if widget is not None:
        return Theme(widget.palette())
    app = QApplication.instance()
    return Theme(app.palette() if app else QPalette())
