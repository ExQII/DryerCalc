"""Графики программы на pyqtgraph: масштаб колесом мыши, сдвиг левой кнопкой, значения под курсором,
двойной щелчок — показать всё, правая кнопка — меню (показать всё, сохранить / скопировать картинку).

Интерфейс прежний (SimplePlot: add_line, add_points, add_hline, set_labels, set_title, include_zero, clear),
поэтому разделы программы не зависят от того, чем рисуется график. Цвета берутся из темы (светлая/тёмная) по
смыслу линии: «line» и «dot» (данные), «alt» (вторая серия), «target» (цель, предложенное), «ref» (фон-контекст).
Подписи серий — над графиком (ui_common.with_legend), а не словами о цветах в заголовке.
"""
from __future__ import annotations

import os
from typing import List, Optional, Sequence, Tuple

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")

import numpy as np  # noqa: E402
import pyqtgraph as pg  # noqa: E402
from PySide6.QtCore import QEvent, QSize, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QSizePolicy, QWidget  # noqa: E402

from .theme import theme_for  # noqa: E402

pg.setConfigOptions(antialias=True, useNumba=False, exitCleanup=False)


def fmt_num(v: float) -> str:
    """Число для подписи: до двух знаков после запятой, десятичная запятая."""
    if abs(v) >= 100:
        s = f"{v:.0f}"
    elif abs(v) >= 10:
        s = f"{v:.1f}"
    else:
        s = f"{v:.2f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.replace(".", ",").replace("-", "−")


class _Axis(pg.AxisItem):
    """Подписи делений с десятичной запятой."""

    def tickStrings(self, values, scale, spacing):  # noqa: N802
        return [s.replace(".", ",").replace("-", "−") for s in super().tickStrings(values, scale, spacing)]


class _Series:
    def __init__(self, kind: str, xs: Sequence[float], ys: Sequence[float], color: str, width: float, dashed: bool):
        self.kind, self.color, self.width, self.dashed = kind, color, width, dashed
        self.xs = np.asarray(list(xs), dtype=float)
        self.ys = np.asarray(list(ys), dtype=float)


class SimplePlot(pg.PlotWidget):
    """График с сеткой, подписями осей и подсказкой значений под курсором."""

    HELP = "Колесо мыши — масштаб, левая кнопка — сдвиг, двойной щелчок — показать всё, правая кнопка — меню."

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent, axisItems={"bottom": _Axis("bottom"), "left": _Axis("left")})
        # PlotWidget кладёт в экземпляр ссылку на PlotItem.clear — она перекрывала наш clear(): данные графика не
        # очищались, при каждом пересчёте серии и линии цели копились (старая цель оставалась на графике)
        self.__dict__.pop("clear", None)
        self._series: List[_Series] = []
        self._hlines: List[Tuple[float, str, bool]] = []
        self._title = ""
        self._xlabel = ""
        self._ylabel = ""
        self._zero_x = False
        self._zero_y = False
        self.legend_widget = None          # ui_common.with_legend: подписи серий над графиком
        self._empty_text = "Пока нечего показать"
        pi = self.getPlotItem()
        pi.setMenuEnabled(False)          # своё меню на русском вместо английского меню pyqtgraph
        pi.hideButtons()
        self.vb = pi.getViewBox()
        self.vb.setMenuEnabled(False)
        pi.showGrid(x=True, y=True, alpha=0.13)       # сетка — тихая, данные — громкие
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(260, 170)
        self._marker = pg.ScatterPlotItem(size=12, symbol="o")
        self._tip = pg.TextItem(anchor=(0, 1))
        self._redraw_timer = QTimer(self)
        self._redraw_timer.setSingleShot(True)
        self._redraw_timer.setInterval(0)
        self._redraw_timer.timeout.connect(self._redraw)
        self.scene().sigMouseMoved.connect(self._on_move)
        self.scene().sigMouseClicked.connect(self._on_click)
        self._apply_theme()

    # --- API (как у прежнего SimplePlot) --------------------------------
    def clear(self) -> None:  # noqa: D401 — совместимость: очищает данные графика
        self._series.clear()
        self._hlines.clear()
        self._title = ""
        self._schedule()

    def set_labels(self, x_label: str, y_label: str) -> None:
        self._xlabel, self._ylabel = x_label, y_label
        self._apply_labels()

    def set_title(self, text: str) -> None:
        self._title = text
        self._apply_labels()

    def set_empty_text(self, text: str) -> None:
        """Надпись на пустом графике (что сделать, чтобы он появился)."""
        self._empty_text = text
        self._schedule()

    def set_legend(self, items) -> None:
        """Подписи серий: [(вид "dot" | "line" | "dash", роль цвета, подпись)] — если график обёрнут with_legend."""
        if self.legend_widget is not None:
            self.legend_widget.set_items(items)

    def include_zero(self, x: bool = False, y: bool = False) -> None:
        self._zero_x, self._zero_y = x, y
        self._schedule()

    def add_line(self, xs: Sequence[float], ys: Sequence[float], color: str = "line", width: float = 2.0,
                 dashed: bool = False) -> None:
        self._series.append(_Series("line", xs, ys, color, width, dashed))
        self._schedule()

    def add_points(self, xs: Sequence[float], ys: Sequence[float], color: str = "dot", size: float = 6.0) -> None:
        self._series.append(_Series("points", xs, ys, color, size, False))
        self._schedule()

    def add_hline(self, y: float, color: str = "target", dashed: bool = True) -> None:
        self._hlines.append((float(y), color, dashed))
        self._schedule()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(520, 300)

    def show_all(self) -> None:
        """Вернуть масштаб по данным (как до масштабирования мышью)."""
        x0, x1, y0, y1 = self._data_range()
        # по оси X только целые (номер прохода) — и деления целые
        xs = [s.xs for s in self._series if len(s.xs)]
        ax = self.getPlotItem().getAxis("bottom")
        if xs and all(np.all(np.mod(a, 1.0) == 0) for a in xs) and 0 < x1 - x0 <= 40:
            step = max(1, int(np.ceil((x1 - x0) / 10)))
            ax.setTickSpacing(major=step, minor=step)
        else:
            ax.setTickSpacing()
        self.vb.setRange(xRange=(x0, x1), yRange=(y0, y1), padding=0.0)

    # --- оформление -----------------------------------------------------
    def _schedule(self) -> None:
        self._redraw_timer.start()

    def _apply_theme(self) -> None:
        th = theme_for(self)
        self.setBackground(th.plot_bg)
        pi = self.getPlotItem()
        font = self.font()
        for name in ("bottom", "left"):
            ax = pi.getAxis(name)
            ax.setPen(pg.mkPen(th.plot_ink, width=1))
            ax.setTextPen(pg.mkPen(th.plot_ink))
            ax.setStyle(tickFont=font, maxTickLevel=1)
        self._tip.fill = pg.mkBrush(QColor(th.plot_bg.red(), th.plot_bg.green(), th.plot_bg.blue(), 230))
        self._tip.border = pg.mkPen(th.plot_grid)
        self._tip.setColor(th.plot_ink)
        self._apply_labels()
        self._redraw()

    def _apply_labels(self) -> None:
        th = theme_for(self)
        pi = self.getPlotItem()
        pt = max(self.font().pointSizeF(), 7.0)
        style = {"color": th.plot_ink.name(), "font-size": f"{pt:.0f}pt"}
        room = {"bottom": max(self.vb.width(), 60), "left": max(self.vb.height(), 60)}
        for side, text in (("bottom", self._xlabel), ("left", self._ylabel)):
            ax = pi.getAxis(side)
            if text:
                # длинная подпись не обрезается краем, а сокращается с «…» (полная — в подсказке под курсором)
                self._fit(text, room[side] - 6, lambda t, s=side: pi.setLabel(s, t, **style),
                          lambda a=ax: a.label.boundingRect().width())
                ax.resizeEvent(None)        # pyqtgraph ставит подпись по старому размеру текста — пересчитать
            else:
                ax.showLabel(False)
        if self._title:
            self._fit(self._title, max(self.vb.width() - 6, 80),          # заголовок — над областью графика
                      lambda t: pi.setTitle(t, color=th.plot_ink.name(), size=f"{pt:.0f}pt"),
                      lambda: pi.titleLabel.item.boundingRect().width())
        else:
            pi.setTitle(None)

    @staticmethod
    def _fit(text: str, avail: float, apply, width) -> None:
        """Показать text целиком или самое длинное начало с «…», которое помещается в avail пикселей
        (ширина меряется по самому элементу pyqtgraph — HTML рисуется не тем шрифтом, что у виджета)."""
        apply(text)
        if width() <= avail:
            return
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            apply(text[:mid].rstrip() + "…")
            if width() <= avail:
                lo = mid
            else:
                hi = mid - 1
        apply(text[:lo].rstrip() + "…")

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        if hasattr(self, "_series"):
            QTimer.singleShot(0, self._apply_labels)

    def changeEvent(self, ev) -> None:  # noqa: N802 — тема Windows или шрифт поменялись на ходу
        super().changeEvent(ev)
        if hasattr(self, "_series") and ev.type() in (QEvent.PaletteChange, QEvent.StyleChange, QEvent.FontChange):
            self._apply_theme()

    def _data_range(self) -> Tuple[float, float, float, float]:
        xs = [s.xs[np.isfinite(s.xs)] for s in self._series if len(s.xs)]
        ys = [s.ys[np.isfinite(s.ys)] for s in self._series if len(s.ys)]
        xs = np.concatenate(xs) if xs else np.array([])
        ys = np.concatenate(ys + [np.array([h[0] for h in self._hlines])]) if (ys or self._hlines) else np.array([])
        x0, x1 = (float(xs.min()), float(xs.max())) if xs.size else (0.0, 1.0)
        y0, y1 = (float(ys.min()), float(ys.max())) if ys.size else (0.0, 1.0)
        if self._zero_x:
            x0 = min(x0, 0.0)
        if self._zero_y:
            y0 = min(y0, 0.0)
        if x1 - x0 < 1e-9:
            x1 = x0 + 1.0
        if y1 - y0 < 1e-9:
            y1 = y0 + 1.0
        pad_y = 0.05 * (y1 - y0)
        pad_x = 0.0 if self._zero_x else 0.05 * (x1 - x0)
        y_lo = y0 if (self._zero_y and y0 == 0.0) else y0 - pad_y
        return x0 - pad_x, x1 + pad_x, y_lo, y1 + pad_y

    def _redraw(self) -> None:
        th = theme_for(self)
        pi = self.getPlotItem()
        pi.clear()
        for yv, color, dashed in self._hlines:
            pen = pg.mkPen(th.role(color), width=1.5)
            if dashed:
                pen.setStyle(Qt.DashLine)
            pi.addItem(pg.InfiniteLine(pos=yv, angle=0, pen=pen))
        for s in self._series:
            if s.kind == "line" and len(s.xs) >= 2:
                pen = pg.mkPen(th.role(s.color), width=s.width)
                pen.setStyle(Qt.DashLine if s.dashed else Qt.SolidLine)
                pen.setJoinStyle(Qt.RoundJoin)
                pen.setCapStyle(Qt.RoundCap)
                pi.addItem(pg.PlotCurveItem(s.xs, s.ys, pen=pen))
            elif s.kind == "points" and len(s.xs):
                pi.addItem(pg.ScatterPlotItem(s.xs, s.ys, size=s.width, symbol="o",
                                              pen=pg.mkPen(th.plot_bg, width=0.8), brush=pg.mkBrush(th.role(s.color))))
        if not self._series and not self._hlines and self._empty_text:
            empty = pg.TextItem(self._empty_text, color=QColor(th.t.muted), anchor=(0.5, 0.5))
            empty.setFont(self.font())
            empty.setPos(0.5, 0.5)
            pi.addItem(empty, ignoreBounds=True)
        self._marker.setPen(pg.mkPen(th.plot_ink, width=1.5))
        self._marker.setBrush(pg.mkBrush(None))
        self._marker.setData([], [])
        self._tip.setVisible(False)
        pi.addItem(self._marker, ignoreBounds=True)
        pi.addItem(self._tip, ignoreBounds=True)
        self._marker.setZValue(100)
        self._tip.setZValue(101)
        self.show_all()

    # --- мышь -----------------------------------------------------------
    def _on_move(self, pos) -> None:
        if not self._series or not self.vb.sceneBoundingRect().contains(pos):
            self._marker.setData([], [])
            self._tip.setVisible(False)
            return
        p = self.vb.mapSceneToView(pos)
        pxw, pxh = self.vb.viewPixelSize()
        if not pxw or not pxh:
            return
        best = None
        for s in self._series:
            if not len(s.xs):
                continue
            d2 = ((s.xs - p.x()) / pxw) ** 2 + ((s.ys - p.y()) / pxh) ** 2
            i = int(np.nanargmin(d2)) if np.isfinite(d2).any() else -1
            if i >= 0 and (best is None or d2[i] < best[0]):
                best = (float(d2[i]), float(s.xs[i]), float(s.ys[i]))
        if best is None or best[0] > 22.0 ** 2:
            self._marker.setData([], [])
            self._tip.setVisible(False)
            return
        _, x, y = best
        self._marker.setData([x], [y])
        xl = self._xlabel or "x"
        yl = self._ylabel or "y"
        self._tip.setText(f"{yl}: {fmt_num(y)}\n{xl}: {fmt_num(x)}")
        # подсказка — с той стороны от точки, где больше места
        (vx0, vx1), (vy0, vy1) = self.vb.viewRange()
        self._tip.setAnchor((1.0 if x > 0.5 * (vx0 + vx1) else 0.0, 0.0 if y > 0.5 * (vy0 + vy1) else 1.0))
        self._tip.setPos(x, y)
        self._tip.setVisible(True)

    def _on_click(self, ev) -> None:
        if ev.button() == Qt.LeftButton and ev.double():
            self.show_all()
            ev.accept()
        elif ev.button() == Qt.RightButton:
            ev.accept()
            self._menu(ev.screenPos().toPoint())

    def _menu(self, global_pos) -> None:
        m = QMenu(self)
        m.addAction("Показать всё", self.show_all)
        m.addSeparator()
        m.addAction("Сохранить картинку (PNG)…", self._save_png)
        m.addAction("Копировать картинку", lambda: QApplication.clipboard().setPixmap(self.grab()))
        m.addSeparator()
        hint = m.addAction(self.HELP)
        hint.setEnabled(False)
        m.exec(global_pos)

    def _save_png(self) -> None:
        name = (self._title or self._ylabel or "график").split(";")[0].split(":")[0].strip()[:40] or "график"
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить график", f"{name}.png", "Картинка PNG (*.png)")
        if path:
            self._marker.setData([], [])
            self._tip.setVisible(False)
            self.grab().save(path, "PNG")

    def pen_for(self, role: str, width: float = 2.0) -> QPen:
        return pg.mkPen(theme_for(self).role(role), width=width)
