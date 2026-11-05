"""Иконки разделов и знаков — нарисованы кодом (без файлов и без интернета), линией 1,6 на сетке 24×24.

Каждая иконка — о содержании раздела: журнал — тетрадь, текущий проход — шкала-прибор, досушка — круг по кругу,
план партии — ступени влажности по проходам, масса на выходные — календарь с залитыми
выходными, быстрая оценка — секундомер, партии — мешки, погода — облако,
точность — мишень, настройки — регуляторы, справка — вопрос. Знак программы — зерно кукурузы. Оформление:
солнце (светлое), месяц (тёмное), полузалитый круг (как в Windows).
"""
from __future__ import annotations

import math
from typing import Callable, Dict

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap


def _pen(p: QPainter, color: QColor, w: float = 1.6) -> None:
    pen = QPen(color, w)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)


def _journal(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    p.drawRoundedRect(QRectF(5, 3, 14, 18), 2.2, 2.2)
    for y in (8.5, 12.5, 16.5):
        p.drawLine(QPointF(9, y), QPointF(15.5, y))
    p.drawLine(QPointF(7.5, 3.5), QPointF(7.5, 20.5))


def _live(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    p.drawArc(QRectF(3.5, 5.5, 17, 17), 200 * 16, -220 * 16)
    a = math.radians(48)
    p.drawLine(QPointF(12, 14), QPointF(12 + 6.0 * math.cos(a), 14 - 6.0 * math.sin(a)))
    p.setBrush(c)
    p.drawEllipse(QPointF(12, 14), 1.5, 1.5)


def _recirc(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    r = QRectF(4.5, 4.5, 15, 15)
    p.drawArc(r, 20 * 16, 140 * 16)
    p.drawArc(r, 200 * 16, 140 * 16)
    for ang, sign in ((160, 1), (340, 1)):
        a = math.radians(ang)
        x, y = 12 + 7.5 * math.cos(a), 12 - 7.5 * math.sin(a)
        t = math.radians(ang + 90 * sign)       # касательная — по ходу дуги
        dx, dy = math.cos(t), -math.sin(t)
        for side in (-1, 1):
            nx, ny = -dy * side, dx * side
            p.drawLine(QPointF(x, y), QPointF(x - 3.0 * dx + 2.2 * nx, y - 3.0 * dy + 2.2 * ny))


def _plan(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    path = QPainterPath(QPointF(3.5, 6))
    for x, y in ((8.5, 6), (8.5, 11), (13.5, 11), (13.5, 16), (19.5, 16)):
        path.lineTo(x, y)
    p.drawPath(path)
    p.drawLine(QPointF(3.5, 20.5), QPointF(20.5, 20.5))
    p.setBrush(c)
    p.drawEllipse(QPointF(19.5, 16), 1.4, 1.4)


def _weekend(p: QPainter, c: QColor) -> None:
    """Календарь: будни — точки, два дня выходных — залиты."""
    _pen(p, c)
    p.drawRoundedRect(QRectF(3.5, 5.0, 17, 15.5), 2.4, 2.4)
    p.drawLine(QPointF(3.5, 9.5), QPointF(20.5, 9.5))
    for x in (8.0, 16.0):
        p.drawLine(QPointF(x, 3.0), QPointF(x, 6.6))
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    for y in (13.2, 17.0):
        for x in (7.0, 10.5):
            p.drawEllipse(QPointF(x, y), 0.95, 0.95)
        for x in (14.0, 17.5):
            p.drawRoundedRect(QRectF(x - 1.45, y - 1.45, 2.9, 2.9), 0.6, 0.6)


def _quick(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    p.drawEllipse(QPointF(12, 13.5), 7.5, 7.5)
    p.drawLine(QPointF(12, 6), QPointF(12, 4))
    p.drawLine(QPointF(9.5, 3.2), QPointF(14.5, 3.2))
    p.drawLine(QPointF(12, 13.5), QPointF(15, 10.5))
    p.drawLine(QPointF(18.2, 7.2), QPointF(19.6, 5.8))


def _batches(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    for x, y in ((3.5, 12.5), (12.5, 12.5), (8, 4)):
        path = QPainterPath()
        path.addRoundedRect(QRectF(x, y, 8, 8), 2.2, 2.2)
        p.drawPath(path)
        p.drawLine(QPointF(x + 2.5, y + 2.6), QPointF(x + 5.5, y + 2.6))


def _data(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    path = QPainterPath(QPointF(7, 17.5))
    path.arcTo(QRectF(3.5, 10.5, 7, 7), 270, -180)
    path.arcTo(QRectF(6.5, 5.5, 10, 10), 160, -150)
    path.arcTo(QRectF(13.5, 9.5, 7, 7), 90, -180)
    path.closeSubpath()
    p.drawPath(path)
    p.drawLine(QPointF(9, 20.5), QPointF(9.8, 19.2))
    p.drawLine(QPointF(14, 20.5), QPointF(14.8, 19.2))


def _check(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
    p.drawEllipse(QPointF(12, 12), 4.8, 4.8)
    p.setBrush(c)
    p.drawEllipse(QPointF(12, 12), 1.4, 1.4)


def _settings(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    for y, x in ((6.5, 9.0), (12, 15.5), (17.5, 7.5)):
        p.drawLine(QPointF(3.5, y), QPointF(x - 2.2, y))
        p.drawLine(QPointF(x + 2.2, y), QPointF(20.5, y))
        p.drawEllipse(QPointF(x, y), 2.2, 2.2)


def _help(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
    path = QPainterPath(QPointF(9.4, 9.6))
    path.cubicTo(9.6, 6.9, 14.6, 6.8, 14.6, 9.6)
    path.cubicTo(14.6, 11.6, 12, 11.6, 12, 13.8)
    p.drawPath(path)
    p.setBrush(c)
    p.drawEllipse(QPointF(12, 16.8), 1.0, 1.0)


def _chev_right(p: QPainter, c: QColor) -> None:
    _pen(p, c, 2.0)
    path = QPainterPath(QPointF(9.5, 6.5))
    path.lineTo(15, 12)
    path.lineTo(9.5, 17.5)
    p.drawPath(path)


def _chev_down(p: QPainter, c: QColor) -> None:
    _pen(p, c, 2.0)
    path = QPainterPath(QPointF(6.5, 9.5))
    path.lineTo(12, 15)
    path.lineTo(17.5, 9.5)
    p.drawPath(path)


def _sun(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    p.drawEllipse(QPointF(12, 12), 4.2, 4.2)
    for k in range(8):
        a = math.radians(k * 45)
        p.drawLine(QPointF(12 + 7.0 * math.cos(a), 12 + 7.0 * math.sin(a)),
                   QPointF(12 + 9.2 * math.cos(a), 12 + 9.2 * math.sin(a)))


def _moon(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    path = QPainterPath(QPointF(15.5, 4.2))
    path.cubicTo(9.0, 4.0, 4.6, 9.2, 5.2, 14.2)
    path.cubicTo(5.8, 18.6, 10.6, 21.4, 15.2, 19.8)
    path.cubicTo(17.0, 19.2, 18.6, 18.0, 19.6, 16.4)
    path.cubicTo(13.8, 17.0, 10.0, 11.0, 15.5, 4.2)
    p.drawPath(path)


def _auto(p: QPainter, c: QColor) -> None:
    """Как в Windows: круг, наполовину залитый."""
    _pen(p, c)
    p.drawEllipse(QPointF(12, 12), 8.0, 8.0)
    half = QPainterPath(QPointF(12, 4))
    half.arcTo(QRectF(4, 4, 16, 16), 90, 180)
    half.closeSubpath()
    p.setBrush(c)
    p.drawPath(half)


def _kernel(p: QPainter, c: QColor) -> None:
    """Зерно кукурузы: широкая округлая «шапка», сужение к ножке; блик зародыша."""
    path = QPainterPath(QPointF(12, 21.5))
    path.cubicTo(8.0, 21.0, 4.0, 15.5, 4.2, 9.5)
    path.cubicTo(4.4, 4.6, 8.0, 2.5, 12, 2.5)
    path.cubicTo(16.0, 2.5, 19.6, 4.6, 19.8, 9.5)
    path.cubicTo(20.0, 15.5, 16.0, 21.0, 12, 21.5)
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    p.drawPath(path)
    germ = QPainterPath(QPointF(12, 17.8))
    germ.cubicTo(9.6, 16.6, 8.6, 12.8, 9.4, 9.8)
    germ.cubicTo(10.2, 7.4, 13.8, 7.4, 14.6, 9.8)
    germ.cubicTo(15.4, 12.8, 14.4, 16.6, 12, 17.8)
    p.setBrush(QColor(255, 255, 255, 120))
    p.drawPath(germ)


_DRAW: Dict[str, Callable[[QPainter, QColor], None]] = {
    "journal": _journal, "live": _live, "recirc": _recirc, "plan": _plan, "weekend": _weekend, "quick": _quick,
    "batches": _batches,
    "data": _data, "check": _check, "settings": _settings, "help": _help, "chev_right": _chev_right,
    "chev_down": _chev_down, "kernel": _kernel, "sun": _sun, "moon": _moon, "auto": _auto,
}


_CACHE: Dict[tuple, QPixmap] = {}


def pixmap(name: str, color, size: int = 20, dpr: float = 2.0) -> QPixmap:
    """Иконка заданного цвета и размера (в логических пикселях) для экрана с масштабом dpr. Готовые — из кэша:
    меню перерисовывает иконки при каждом наведении мыши."""
    key = (name, QColor(color).name(QColor.HexArgb), int(size), round(float(dpr), 2))
    pm = _CACHE.get(key)
    if pm is None:
        pm = _CACHE[key] = _render(name, color, size, dpr)
    return pm


def _render(name: str, color, size: int, dpr: float) -> QPixmap:
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 24.0, size / 24.0)
    _DRAW[name](p, QColor(color))
    p.end()
    return pm


def icon(name: str, color, size: int = 20, selected_color=None) -> QIcon:
    """Иконка; selected_color — цвет у выбранного пункта (QIcon.Selected)."""
    ic = QIcon()
    for dpr in (1.0, 2.0):
        ic.addPixmap(pixmap(name, color, size, dpr), QIcon.Normal)
        if selected_color is not None:
            ic.addPixmap(pixmap(name, selected_color, size, dpr), QIcon.Selected)
    return ic


def names():
    return sorted(_DRAW)
