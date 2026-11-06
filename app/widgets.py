"""Свои виджеты оформления (рисуются QPainter, без картинок):

* Glass — плавающий слой «стекло»: полупрозрачная заливка, светлая кромка сверху и тёмная снизу, блик в верхней
  части и мягкая тень (настоящее гауссово размытие, посчитанное один раз на размер и растянутое по 9 частям).
  Размытия того, что под стеклом, нет — в Qt Widgets его не сделать дёшево; стекло читается по кромке, блику и тени
  на тёплом фоне окна (paint_backdrop).
* Gauge — прибор дугой 270°: точка — сейчас, ручка — что поставить, золотая дуга между ними — насколько и в какую
  сторону крутить, тонкая зелёная полоса внутри — где модель проверена журналом. DDial — разгрузка D (0…100 с),
  TDial — T блоков (30…90 °C): пара приборов раздела «Текущий проход».
* Sparkline — маленький график в плитке (последние пробы выхода, цель, прогноз).
* ProgressLine — тонкая полоса «сколько уже сделано» (выгрузка прохода).
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
                           QRadialGradient)
from PySide6.QtWidgets import QFrame, QSizePolicy, QWidget

from . import design

# ----------------------------------------------------------------------------------------------------------------
# Тень: гауссово размытие скруглённого прямоугольника (numpy), 9-частная растяжка под любой размер
# ----------------------------------------------------------------------------------------------------------------

_SHADOWS: Dict[Tuple, Tuple[QImage, int]] = {}


def _shadow_image(radius: int, blur: int, rgba: Tuple[int, int, int, int]) -> Tuple[QImage, int]:
    """Картинка тени и размер её угла (в пикселях). Середина картинки — участок прямой кромки (растягивается)."""
    key = (radius, blur, rgba)
    if key in _SHADOWS:
        return _SHADOWS[key]
    sigma = max(blur / 2.0, 0.5)
    kr = int(math.ceil(3 * sigma))
    pad = 2 * blur
    corner = pad + radius + kr
    n = 2 * corner + 1
    mask = QImage(n, n, QImage.Format_ARGB32_Premultiplied)
    mask.fill(Qt.transparent)
    p = QPainter(mask)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0, 255))
    p.drawRoundedRect(QRectF(pad, pad, n - 2 * pad, n - 2 * pad), radius, radius)
    p.end()
    raw = np.frombuffer(mask.constBits(), dtype=np.uint8, count=mask.sizeInBytes())
    a = raw.reshape(n, mask.bytesPerLine() // 4, 4)[:, :n, 3].astype(np.float64) / 255.0
    x = np.arange(-kr, kr + 1, dtype=np.float64)
    k = np.exp(-0.5 * (x / sigma) ** 2)
    k /= k.sum()
    band = np.zeros((n, n))
    for i in range(n):                      # матрица свёртки: размытие = B · A · Bᵀ
        lo, hi = max(0, i - kr), min(n, i + kr + 1)
        band[i, lo:hi] = k[lo - i + kr:hi - i + kr]
    a = band @ a @ band.T
    r, g, b, al = rgba
    alpha = np.clip(a * al, 0, 255)
    out = np.empty((n, n, 4), dtype=np.uint8)
    out[..., 0] = np.round(alpha * b / 255.0)   # ARGB32 в памяти: B, G, R, A (premultiplied)
    out[..., 1] = np.round(alpha * g / 255.0)
    out[..., 2] = np.round(alpha * r / 255.0)
    out[..., 3] = np.round(alpha)
    buf = out.tobytes()
    img = QImage(buf, n, n, n * 4, QImage.Format_ARGB32_Premultiplied).copy()
    _SHADOWS[key] = (img, corner)
    return img, corner


def paint_shadow(p: QPainter, body: QRectF, radius: float, blur: int, color: QColor, dy: float = 3.0) -> None:
    """Мягкая тень под скруглённым прямоугольником body (сдвинута вниз на dy)."""
    if blur <= 0 or color.alpha() == 0:
        return
    img, corner = _shadow_image(int(round(radius)), int(blur), color.getRgb())
    n = img.width()
    pad = 2 * int(blur)
    # целые пиксели: дробные края частей дают просветы-«швы» между ними
    left, top = int(round(body.left())) - pad, int(round(body.top() + dy)) - pad
    right, bottom = int(round(body.right())) + pad, int(round(body.bottom() + dy)) + pad
    if right - left < 2 * corner + 1 or bottom - top < 2 * corner + 1:
        return
    sx = [0, corner, corner + 1, n]
    tx = [left, left + corner, right - corner, right]
    ty = [top, top + corner, bottom - corner, bottom]
    p.save()
    p.setRenderHint(QPainter.SmoothPixmapTransform, False)
    for i in range(3):
        for j in range(3):
            src = QRect(sx[i], sx[j], sx[i + 1] - sx[i], sx[j + 1] - sx[j])
            dst = QRect(tx[i], ty[j], tx[i + 1] - tx[i], ty[j + 1] - ty[j])
            if dst.width() > 0 and dst.height() > 0:
                p.drawImage(dst, img, src)
    p.restore()


def paint_backdrop(p: QPainter, rect: QRectF, t: design.Tokens) -> None:
    """Фон окна: тёплый градиент сверху вниз и слабое золотистое свечение у меню (за стеклом)."""
    g = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    g.setColorAt(0.0, QColor(t.paper))
    g.setColorAt(1.0, QColor(t.paper2))
    p.fillRect(rect, g)
    glow = design.qcolor(t.glow)
    rg = QRadialGradient(QPointF(rect.left() + 150, rect.top() + 40), max(rect.width(), rect.height()) * 0.6)
    clear = QColor(glow)
    clear.setAlpha(0)
    rg.setColorAt(0.0, glow)
    rg.setColorAt(1.0, clear)
    p.fillRect(rect, rg)


# ----------------------------------------------------------------------------------------------------------------
# Стекло
# ----------------------------------------------------------------------------------------------------------------

class Glass(QFrame):
    """Плавающий слой. tint: None, "gold" (действие), "green" (всё в порядке), "steel" (сведения), "rust"."""

    def __init__(self, radius: int = 16, blur: int = 12, dy: int = 3, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._radius, self._blur, self._dy = radius, blur, dy
        self._tint: Optional[str] = None
        self.setFrameShape(QFrame.NoFrame)
        self.setContentsMargins(*self.shadow_margins())

    def shadow_margins(self) -> Tuple[int, int, int, int]:
        """Поля под тень (слева, сверху, справа, снизу) — внутри виджета: соседей выравнивать на них."""
        b = self._blur
        return (b, max(b - self._dy, 0), b, b + self._dy) if b else (0, 0, 0, 0)

    def body(self) -> QRectF:
        l, t, r, b = self.shadow_margins()
        return QRectF(self.rect()).adjusted(l + 0.5, t + 0.5, -r - 0.5, -b - 0.5)

    def set_tint(self, tint: Optional[str]) -> None:
        if tint != self._tint:
            self._tint = tint
            self.update()

    def tint(self) -> Optional[str]:
        return self._tint

    def paintEvent(self, _e) -> None:  # noqa: N802
        t = design.tokens_for(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        body = self.body()
        rad = float(self._radius)
        path = QPainterPath()
        path.addRoundedRect(body, rad, rad)
        if self._blur:                       # тень — только вокруг: под стеклом её не видно (оно матовое)
            outside = QPainterPath()
            outside.addRect(QRectF(self.rect()))
            p.save()
            p.setClipPath(outside.subtracted(path))
            paint_shadow(p, body, rad, self._blur, design.qcolor(t.shadow), self._dy)
            p.restore()
        p.fillPath(path, design.qcolor(t.glass))
        tc = design.tint_color(t, self._tint)
        if tc:
            g = QLinearGradient(body.topLeft(), body.bottomRight())
            c0, c1 = QColor(tc), QColor(tc)
            c0.setAlphaF(0.17 if t.dark else 0.24)
            c1.setAlphaF(0.07 if t.dark else 0.10)
            g.setColorAt(0.0, c0)
            g.setColorAt(1.0, c1)
            p.fillPath(path, g)
        hi = design.qcolor(t.glass_hi)
        sheen = QLinearGradient(body.topLeft(), QPointF(body.left(), body.top() + body.height() * 0.6))
        s0, s1 = QColor(hi), QColor(hi)
        s0.setAlphaF(hi.alphaF() * (0.30 if t.dark else 0.42))
        s1.setAlpha(0)
        sheen.setColorAt(0.0, s0)
        sheen.setColorAt(1.0, s1)
        p.fillPath(path, sheen)
        rim = QLinearGradient(body.topLeft(), body.bottomLeft())
        mid = QColor(hi)
        mid.setAlphaF(hi.alphaF() * 0.30)
        rim.setColorAt(0.0, hi)
        rim.setColorAt(0.45, mid)
        rim.setColorAt(1.0, design.qcolor(t.glass_lo))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QBrush(rim), 1.0))
        p.drawPath(path)
        p.end()


# ----------------------------------------------------------------------------------------------------------------
# Шкала D
# ----------------------------------------------------------------------------------------------------------------

class Gauge(QWidget):
    """Прибор-шкала дугой 270° (как на приборной панели): lo — слева внизу, hi — справа внизу.

    kind: change — стальная точка «сейчас», золотая дуга до золотой ручки «поставить», в центре — новое значение;
          keep — зелёная ручка на текущем; hold — стальная ручка на текущем (эту величину совет не трогает);
          wait — стальная ручка на текущем; info — стальная ручка на проверяемом; nodata — пустая шкала.
    Полоса (set_band) — тонкая зелёная дуга внутри шкалы: где модель проверена журналом. text — что написать в центре
    вместо числа (например, «50/55» для двух блоков)."""

    START, SPAN = 225.0, 270.0
    LO, HI, UNIT, STEP, TIP = 0.0, 100.0, " с", 10, ""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._kind = "nodata"
        self._cur: Optional[float] = None
        self._knob: Optional[float] = None
        self._caption = ""
        self._text: Optional[str] = None
        self._band: Optional[Tuple[float, float]] = None
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setToolTip(self.TIP)

    def set_state(self, kind: str, cur: Optional[float], knob: Optional[float], caption: str = "",
                  text: Optional[str] = None) -> None:
        self._kind, self._cur, self._knob, self._caption, self._text = kind, cur, knob, caption, text
        self.update()

    def state(self) -> Tuple[str, Optional[float], Optional[float], str]:
        return self._kind, self._cur, self._knob, self._caption

    def text(self) -> Optional[str]:
        return self._text

    def set_band(self, band: Optional[Tuple[float, float]]) -> None:
        """Проверенный журналом диапазон (мин, макс) или None — полосы нет."""
        self._band = (float(band[0]), float(band[1])) if band else None
        self.update()

    def band(self) -> Optional[Tuple[float, float]]:
        return self._band

    def sizeHint(self) -> QSize:  # noqa: N802
        s = int(self.fontMetrics().height() * 7.8)
        return QSize(s, s)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def _ang(self, v: float) -> float:
        f = (max(self.LO, min(self.HI, v)) - self.LO) / (self.HI - self.LO)
        return self.START - self.SPAN * f

    def paintEvent(self, _e) -> None:  # noqa: N802
        t = design.tokens_for(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        side = float(min(self.width(), self.height()))
        stroke = max(6.0, side * 0.072)
        m = stroke / 2 + stroke * 0.9
        x0 = (self.width() - side) / 2 + m
        y0 = (self.height() - side) / 2 + m
        r = QRectF(x0, y0, side - 2 * m, side - 2 * m)
        c = r.center()
        rad = r.width() / 2

        def at(v: float, rr: float) -> QPointF:
            a = math.radians(self._ang(v))
            return QPointF(c.x() + rr * math.cos(a), c.y() - rr * math.sin(a))

        pen = QPen(QColor(t.line), stroke)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(r, int(self.START * 16), int(-self.SPAN * 16))
        # проверенная журналом зона — тонкая зелёная дуга по внутреннему краю шкалы
        if self._band is not None:
            b0, b1 = max(self._band[0], self.LO), min(self._band[1], self.HI)
            if b1 > b0:
                rb = rad - stroke * 0.5 - stroke * 0.42
                rr = QRectF(c.x() - rb, c.y() - rb, 2 * rb, 2 * rb)
                band = QColor(t.good)
                band.setAlphaF(0.85)
                bp = QPen(band, max(2.0, stroke * 0.34))
                bp.setCapStyle(Qt.FlatCap)
                p.setPen(bp)
                a0, a1 = self._ang(b0), self._ang(b1)
                p.drawArc(rr, int(round(a0 * 16)), int(round((a1 - a0) * 16)))
        tick = QPen(QColor(t.line_strong), 1.2)
        tick.setCapStyle(Qt.RoundCap)
        p.setPen(tick)
        n_steps = int(round((self.HI - self.LO) / self.STEP))
        for k in range(n_steps + 1):
            v = self.LO + k * self.STEP
            r1 = rad - stroke * 0.5 - stroke * 0.95
            r2 = r1 - (stroke * 0.85 if k in (0, n_steps) or k * 2 == n_steps else stroke * 0.45)
            p.drawLine(at(v, r1), at(v, r2))
        kind, cur, knob = self._kind, self._cur, self._knob
        if kind == "change" and cur is not None and knob is not None:
            gold = QPen(QColor(t.accent), stroke)
            gold.setCapStyle(Qt.RoundCap)
            p.setPen(gold)
            a0, a1 = self._ang(cur), self._ang(knob)
            p.drawArc(r, int(round(a0 * 16)), int(round((a1 - a0) * 16)))
        ring = QPen(QColor(t.surface), max(1.5, stroke * 0.22))
        if cur is not None and not (kind in ("keep", "wait", "hold") and knob is not None and abs(knob - cur) < 0.5):
            p.setPen(ring)
            p.setBrush(QColor(t.steel))
            p.drawEllipse(at(cur, rad), stroke * 0.45, stroke * 0.45)
        if knob is not None:
            col = {"change": t.accent, "keep": t.good, "wait": t.steel, "info": t.steel, "hold": t.steel}.get(kind, t.muted)
            p.setPen(ring)
            p.setBrush(QColor(col))
            p.drawEllipse(at(knob, rad), stroke * 0.80, stroke * 0.80)
        # подписи концов шкалы
        small = QFont(self.font())
        small.setPointSizeF(max(self.font().pointSizeF() - 1.5, 6.5))
        p.setFont(small)
        p.setPen(QColor(t.muted))
        fm = QFontMetricsF(small)
        for v in (self.LO, self.HI):
            txt = f"{v:g}"
            pt = at(v, rad)
            w = fm.horizontalAdvance(txt)
            p.drawText(QRectF(pt.x() - w / 2 - 2, pt.y() + stroke * 0.65, w + 4, fm.height()), Qt.AlignCenter, txt)
        # число в центре
        val = knob if knob is not None else cur
        num = "—" if val is None or kind == "nodata" else (self._text or f"{val:.0f}")
        big = design.num_font(1.0)
        unit = QFont(self.font())
        show_unit = num != "—"
        px = side * 0.27
        while True:                       # число с единицей — по ширине круга внутри делений («60/65 °C» мельче)
            big.setPixelSize(max(int(px), 10))
            unit.setPixelSize(max(int(px * 0.41), 8))
            fmb, fmu = QFontMetricsF(big), QFontMetricsF(unit)
            wn = fmb.horizontalAdvance(num)
            wu = fmu.horizontalAdvance(self.UNIT) if show_unit else 0.0
            if wn + wu <= side * 0.56 or px <= 10:
                break
            px *= 0.92
        base_y = c.y() + fmb.capHeight() / 2 - (fm.height() * 0.35 if self._caption else 0)
        xn = c.x() - (wn + wu) / 2
        p.setFont(big)
        p.setPen(QColor(t.ink if kind != "nodata" else t.muted))
        p.drawText(QPointF(xn, base_y), num)
        if show_unit:
            p.setFont(unit)
            p.setPen(QColor(t.ink2))
            p.drawText(QPointF(xn + wn, base_y), self.UNIT)
        if self._caption:
            p.setFont(small)
            p.setPen(QColor(t.ink2))
            cw = fm.horizontalAdvance(self._caption)
            p.drawText(QRectF(c.x() - cw / 2 - 4, base_y + fm.height() * 0.35, cw + 8, fm.height()), Qt.AlignCenter,
                       self._caption)
        p.end()


class DDial(Gauge):
    """Шкала разгрузки D: 0…100 с."""
    LO, HI, UNIT, STEP = 0.0, 100.0, " с", 10
    TIP = ("Разгрузка D, с: точка — сейчас, ручка — что поставить, золотая дуга — насколько крутить. Зелёная полоса "
           "внутри — D, при которых журнал проверил модель.")


class TDial(Gauge):
    """Шкала T блоков: 30…90 °C; в центре — обе ступени, как их ставят («50/45»), ручка — по средней."""
    LO, HI, UNIT, STEP = 30.0, 90.0, " °C", 10
    TIP = ("T блоков, °C — 1-я ступень / 2-я ступень, как на пульте (например, 50/45). Точка — сейчас (по средней), "
           "ручка — что поставить, золотая дуга — насколько. Стальная ручка без дуги — T не менять. Зелёная полоса "
           "внутри — T, при которых журнал проверил модель.")


# ----------------------------------------------------------------------------------------------------------------
# Плитки: спарклайн и полоса прогресса
# ----------------------------------------------------------------------------------------------------------------

class Sparkline(QWidget):
    """Маленький график: пробы (линия с последней точкой), цель (пунктир), прогноз (бледный пунктир)."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._xs: List[float] = []
        self._ys: List[float] = []
        self._fx: List[float] = []
        self._fy: List[float] = []
        self._target: Optional[float] = None
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_data(self, xs: Sequence[float], ys: Sequence[float], target: Optional[float] = None,
                 fx: Sequence[float] = (), fy: Sequence[float] = ()) -> None:
        self._xs, self._ys = list(xs), list(ys)
        self._fx, self._fy = list(fx), list(fy)
        self._target = target
        self.setVisible(len(self._ys) >= 2)
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        h = self.fontMetrics().height()
        return QSize(int(h * 6), int(h * 1.7))

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        h = self.fontMetrics().height()
        return QSize(int(h * 2), int(h * 1.7))

    def paintEvent(self, _e) -> None:  # noqa: N802
        if len(self._ys) < 2:
            return
        t = design.tokens_for(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        xs = self._xs + self._fx
        ys = self._ys + self._fy + ([self._target] if self._target is not None else [])
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        if x1 - x0 < 1e-9:
            x1 = x0 + 1.0
        if y1 - y0 < 0.6:
            mid = (y0 + y1) / 2
            y0, y1 = mid - 0.3, mid + 0.3
        pad = 3.5
        w, h = self.width() - 2 * pad, self.height() - 2 * pad

        def pt(x: float, y: float) -> QPointF:
            return QPointF(pad + (x - x0) / (x1 - x0) * w, pad + (1 - (y - y0) / (y1 - y0)) * h)

        if self._target is not None:
            pen = QPen(QColor(t.target), 1.2, Qt.DashLine)
            p.setPen(pen)
            a, b = pt(x0, self._target), pt(x1, self._target)
            p.drawLine(a, b)
        if len(self._fx) >= 1:
            fc = QColor(t.series1)
            fc.setAlphaF(0.55)
            pen = QPen(fc, 1.4, Qt.DashLine)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            path = QPainterPath(pt(self._xs[-1], self._ys[-1]))
            for x, y in zip(self._fx, self._fy):
                path.lineTo(pt(x, y))
            p.drawPath(path)
        pen = QPen(QColor(t.series1), 1.8)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        path = QPainterPath(pt(self._xs[0], self._ys[0]))
        for x, y in zip(self._xs[1:], self._ys[1:]):
            path.lineTo(pt(x, y))
        p.drawPath(path)
        p.setPen(QPen(QColor(t.surface), 1.5))
        p.setBrush(QColor(t.series1))
        p.drawEllipse(pt(self._xs[-1], self._ys[-1]), 3.0, 3.0)
        p.end()


def role_color(t: design.Tokens, role: str) -> str:
    """Цвет серии графика по смыслу (как theme.Theme.role)."""
    return {"line": t.series1, "dot": t.series1, "alt": t.series2, "target": t.target, "ref": t.ref}.get(role, role)


class Legend(QWidget):
    """Подписи серий графика одной строкой: значок (точка, линия или пунктир) цвета серии и текст обычным цветом.
    items: [(вид "dot" | "line" | "dash", роль цвета, подпись)]."""

    def __init__(self, items: Sequence[Tuple[str, str, str]] = (), parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._items = list(items)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def set_items(self, items: Sequence[Tuple[str, str, str]]) -> None:
        self._items = list(items)
        self.updateGeometry()
        self.update()

    def items(self) -> List[Tuple[str, str, str]]:
        return list(self._items)

    def _geometry(self, width: Optional[int] = None):
        fm = QFontMetricsF(self.font())
        sw, gap, sp = fm.height() * 1.3, fm.height() * 0.4, fm.height() * 1.1
        x, y, rows = 0.0, 0.0, []
        lim = width if width else 10 ** 6
        for kind, role, text in self._items:
            w = sw + gap + fm.horizontalAdvance(text)
            if x > 0 and x + w > lim:
                x, y = 0.0, y + fm.height() * 1.35
            rows.append((x, y, kind, role, text))
            x += w + sp
        return rows, y + fm.height() * 1.35, sw, gap

    def sizeHint(self) -> QSize:  # noqa: N802
        rows, h, sw, gap = self._geometry()          # одной строкой
        fm = QFontMetricsF(self.font())
        w = max((x + sw + gap + fm.horizontalAdvance(text) for x, _y, _k, _r, text in rows), default=10.0)
        return QSize(int(w) + 2, int(h))

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, w: int) -> int:  # noqa: N802
        return int(self._geometry(w)[1])

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(40, int(QFontMetricsF(self.font()).height() * 1.35))

    def paintEvent(self, _e) -> None:  # noqa: N802
        t = design.tokens_for(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        fm = QFontMetricsF(self.font())
        rows, _h, sw, gap = self._geometry(self.width())
        for x, y, kind, role, text in rows:
            cy = y + fm.height() * 0.62
            col = QColor(role_color(t, role))
            if kind == "dot":
                p.setPen(Qt.NoPen)
                p.setBrush(col)
                p.drawEllipse(QPointF(x + sw / 2, cy), 3.6, 3.6)
            else:
                pen = QPen(col, 2.4 if kind == "line" else 1.6, Qt.SolidLine if kind == "line" else Qt.DashLine)
                pen.setCapStyle(Qt.RoundCap if kind == "line" else Qt.FlatCap)
                p.setPen(pen)
                p.drawLine(QPointF(x + 1, cy), QPointF(x + sw - 1, cy))
            p.setPen(QColor(t.ink2))
            p.drawText(QPointF(x + sw + gap, y + fm.ascent() + (fm.height() * 1.35 - fm.height()) / 2 - 1), text)
        p.end()


class ProgressLine(QWidget):
    """Тонкая полоса прогресса (0…1)."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._frac: Optional[float] = None
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_value(self, frac: Optional[float]) -> None:
        self._frac = None if frac is None else max(0.0, min(1.0, float(frac)))
        self.setVisible(self._frac is not None)
        self.update()

    def value(self) -> Optional[float]:
        return self._frac

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(60, 10)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(20, 10)

    def paintEvent(self, _e) -> None:  # noqa: N802
        if self._frac is None:
            return
        t = design.tokens_for(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = 5.0
        r = QRectF(0.5, (self.height() - h) / 2, self.width() - 1.0, h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(t.line_soft if not t.dark else t.line))
        p.drawRoundedRect(r, h / 2, h / 2)
        if self._frac > 0:
            f = QRectF(r.left(), r.top(), max(h, r.width() * self._frac), h)
            p.setBrush(QColor(t.steel))
            p.drawRoundedRect(f, h / 2, h / 2)
        p.end()
