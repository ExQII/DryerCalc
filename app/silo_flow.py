"""Схема плана размещения по силосам: шаг за шагом — четыре силоса с уровнем зерна и стрелка «откуда → через
сушилку → куда».

Каждая строка — одно переключение маршрута. Силосы нарисованы после шага: высота заливки — доля вместимости, цвет —
сколько проходов прошло зерно (от сырого к готовому). В силосе-источнике пунктиром показано, сколько из него ушло;
силос-приёмник обведён. Перед шагами каждого прохода — полоса с режимом прохода (влажность, D). Первая строка —
приём сырого зерна с поля. Данные — silos.SiloPlan; цвета — токены оформления (светлая и тёмная тема).
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

from .silos import EPS, Silo, SiloPlan
from .theme import mix, theme_for

LEFT = 176              # колонка с описанием шага
ROW_H = 142             # строка шага
BAND_H = 30             # полоса «N-й проход»
LEGEND_H = 34
PAD = 12


def _t(m: float) -> str:
    return f"{m:,.0f}".replace(",", " ")


class SiloFlow(QWidget):
    def __init__(self):
        super().__init__()
        self.plan: Optional[SiloPlan] = None
        self.silos: List[Silo] = []
        self.passes: Sequence = ()
        self.empty_text = "Здесь появится схема: откуда и куда идёт зерно на каждом шаге."
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.setMinimumHeight(200)

    def set_plan(self, plan: Optional[SiloPlan], silos: Sequence[Silo], passes: Sequence = ()) -> None:
        """plan — результат silos.plan_silos (None — пусто); passes — проходы плана сушки (model.PassResult)."""
        self.plan = plan if plan is not None and (plan.steps or plan.placement) else None
        self.silos = list(silos)
        self.passes = passes
        self.setMinimumHeight(self._height())
        self.updateGeometry()
        self.update()

    # ------------------------------------------------------------- размеры
    def _rows(self) -> List[tuple]:
        """[("intake", None) | ("band", k) | ("step", Step)] сверху вниз."""
        if self.plan is None:
            return []
        rows: List[tuple] = []
        if self.plan.placement:
            rows.append(("intake", None))
        last = None
        for s in self.plan.steps:
            if s.k != last:
                rows.append(("band", s.k))
                last = s.k
            rows.append(("step", s))
        return rows

    def _height(self) -> int:
        h = LEGEND_H + PAD
        for kind, _x in self._rows():
            h += BAND_H if kind == "band" else ROW_H
        return max(h + PAD, 200)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(640, self._height())

    def save_png(self, path: str, width: int = 1100) -> bool:
        """Вся схема одной картинкой (для печати и передачи сушильщикам), шириной width пикселей."""
        from PySide6.QtGui import QPixmap
        old = self.size()
        self.resize(width, self._height())
        pm = QPixmap(self.size())
        pm.fill(QColor(theme_for(self).t.surface))
        self.render(pm)
        self.resize(old)
        return pm.save(path, "PNG")

    # ------------------------------------------------------------- рисование
    def _grain(self, th, done: int, n: int) -> QColor:
        """Цвет зерна: сырое — стальной, готовое — золото, между ними — по числу пройденных проходов."""
        wet, half, dry = QColor(th.t.steel), QColor(th.t.good), QColor(th.t.accent)
        pos = min(done, n) / max(n, 1)                  # через зелёный: прямое смешение синего с золотом даёт серый
        return mix(wet, half, pos * 2.0) if pos < 0.5 else mix(half, dry, (pos - 0.5) * 2.0)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        th = theme_for(self)
        t = th.t
        w = self.width()
        ink, ink2, muted, line = QColor(t.ink), QColor(t.ink2), QColor(t.muted), QColor(t.line_strong)
        base = QFont(self.font())
        bold = QFont(base)
        bold.setBold(True)
        small = QFont(base)
        small.setPointSizeF(base.pointSizeF() * 0.92)
        big = QFont(bold)
        big.setPointSizeF(base.pointSizeF() * 1.25)
        fm = QFontMetrics(base)
        if self.plan is None:
            p.setPen(muted)
            p.setFont(base)
            p.drawText(QRectF(0, 0, w, self.height()), Qt.AlignCenter | Qt.TextWordWrap, self.empty_text)
            p.end()
            return
        n = max((d for _m, d in self.plan.final), default=1) or 1
        n = max(n, len(self.passes) or 1)
        ns = len(self.silos)
        col_w = (w - LEFT - PAD) / max(ns, 1)
        body_w = min(86.0, col_w * 0.62)
        cx = [LEFT + col_w * (i + 0.5) for i in range(ns)]

        # легенда: цвет — сколько проходов прошло зерно
        x = float(PAD)
        p.setFont(small)
        for d in range(n + 1):
            p.setPen(Qt.NoPen)
            p.setBrush(self._grain(th, d, n))
            p.drawRoundedRect(QRectF(x, 10, 16, 14), 3, 3)
            txt = "сырое" if d == 0 else ("готовое" if d == n else f"после {d}-го прохода")
            p.setPen(ink2)
            p.drawText(QPointF(x + 22, 22), txt)
            x += 22 + QFontMetrics(small).horizontalAdvance(txt) + 18
        y = float(LEGEND_H + PAD)
        prev_state = [(s.mass, s.done if s.mass > EPS else 0) for s in self.silos]

        def silo(i: int, top: float, state, was=None, accent: bool = False) -> None:
            """Силос i в строке с верхом top: уровень state=(масса, проходов); was — масса до шага (ушедшее пунктиром)."""
            s = self.silos[i]
            m, d = state
            x0 = cx[i] - body_w / 2
            roof, body_top, body_h = top + 36.0, top + 48.0, 52.0
            path = QPainterPath()
            path.moveTo(x0, body_top)
            path.lineTo(cx[i], roof)
            path.lineTo(x0 + body_w, body_top)
            path.lineTo(x0 + body_w, body_top + body_h)
            path.lineTo(x0, body_top + body_h)
            path.closeSubpath()
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(t.surface_alt if hasattr(t, "surface_alt") else t.paper))
            p.drawPath(path)
            if s.intake:
                p.setBrush(QColor(t.line_soft))
                p.drawPath(path)
            cap = max(s.cap, 1.0)
            if m > EPS and not s.intake:
                fh = body_h * min(m / cap, 1.0)
                p.setBrush(self._grain(th, d, n))
                p.drawRect(QRectF(x0 + 1, body_top + body_h - fh, body_w - 2, fh))
            if was is not None and was - m > EPS and not s.intake:          # сколько ушло из источника
                h0, h1 = body_h * min(was / cap, 1.0), body_h * min(max(m, 0.0) / cap, 1.0)
                pen = QPen(muted, 1.2, Qt.DashLine)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawRect(QRectF(x0 + 4, body_top + body_h - h0, body_w - 8, h0 - h1))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(t.accent_edge) if accent else line, 2.4 if accent else 1.4))
            p.drawPath(path)
            p.setFont(bold)
            p.setPen(ink)
            p.drawText(QRectF(cx[i] - col_w / 2, body_top + body_h + 4, col_w, fm.height()), Qt.AlignHCenter, s.name)
            p.setFont(small)
            p.setPen(ink2 if (m > EPS or s.intake) else muted)
            if s.intake:
                txt = "приёмка с поля"
            elif m <= EPS:
                txt = "пусто"
            else:
                txt = f"{_t(m)} т · " + ("сырое" if d == 0 else ("готовое" if d >= n else f"{d} пр."))
            p.drawText(QRectF(cx[i] - col_w / 2, body_top + body_h + 4 + fm.height(), col_w, fm.height()),
                       Qt.AlignHCenter, txt)

        def arrow_head(x_: float, y_: float, color: QColor) -> None:
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawPolygon(QPolygonF([QPointF(x_, y_), QPointF(x_ - 6, y_ - 10), QPointF(x_ + 6, y_ - 10)]))

        for kind, obj in self._rows():
            if kind == "band":
                k = obj
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(t.steel_tint))
                p.drawRoundedRect(QRectF(PAD, y + 4, w - 2 * PAD, BAND_H - 8), 8, 8)
                txt = f"{k}-й проход"
                if len(self.passes) >= k:
                    ps = self.passes[k - 1]
                    txt += (f"   ·   влажность {ps.w_in:.1f} → {ps.w_out:.1f} %   ·   D = {ps.d:.0f} с   ·   "
                            f"{ps.q:.0f} т/ч").replace(".", ",")
                p.setFont(bold)
                p.setPen(ink)
                p.drawText(QRectF(PAD + 12, y + 4, w - 2 * PAD - 24, BAND_H - 8), Qt.AlignVCenter | Qt.AlignLeft, txt)
                y += BAND_H
                continue
            p.setPen(QPen(QColor(t.line_soft), 1))
            p.drawLine(QPointF(PAD, y + ROW_H - 1), QPointF(w - PAD, y + ROW_H - 1))
            lane = y + 16.0
            if kind == "intake":
                state = self.plan.start_state
                put = dict(self.plan.placement)
                for i in range(ns):
                    silo(i, y, state[i], accent=i in put)
                col = QColor(t.steel)
                for i, m in put.items():
                    p.setPen(QPen(col, 2.2))
                    p.drawLine(QPointF(cx[i], y + 6), QPointF(cx[i], y + 32))
                    arrow_head(cx[i], y + 36, col)
                    p.setFont(small)
                    p.setPen(ink2)
                    p.drawText(QPointF(cx[i] + 10, y + 20), f"+{_t(m)} т")
                p.setFont(big)
                p.setPen(ink)
                p.drawText(QRectF(PAD, y + 30, LEFT - PAD, 26), Qt.AlignLeft | Qt.AlignVCenter, "Приём с поля")
                p.setFont(base)
                p.setPen(ink2)
                p.drawText(QRectF(PAD, y + 58, LEFT - PAD - 6, 44), Qt.AlignLeft | Qt.TextWordWrap,
                           f"{_t(sum(put.values()))} т сырого зерна")
                prev_state = list(state)
                y += ROW_H
                continue
            s = obj
            idx = self.plan.steps.index(s) + 1
            for i in range(ns):
                silo(i, y, s.after[i], was=prev_state[i][0] if i == s.src else None, accent=i == s.dst)
            # стрелка: из источника вверх, по дорожке через сушилку, вниз в приёмник
            col = QColor(t.ink2)
            p.setPen(QPen(col, 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            path = QPainterPath()
            path.moveTo(cx[s.src], y + 36)
            path.lineTo(cx[s.src], lane + 8)
            path.quadTo(cx[s.src], lane, cx[s.src] + (8 if s.dst > s.src else -8), lane)
            path.lineTo(cx[s.dst] - (8 if s.dst > s.src else -8), lane)
            path.quadTo(cx[s.dst], lane, cx[s.dst], lane + 8)
            path.lineTo(cx[s.dst], y + 28)
            p.drawPath(path)
            arrow_head(cx[s.dst], y + 36, col)
            # сушилка — плашка посреди дорожки
            p.setFont(small)
            label = "сушилка"
            bw = QFontMetrics(small).horizontalAdvance(label) + 20
            mid = (cx[s.src] + cx[s.dst]) / 2
            box = QRectF(mid - bw / 2, lane - 11, bw, 22)
            p.setPen(QPen(QColor(t.accent_edge), 1.2))
            p.setBrush(QColor(t.accent_tint))
            p.drawRoundedRect(box, 11, 11)
            p.setPen(QColor(t.amber_ink))
            p.drawText(box, Qt.AlignCenter, label)
            # слева: номер шага, маршрут, тонны, время
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(t.accent))
            p.drawEllipse(QRectF(PAD, y + 26, 28, 28))
            p.setFont(bold)
            p.setPen(QColor(t.accent_ink))
            p.drawText(QRectF(PAD, y + 26, 28, 28), Qt.AlignCenter, str(idx))
            p.setFont(big)
            p.setPen(ink)
            p.drawText(QRectF(PAD + 36, y + 26, LEFT - PAD - 36, 28), Qt.AlignLeft | Qt.AlignVCenter,
                       f"{self.silos[s.src].name} → {self.silos[s.dst].name}")
            p.setFont(base)
            p.setPen(ink2)
            lines = [f"{_t(s.m_in)} т → {_t(s.m_out)} т" + (" (часть)" if s.split else "")]
            if s.start and s.end:
                lines.append(f"{s.start:%d.%m %H:%M} – {s.end:%d.%m %H:%M}")
                lines.append(f"{s.hours:.0f} ч")
            p.drawText(QRectF(PAD, y + 60, LEFT - PAD - 6, 70), Qt.AlignLeft | Qt.TextWordWrap, "\n".join(lines))
            prev_state = list(s.after)
            y += ROW_H
        p.end()
