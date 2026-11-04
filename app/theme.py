"""Цвета для рисования (графики, подсветка строк, баннеры) — из токенов оформления (design.py).

Тёмная или светлая — по палитре окна (светлота фона < 50 % — тёмная): палитру ставит design.apply по выбору
«Как в Windows / Светлое / Тёмное», и всё, что рисуется по Theme, перекрашивается вместе с ней.
"""
from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QWidget


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


class Theme:
    """Цвета для рисования и подсказок — из токенов оформления (design.py) под тёмную или светлую тему окна."""

    def __init__(self, palette: QPalette):
        from . import design
        window = palette.color(QPalette.Window)
        self.dark = window.lightness() < 128
        t = design.DARK if self.dark else design.LIGHT
        self.t = t
        self.window = QColor(t.paper)
        self.text = QColor(t.ink)
        self.base = QColor(t.surface)
        self.base_text = QColor(t.ink)
        self.muted = t.muted
        self.error = t.rust
        self.summary_bg = t.steel_tint
        self.summary_fg = t.ink
        self._banner = {"amber": (t.accent_tint, t.amber_ink), "green": (t.good_tint, t.good_ink),
                        "red": (t.rust_tint, t.rust_ink)}
        self._level = {0: (t.good_tint, t.ink), 1: (t.accent_tint, t.ink), 2: (t.rust_tint, t.ink)}
        self.highlight = (t.accent_tint, t.ink)
        # цвета графика
        self.plot_bg = QColor(t.surface)
        self.plot_ink = QColor(t.ink2)
        self.plot_grid = QColor(t.line_soft)
        self._roles = {"line": t.series1, "dot": t.series1, "alt": t.series2, "target": t.target, "ref": t.ref}

    def banner(self, kind: str) -> Tuple[str, str]:
        return self._banner[kind]

    def level(self, level: int) -> Tuple[QColor, QColor]:
        bg, fg = self._level[level]
        return QColor(bg), QColor(fg)

    def role(self, name_or_hex: str) -> QColor:
        """«line», «dot» (данные), «alt» (вторая серия), «target» (цель), «ref» (фон-контекст) — цвета по смыслу,
        иначе обычная запись цвета (#rrggbb)."""
        return QColor(self._roles.get(name_or_hex, name_or_hex))

    def contrast_report(self):
        """Пары «текст / фон» этой темы и их контраст: [(название, контраст, минимум)]."""
        t = self.t
        pairs = [
            ("сводка", self.summary_fg, self.summary_bg, 7.0),
            ("подсказки", self.muted, t.paper, 4.5),
            ("подсказки на карточке", self.muted, t.surface, 4.5),
            ("ошибки", self.error, t.paper, 4.5),
            ("выделение строки", self.highlight[1], self.highlight[0], 4.5),
            ("главная кнопка", t.accent_ink, t.accent, 4.5),
            ("график: подписи", self.plot_ink, self.plot_bg, 4.5),
            ("график: данные", self._roles["line"], self.plot_bg, 3.0),
            ("график: вторая серия", self._roles["alt"], self.plot_bg, 3.0),
            ("график: цель", self._roles["target"], self.plot_bg, 3.0),
        ]
        pairs += [(f"баннер {k}", fg, bg, 4.5) for k, (bg, fg) in self._banner.items()]
        pairs += [(f"уровень {k}", fg, bg, 4.5) for k, (bg, fg) in self._level.items()]
        return [(name, contrast(fg, bg), need) for name, fg, bg, need in pairs]


def theme_for(widget: Optional[QWidget] = None) -> Theme:
    """Цвета по палитре окна виджета (см. design.window_palette: у прозрачных в QSS виджетов своя палитра обманчива)."""
    from .design import window_palette
    return Theme(window_palette(widget))
