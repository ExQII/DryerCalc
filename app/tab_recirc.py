"""Раздел «Досушка» (рециркуляция): досушка содержимого сушилки по кругу (без подачи из силоса)."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QDateTimeEdit, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton,
                               QSplitter, QVBoxLayout, QWidget)

from . import model as mdl
from .plots import SimplePlot
from .theme import theme_for
from .ui_common import Collapsible, dspin, fmt_hours, item, mute, scroll_wrap, set_role, table, with_legend

if TYPE_CHECKING:
    from .gui import MainWindow


class RecircTab(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        lay = QHBoxLayout(self)
        left = QWidget()
        lv = QVBoxLayout(left)
        gb = QGroupBox("Что в сушилке")
        f = QFormLayout(gb)
        self.mass = dspin(1, 500, win.params.m_dryer, 1, 1, " т")
        self.w0 = dspin(5, 45, 17, 1, 0.5, " %")
        self.wk = dspin(5, 30, 14, 1, 0.5, " %")
        self.tproc = dspin(20, 150, 60, 1, 5, " °C")
        self.tamb = dspin(-40, 45, 10, 1, 1, " °C")
        self.rh = dspin(5, 100, 70, 0, 5, " %")
        self.start = QDateTimeEdit(QDateTime.currentDateTime())
        self.start.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.start.setCalendarPopup(True)
        self.cool = QCheckBox("Потом охлаждение на D = 100 (горелки выкл.)")
        self.cool.setChecked(True)
        b_j = set_role(QPushButton("Взять из журнала (последние пробы)"), "link")
        b_j.setCursor(Qt.PointingHandCursor)
        b_j.clicked.connect(self._from_journal)
        b_c = set_role(QPushButton("Взять T блоков и воздух из «План сушки партии»"), "link")
        b_c.setCursor(Qt.PointingHandCursor)
        b_c.clicked.connect(self._from_calc)
        f.addRow("Масса в сушилке", self.mass)
        f.addRow("Влажность сейчас (средняя, после охл.)", self.w0)
        f.addRow(b_j)
        f.addRow("Цель (после охлаждения)", self.wk)
        f.addRow("T блоков", self.tproc)
        f.addRow("Наружный воздух, T", self.tamb)
        f.addRow("Наружный воздух, RH", self.rh)
        f.addRow(b_c)
        f.addRow("Начало рециркуляции", self.start)
        f.addRow(self.cool)
        lv.addWidget(gb)
        self.hint_box = Collapsible(
            "Как считается досушка",
            "Рециркуляция — зерно ходит по кругу через шахту без подачи из силоса: досушить то, что осталось в "
            "сушилке после окончания подачи, или высушить небольшую партию (не больше вместимости). Шахта должна "
            "быть полной, иначе воздух идёт мимо зерна.<br><br>Средняя влажность в сушилке в конце прохода ≈ "
            "середина между входом и выходом. Расчёт: эмпирическая модель — та же скорость съёма влаги R(W), что и "
            "в проходе, при W = средней по сушилке; физическая — кривая сушки по накопленному времени в горячей "
            "зоне.<br><br>В журнале октября рециркуляция шла 1,5 ч, и её результат на выходе ещё не был виден "
            "(зерно проходит шахту ≈ 3 ч), поэтому для этого режима модель пока не проверена — записывайте пробы "
            "во время рециркуляции.")
        self.hint = self.hint_box.content
        lv.addWidget(self.hint_box)
        lv.addStretch(1)
        left.setMaximumWidth(470)

        right = QSplitter(Qt.Vertical)
        top = QWidget()
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 0, 0, 0)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.RichText)
        tv.addWidget(self.summary)
        self.more = mute(QLabel())
        self.more.setWordWrap(True)
        self.more.setTextFormat(Qt.RichText)
        self.more_box = Collapsible("Подробнее: вторая модель, вода, показание влагомера", self.more)
        tv.addWidget(self.more_box)
        self.tbl = table(["Время", "Часов", "W эмпир., %", "W физ., %", "Влагомер покажет (эмпир.), %"])
        tv.addWidget(self.tbl)
        right.addWidget(top)
        self.plot = SimplePlot()
        self.plot.set_labels("часы рециркуляции", "влажность (после охлаждения), %")
        self.plot.include_zero(x=True)
        right.addWidget(with_legend(self.plot, (("line", "line", "модель для расчётов"), ("dash", "ref", "вторая модель"),
                                                ("dash", "target", "цель"))))
        right.setSizes([380, 380])
        lay.addWidget(scroll_wrap(left, 500), 0)
        lay.addWidget(right, 1)
        for w in (self.mass, self.w0, self.wk, self.tproc, self.tamb, self.rh):
            w.valueChanged.connect(self.recalc)
        self.start.dateTimeChanged.connect(self.recalc)
        self.cool.stateChanged.connect(self.recalc)

    def _from_journal(self) -> None:
        from .live import live_state
        p = self.win.params
        st = live_state(self.win.journal_rows(), p, weather=self.win.weather_series())
        if st.w_out_read is None:
            self.summary.setText("В журнале нет проб выхода.")
            return
        w_out_true = mdl.true_from_read(p, st.w_out_read, st.t_grain)
        w = 0.5 * (st.w_in + w_out_true) if st.w_in is not None else w_out_true
        self.w0.setValue(round(w, 1))
        if st.t_proc is not None:
            self.tproc.setValue(st.t_proc)
        if st.t_amb is not None:
            self.tamb.setValue(st.t_amb)
        if st.rh is not None:
            self.rh.setValue(st.rh)
        if st.last is not None:
            self.start.setDateTime(st.last)

    def _from_calc(self) -> None:
        c = self.win.conditions()
        self.tproc.setValue(c.t_proc)
        self.tamb.setValue(c.t_amb)
        self.rh.setValue(c.rh)

    def on_show(self) -> None:
        if abs(self.mass.value() - self.win.params.m_dryer) > 1e-9 and not getattr(self, "_touched", False):
            self.mass.setValue(self.win.params.m_dryer)
        self.recalc()

    def recalc(self) -> None:
        th = theme_for(self)
        mute(self.hint)
        self.summary.setStyleSheet(f"background:{th.summary_bg}; color:{th.summary_fg}; border-radius:10px; "
                                   f"padding:10px 12px; font-size:{QApplication.font().pointSizeF() + 2:.1f}pt;")
        p = self.win.params
        c = mdl.Conditions(self.tproc.value(), self.tamb.value(), self.rh.value())
        m, w0, wk = self.mass.value(), self.w0.value(), self.wk.value()
        start: datetime = self.start.dateTime().toPython()
        self.tbl.setRowCount(0)
        self.plot.clear()
        self.more_box.setVisible(False)
        if w0 <= wk:
            self.summary.setText("Влажность уже не выше цели — досушивать не нужно.")
            return
        active = "physics" if p.model_kind == "physics" else "empirical"
        res = {k: mdl.recirc(replace(p, model_kind=k), c, m, w0, wk) for k in ("empirical", "physics")}
        main = res[active]
        # честный допуск: проверка на журнале + разброс коэффициентов + расхождение моделей сверх обычного
        other_k = "physics" if active == "empirical" else "empirical"
        s_par = mdl.param_sigma(p, 0.5 * (w0 + wk), c.t_proc, c.t_amb, c.rh)
        h_main, h_other = res[active].hours, res[other_k].hours
        s_str = (0.5 * max(abs(h_other - h_main) / h_main - p.struct_base, 0.0)
                 if h_main and h_other else 0.0)          # относит. расхождение времени ≈ расхождению скорости
        sig = (p.rate_sigma ** 2 + s_par ** 2 + s_str ** 2) ** 0.5
        self.sigma_info = {"check": p.rate_sigma, "param": s_par, "struct": s_str,
                           "flags": mdl.explored_flags(p, [], [c.t_proc], [w0])}
        lo_hi = []
        for k in (1.0 + mdl.Z90 * sig, 1.0 - mdl.Z90 * sig):
            r = mdl.recirc(mdl.scaled(replace(p, model_kind=active), k), c, m, w0, wk)
            lo_hi.append(r.hours)
        cool_h = (main.m_end / mdl.q_full(p)) if self.cool.isChecked() and mdl.q_full(p) > 0 else 0.0
        lines = []
        more: list = []                       # второстепенное — под «Подробнее», чтобы итог читался с одного взгляда
        name = {"empirical": "эмпирической", "physics": "физической"}
        other = "physics" if active == "empirical" else "empirical"
        if main.hours is None:
            lines.append(f"<span style='color:{th.error}'>За 48 ч до {wk:g} % не досушить: {main.note}.</span>")
        else:
            rng = ""
            if all(x is not None for x in lo_hi):
                rng = f" (90 %: {min(lo_hi):.1f}–{max(lo_hi):.1f} ч)"
            end = start + timedelta(hours=main.hours)
            lines.append(f"Досушка {w0:g} → {wk:g} %: <b>{fmt_hours(main.hours)}</b>{rng} по {name[active]} модели — "
                         f"к <b>{end:%d.%m %H:%M}</b>.")
            self.summary.setToolTip(mdl.sigma_text(self.sigma_info))
            oh = res[other].hours
            more.append(f"По {name[other]} модели: " + (fmt_hours(oh) if oh is not None else "не досушить за 48 ч") + ".")
            more.append(f"Испарится ≈ {main.water:.1f} т воды; масса станет {main.m_end:.1f} т.")
            more.append(f"Влагомер на выходе покажет ≈ {mdl.read_from_true(p, wk):.1f} %.")
            if cool_h:
                lines.append(f"Затем охлаждение ≈ {cool_h:.1f} ч (D = 100, горелки выкл.) — готово к "
                             f"<b>{end + timedelta(hours=cool_h):%d.%m %H:%M}</b>.")
        if main.note and main.hours is not None:
            lines.append(f"<span style='color:{th.error}'>⚠ {main.note}.</span>")
        self.summary.setText("<br>".join(lines))
        self.more.setText("<br>".join(more))
        self.more_box.setVisible(bool(more))
        # таблица по часам
        horizon = max([r.points[-1][0] for r in res.values()] + [1.0])
        hours = [h * 0.5 for h in range(0, int(horizon / 0.5) + 2)]

        def w_at(r, h):
            pts = r.points
            if h >= pts[-1][0]:
                return pts[-1][1] if r.hours is None or h <= r.hours + 1e-9 else None
            for (a, wa), (b, wb) in zip(pts, pts[1:]):
                if a <= h <= b:
                    return wa + (wb - wa) * (h - a) / (b - a) if b > a else wb
            return None
        self.tbl.setRowCount(len(hours))
        for i, h in enumerate(hours):
            we, wp = w_at(res["empirical"], h), w_at(res["physics"], h)
            cells = [f"{start + timedelta(hours=h):%d.%m %H:%M}", f"{h:.1f}", "—" if we is None else f"{we:.1f}",
                     "—" if wp is None else f"{wp:.1f}", "—" if we is None else f"{mdl.read_from_true(p, we):.1f}"]
            for j, txt in enumerate(cells):
                self.tbl.setItem(i, j, item(txt, align_right=j > 0))
        for k, role in (("empirical", "line"), ("physics", "ref")):
            pts = res[k].points
            self.plot.add_line([a for a, _ in pts], [b for _, b in pts], role, 2.5 if k == active else 1.5,
                               dashed=(k != active))
        self.plot.add_hline(wk, "target")
        self.plot.set_title("")
