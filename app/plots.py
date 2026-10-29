"""Простой график на QPainter: линии, точки, горизонтальные линии, сетка, подписи осей.

Заменяет pyqtgraph + numpy (около 20 МБ в собранном .exe) — для трёх графиков программы этого достаточно.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QBrush, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from .theme import theme_for


def nice_ticks(lo: float, hi: float, target: int = 6) -> Tuple[List[float], float]:
    """Деления «круглыми» числами (шаг 1·2·2,5·5·10 · 10^k) в пределах [lo, hi]."""
    if not (hi - lo > 1e-12):
        hi = lo + 1.0
    raw = (hi - lo) / max(target, 1)
    mag = 10.0 ** math.floor(math.log10(raw))
    step = mag * 10.0
    for m in (1.0, 2.0, 2.5, 5.0, 10.0):
        if m * mag >= raw - 1e-12:
            step = m * mag
            break
    t = math.ceil(lo / step - 1e-9) * step
    ticks: List[float] = []
    while t <= hi + step * 1e-9:
        ticks.append(round(t, 10))
        t += step
    return ticks, step


def _tick_text(value: float, step: float) -> str:
    dec = 0
    while dec < 6 and abs(step * 10 ** dec - round(step * 10 ** dec)) > 1e-9:
        dec += 1
    text = f"{value:.{dec}f}"
    return "0" if text in ("-0", "-0.0", "-0.00") else text


class _Series:
    def __init__(self, kind: str, xs: Sequence[float], ys: Sequence[float], color: str, width: float, dashed: bool):
        self.kind, self.xs, self.ys, self.color, self.width, self.dashed = kind, list(xs), list(ys), color, width, dashed


class SimplePlot(QWidget):
    """Белый график с рамкой, серой сеткой и подписями осей."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._series: List[_Series] = []
        self._hlines: List[Tuple[float, str, bool]] = []
        self._title = ""
        self._xlabel = ""
        self._ylabel = ""
        self._zero_x = False
        self._zero_y = False
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(260, 170)
        self.setAutoFillBackground(False)

    # --- API ------------------------------------------------------------
    def clear(self) -> None:
        self._series.clear()
        self._hlines.clear()
        self._title = ""
        self.update()

    def set_labels(self, x_label: str, y_label: str) -> None:
        self._xlabel, self._ylabel = x_label, y_label
        self.update()

    def set_title(self, text: str) -> None:
        self._title = text
        self.update()

    def include_zero(self, x: bool = False, y: bool = False) -> None:
        self._zero_x, self._zero_y = x, y
        self.update()

    def add_line(self, xs: Sequence[float], ys: Sequence[float], color: str = "line", width: float = 2.0,
                 dashed: bool = False) -> None:
        self._series.append(_Series("line", xs, ys, color, width, dashed))
        self.update()

    def add_points(self, xs: Sequence[float], ys: Sequence[float], color: str = "dot", size: float = 6.0) -> None:
        self._series.append(_Series("points", xs, ys, color, size, False))
        self.update()

    def add_hline(self, y: float, color: str = "target", dashed: bool = True) -> None:
        self._hlines.append((float(y), color, dashed))
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(520, 300)

    # --- диапазоны ------------------------------------------------------
    def _data_range(self) -> Tuple[float, float, float, float]:
        xs = [x for s in self._series for x in s.xs]
        ys = [y for s in self._series for y in s.ys] + [h[0] for h in self._hlines]
        if not xs:
            xs = [0.0, 1.0]
        if not ys:
            ys = [0.0, 1.0]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        if self._zero_x:
            x0 = min(x0, 0.0)
        if self._zero_y:
            y0 = min(y0, 0.0)
        if x1 - x0 < 1e-9:
            x1 = x0 + 1.0
        if y1 - y0 < 1e-9:
            y1 = y0 + 1.0
        pad_y = 0.05 * (y1 - y0)
        pad_x = 0.0 if self._zero_x else 0.03 * (x1 - x0)
        y_lo = y0 if (self._zero_y and y0 == 0.0) else y0 - pad_y      # от нуля — без запаса снизу
        return x0 - pad_x, x1 + pad_x, y_lo, y1 + pad_y

    # --- рисование ------------------------------------------------------
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        w, h = self.width(), self.height()
        th = theme_for(self)
        p.fillRect(0, 0, w, h, th.plot_bg)
        ink = th.plot_ink
        grid = th.plot_grid

        base = QFont(self.font())
        small = QFont(base)
        small.setPointSizeF(max(base.pointSizeF() - 1.0, 7.0))
        title_font = QFont(base)
        title_font.setPointSizeF(base.pointSizeF() + 1.0)
        fm_t = QFontMetrics(small)
        fm_title = QFontMetrics(title_font)
        label_h = fm_t.height()

        x_lo, x_hi, y_lo, y_hi = self._data_range()

        # приблизительная область для выбора числа делений
        approx_w = max(w - 80, 120)
        approx_h = max(h - 90, 60)
        xt, xstep = nice_ticks(x_lo, x_hi, int(min(max(approx_w / 70, 3), 10)))
        yt, ystep = nice_ticks(y_lo, y_hi, int(min(max(approx_h / 38, 3), 8)))
        y_labels = [_tick_text(v, ystep) for v in yt]
        x_labels = [_tick_text(v, xstep) for v in xt]
        ytick_w = max((fm_t.horizontalAdvance(s) for s in y_labels), default=20)

        left = 10 + (label_h + 4 if self._ylabel else 0) + ytick_w + 8
        right = 16
        top = 10 + (fm_title.height() + 8 if self._title else 0)
        bottom = 8 + label_h + 6 + (label_h + 4 if self._xlabel else 0)
        pw, ph = w - left - right, h - top - bottom
        if pw < 40 or ph < 30:
            p.end()
            return
        rect = QRectF(left, top, pw, ph)

        def sx(x: float) -> float:
            return rect.left() + (x - x_lo) / (x_hi - x_lo) * rect.width()

        def sy(y: float) -> float:
            return rect.bottom() - (y - y_lo) / (y_hi - y_lo) * rect.height()

        # сетка и подписи делений
        p.setFont(small)
        p.setPen(QPen(grid, 1))
        for v in xt:
            x = sx(v)
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        for v in yt:
            y = sy(v)
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        p.setPen(QPen(ink, 1))
        for v, s in zip(xt, x_labels):
            x = sx(v)
            p.drawText(QRectF(x - 30, rect.bottom() + 4, 60, label_h), Qt.AlignHCenter | Qt.AlignTop, s)
        for v, s in zip(yt, y_labels):
            y = sy(v)
            p.drawText(QRectF(rect.left() - ytick_w - 6, y - label_h / 2, ytick_w, label_h),
                       Qt.AlignRight | Qt.AlignVCenter, s)

        # данные (с обрезкой по области графика)
        p.save()
        p.setClipRect(rect.adjusted(-1, -1, 1, 1))
        for yv, color, dashed in self._hlines:
            pen = QPen(th.role(color), 1.5)
            pen.setStyle(Qt.DashLine if dashed else Qt.SolidLine)
            p.setPen(pen)
            p.drawLine(QPointF(rect.left(), sy(yv)), QPointF(rect.right(), sy(yv)))
        for s in self._series:
            if s.kind == "line" and len(s.xs) >= 2:
                pen = QPen(th.role(s.color), s.width)
                pen.setStyle(Qt.DashLine if s.dashed else Qt.SolidLine)
                pen.setJoinStyle(Qt.RoundJoin)
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                path = QPainterPath(QPointF(sx(s.xs[0]), sy(s.ys[0])))
                for x, y in zip(s.xs[1:], s.ys[1:]):
                    path.lineTo(sx(x), sy(y))
                p.drawPath(path)
            elif s.kind == "points":
                p.setPen(QPen(th.plot_bg, 0.8))
                p.setBrush(QBrush(th.role(s.color)))
                r = s.width / 2.0
                for x, y in zip(s.xs, s.ys):
                    p.drawEllipse(QPointF(sx(x), sy(y)), r, r)
        p.restore()

        # рамка
        p.setPen(QPen(ink, 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(rect)

        # заголовок и подписи осей
        if self._title:
            p.setFont(title_font)
            text = fm_title.elidedText(self._title, Qt.ElideRight, int(w - 20))
            p.drawText(QRectF(10, 6, w - 20, fm_title.height() + 2), Qt.AlignHCenter | Qt.AlignVCenter, text)
        p.setFont(small)
        if self._xlabel:
            p.drawText(QRectF(rect.left(), h - label_h - 4, rect.width(), label_h), Qt.AlignHCenter | Qt.AlignVCenter,
                       fm_t.elidedText(self._xlabel, Qt.ElideRight, int(rect.width())))
        if self._ylabel:
            p.save()
            p.translate(12 + label_h / 2, rect.center().y())
            p.rotate(-90)
            p.drawText(QRectF(-rect.height() / 2, -label_h / 2, rect.height(), label_h), Qt.AlignHCenter | Qt.AlignVCenter,
                       fm_t.elidedText(self._ylabel, Qt.ElideRight, int(rect.height())))
            p.restore()
        p.end()
