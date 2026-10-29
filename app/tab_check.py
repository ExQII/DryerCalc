"""Вкладка «Проверка и опыты»: прогноз против факта по проходам журнала, сравнение моделей, дрейф,
допуск прогнозов и план опытов (какие режимы поставить, чтобы модель училась быстрее)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import (QApplication, QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton,
                               QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget)

from .analysis import backtest, drift_text, rate_sigma_from_backtest
from .doe import explored_passes, suggest, typical_regime
from .plots import SimplePlot
from .theme import theme_for
from .ui_common import dspin, fnum, item, scroll_wrap, table

if TYPE_CHECKING:
    from .gui import MainWindow


class CheckTab(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self._dirty = True
        self._doe_dirty = True
        tabs = QTabWidget()
        self.sub = tabs
        QVBoxLayout(self).addWidget(tabs)
        tabs.addTab(self._build_backtest(), "Прогноз против факта")
        tabs.addTab(self._build_doe(), "План опытов")
        tabs.currentChanged.connect(lambda _i: self.on_show())
        cfg = self.win.storage.get_json("calib_cfg", {}) or {}
        self.half.setValue(float(cfg.get("half_life", 0.0)))
        self.cb_auto.setChecked(bool(cfg.get("auto", False)))
        self.half.valueChanged.connect(self._save_cfg)
        self.cb_auto.stateChanged.connect(self._save_cfg)

    # ==================================================================
    # Прогноз против факта
    # ==================================================================
    def _build_backtest(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.hint = QLabel(
            "Для каждого прохода журнала: «текущая» — ошибка съёма модели с нынешними параметрами (насколько хорошо "
            "она описывает уже известное); «прогноз» — модель откалибрована только по проходам ДО этого и "
            "предсказывает его, как было бы в жизни. Ошибка = модель − факт, п.п. съёма за проход. Честная оценка "
            "точности — колонки «прогноз»; по ним же считается допуск ± во всех прогнозах времени.")
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)
        top = QHBoxLayout()
        gb = QGroupBox("Калибровка")
        f = QFormLayout(gb)
        self.half = dspin(0, 365, 0, 0, 1, " сут")
        self.half.setSpecialValueText("все проходы с одинаковым весом")
        self.cb_auto = QCheckBox("Перекалибровывать автоматически после правок журнала")
        f.addRow("Вес свежих данных: полураспад", self.half)
        f.addRow(self.cb_auto)
        top.addWidget(gb)
        right = QVBoxLayout()
        b = QPushButton("Пересчитать проверку")
        b.clicked.connect(self.refresh)
        right.addWidget(b)
        right.addStretch(1)
        top.addLayout(right)
        top.addStretch(1)
        lay.addLayout(top)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.RichText)
        lay.addWidget(self.summary)
        split = QSplitter(Qt.Vertical)
        self.tbl = table(["Начало прохода", "Строк", "W вх, %", "W вых, %", "D, с", "T, °C", "Съём факт",
                          "Эмп. текущая", "Эмп. прогноз", "Физ. текущая", "Физ. прогноз"], stretch_last=False)
        split.addWidget(self.tbl)
        self.plot = SimplePlot()
        self.plot.set_labels("проход по порядку", "ошибка съёма, п.п. (модель − факт)")
        split.addWidget(self.plot)
        split.setSizes([300, 300])
        lay.addWidget(split, 1)
        return w

    def _save_cfg(self) -> None:
        self.win.storage.set_json("calib_cfg", {"half_life": self.half.value(), "auto": self.cb_auto.isChecked()})

    def half_life(self) -> float:
        return self.half.value()

    def auto_enabled(self) -> bool:
        return self.cb_auto.isChecked()

    def mark_dirty(self) -> None:
        self._dirty = True
        self._doe_dirty = True

    def on_show(self) -> None:
        if self.sub.currentIndex() == 0 and self._dirty:
            self.refresh()
        elif self.sub.currentIndex() == 1 and self._doe_dirty:
            self.refresh_doe()

    def refresh(self) -> None:
        th = theme_for(self)
        self.hint.setStyleSheet(f"color:{th.muted};")
        self._dirty = False
        rows = self.win.journal_rows()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            bt = backtest(rows, self.win.params, self.half.value(), self.win.weather_series())
        finally:
            QApplication.restoreOverrideCursor()
        data = bt["rows"]
        self.tbl.setRowCount(len(data))
        for i, r in enumerate(data):
            def err(key):
                v = r.get(key)
                return None if v is None else v - r["dw_obs"]
            cells = [f"{r['start']:%d.%m.%Y %H:%M}", str(r["n"]), fnum(r["w_in"]), fnum(r["w_out"]), fnum(r["d"], 0),
                     fnum(r["t"], 0), fnum(r["dw_obs"], 2)]
            for key in ("cur_emp", "fc_emp", "cur_phys", "fc_phys"):
                e = err(key)
                cells.append("—" if e is None else f"{r[key]:.2f} ({e:+.2f})")
            for j, txt in enumerate(cells):
                it = item(txt, align_right=j > 0)
                if j >= 7:
                    e = err(("cur_emp", "fc_emp", "cur_phys", "fc_phys")[j - 7])
                    if e is not None:
                        lvl = 0 if abs(e) <= 0.3 else 1 if abs(e) <= 0.7 else 2
                        bg, fg = th.level(lvl)
                        it.setBackground(QBrush(bg))
                        it.setForeground(QBrush(fg))
                self.tbl.setItem(i, j, it)
        m = bt["mae"]
        sig = rate_sigma_from_backtest(bt, self.win.params.model_kind)
        sig_txt = ""
        if sig is not None:
            s, n = sig
            self.win.set_rate_sigma(s, f"проверка прогноза на {n} проходах журнала, ±{s * 100:.1f} %")
            sig_txt = (f"<br>Ошибка скорости сушки по прогнозам: <b>±{s * 100:.1f} %</b> (по {n} проходам) — "
                       f"по ней считаются допуски ± во всех прогнозах времени.")
        else:
            sig_txt = (f"<br>Для своего допуска нужно ≥ 3 прохода с прогнозом; пока используется "
                       f"±{self.win.params.rate_sigma * 100:.1f} % ({self.win.params.rate_sigma_info}).")
        if not data:
            self.summary.setText("В журнале нет проходов с ≥ 12 строками замеров выхода — проверять не на чем.")
        else:
            active = "физическая" if self.win.params.model_kind == "physics" else "эмпирическая"
            self.summary.setText(
                f"Проходов: {len(data)}. Средняя ошибка съёма за проход, п.п.:<br>"
                f"&nbsp;&nbsp;эмпирическая — текущая <b>±{fnum(m['cur_emp'], 2)}</b>, прогноз <b>±{fnum(m['fc_emp'], 2)}</b>;<br>"
                f"&nbsp;&nbsp;физическая — текущая <b>±{fnum(m['cur_phys'], 2)}</b>, прогноз <b>±{fnum(m['fc_phys'], 2)}</b>.<br>"
                f"В расчётах сейчас: <b>{active}</b>. {drift_text(bt, self.win.params.model_kind)}{sig_txt}")
        self.plot.clear()
        xs = list(range(1, len(data) + 1))
        for key, role in (("fc_emp", "line"), ("fc_phys", "target"), ("cur_emp", "ref")):
            pts = [(x, r[key] - r["dw_obs"]) for x, r in zip(xs, data) if r.get(key) is not None]
            if pts:
                self.plot.add_points([a for a, _ in pts], [b for _, b in pts], role, 8 if key != "cur_emp" else 5)
        if data:
            self.plot.add_hline(0.0, "ref", dashed=True)
        self.plot.set_title("синие — прогноз эмпирической, красные — прогноз физической, серые — эмпирическая текущая")

    # ==================================================================
    # План опытов
    # ==================================================================
    def _build_doe(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        left = QWidget()
        lv = QVBoxLayout(left)
        gb = QGroupBox("Условия следующих проходов")
        f = QFormLayout(gb)
        self.d_w = dspin(10, 45, 32, 1, 0.5, " %")
        self.d_lim = dspin(1, 25, 6, 1, 0.5, " п.п.")
        self.d_tmax = dspin(35, 150, 70, 0, 1, " °C")
        self.d_n = QSpinBox()
        self.d_n.setRange(1, 8)
        self.d_n.setValue(4)
        self.d_one = QCheckBox("Менять один фактор (D или T), второй — привычный")
        self.d_one.setChecked(True)
        b_w = QPushButton("W вх — из «Расчёт по модели»")
        b_w.clicked.connect(lambda: self.d_w.setValue(self.win.in_w0.value()))
        f.addRow("Влажность зерна на входе", self.d_w)
        f.addRow(b_w)
        f.addRow("Лимит съёма за проход для опытов", self.d_lim)
        f.addRow("T блоков в опытах не выше", self.d_tmax)
        f.addRow("Сколько опытных проходов", self.d_n)
        f.addRow(self.d_one)
        lv.addWidget(gb)
        b = QPushButton("Подобрать план")
        b.clicked.connect(self.refresh_doe)
        lv.addWidget(b)
        self.doe_hint = QLabel(
            "Зачем: в журнале октября D было только 35–43, T блоков 48–73 °C. Внутри этой области обе модели "
            "говорят почти одно и то же, а за её пределами расходятся — проверить, какая права, и уточнить "
            "влияние T и времени пребывания можно только проходами в других режимах. Программа выбирает режимы, "
            "которые (1) дальше всего от уже изученных и (2) там, где модели расходятся сильнее всего, — такой "
            "проход даёт больше всего информации. Все кандидаты проверены на ваш лимит съёма по ОБЕИМ моделям "
            "(берётся худшая), на пределы T блоков и T зерна. Правила: меняйте режим в начале прохода и держите его "
            "весь проход; записывайте пробы как обычно; после прохода нажмите «Калибровать модель» на вкладке "
            "«Журнал». Если зерно семенное — согласуйте T с инструкцией.")
        self.doe_hint.setWordWrap(True)
        lv.addWidget(self.doe_hint)
        lv.addStretch(1)
        left.setMaximumWidth(460)
        right = QSplitter(Qt.Vertical)
        top = QWidget()
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 0, 0, 0)
        self.doe_summary = QLabel()
        self.doe_summary.setWordWrap(True)
        self.doe_summary.setTextFormat(Qt.RichText)
        tv.addWidget(self.doe_summary)
        self.doe_tbl = table(["Проход", "D, с", "T блоков, °C", "Съём эмпир., п.п.", "Съём физ., п.п.",
                              "W вых ≈, %", "T зерна ≈, °C", "Новизна", "Расхождение, σ", "Что даст"])
        tv.addWidget(self.doe_tbl)
        right.addWidget(top)
        self.doe_plot = SimplePlot()
        self.doe_plot.set_labels("разгрузка D, с", "T блоков, °C")
        right.addWidget(self.doe_plot)
        right.setSizes([330, 330])
        lay.addWidget(scroll_wrap(left, 480), 0)
        lay.addWidget(right, 1)
        for wd in (self.d_w, self.d_lim, self.d_tmax):
            wd.valueChanged.connect(self._doe_changed)
        self.d_n.valueChanged.connect(self._doe_changed)
        self.d_one.stateChanged.connect(self._doe_changed)
        return w

    def _doe_changed(self, *_a) -> None:
        self._doe_dirty = True
        if self.sub.currentIndex() == 1 and self.isVisible():
            self.refresh_doe()

    def refresh_doe(self) -> None:
        th = theme_for(self)
        self.doe_hint.setStyleSheet(f"color:{th.muted};")
        self._doe_dirty = False
        p = self.win.params
        if p.t_agent_max and self.d_tmax.value() > p.t_agent_max:
            self.d_tmax.blockSignals(True)
            self.d_tmax.setValue(p.t_agent_max)
            self.d_tmax.blockSignals(False)
        passes = explored_passes(self.win.journal_rows(), p)
        d_typ, t_typ = typical_regime(passes)
        c = self.win.conditions()
        sigma_pp = 0.35
        plan, ok = suggest(p, c, self.d_w.value(), passes, self.d_lim.value(), d_typ, t_typ, self.d_n.value(),
                           self.d_one.isChecked(), sigma_pp, t_max=self.d_tmax.value())
        explored = ", ".join(f"D {x['d']:.0f} / {x['t']:.0f} °C / W {x['w_in']:.0f} %" for x in passes) or "нет"
        if not plan:
            self.doe_summary.setText(
                f"Изучено: {explored}.<br><span style='color:{th.error}'>Нет режимов, которые укладываются в лимит "
                f"съёма {self.d_lim.value():g} п.п. по обеим моделям и в пределы T. Ослабьте лимит или снимите "
                f"«один фактор».</span>")
        else:
            self.doe_summary.setText(
                f"Привычный режим по журналу: <b>D = {d_typ:.0f}, T блоков = {t_typ:.0f} °C</b>. Изученные проходы: "
                f"{explored}.<br>Предлагаемый порядок опытов — ниже (каждый следующий выбран с учётом предыдущих). "
                f"Допустимых режимов всего: {len(ok)}.")
        self.doe_tbl.setRowCount(len(plan))
        for i, x in enumerate(plan):
            cells = [str(i + 1), f"{x.d:.0f}", f"{x.t:.0f}", f"{x.dw_emp:.1f}", f"{x.dw_phys:.1f}", f"{x.w_out:.1f}",
                     f"{x.t_grain:.0f}", f"{x.novelty:.1f}", f"{x.disagreement:.1f}", x.why]
            for j, txt in enumerate(cells):
                it = item(txt, align_right=0 < j < 9)
                it.setToolTip(x.why)
                self.doe_tbl.setItem(i, j, it)
        self.doe_plot.clear()
        if ok:
            self.doe_plot.add_points([x.d for x in ok], [x.t for x in ok], "ref", 5)
        if passes:
            self.doe_plot.add_points([x["d"] for x in passes], [x["t"] for x in passes], "dot", 10)
        if plan:
            self.doe_plot.add_points([x.d for x in plan], [x.t for x in plan], "target", 10)
        self.doe_plot.set_title("синие — уже изучено, красные — предлагаемые опыты, серые — другие допустимые режимы")
