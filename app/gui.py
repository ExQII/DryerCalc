"""Окно программы (PySide6)."""
from __future__ import annotations

import re
import shutil
import sys
import traceback
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from PySide6.QtCore import QDateTime, QEvent, QTimer, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox, QInputDialog,
    QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSplitter, QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from . import __version__
from . import model as mdl
from .calibration import (FIELDS, HEADERS_RU, TEXT_FIELDS, calibrate, chronological_issues, chronological_order,
                          insert_position, note_trap, read_journal, to_float, write_template)
from . import backup as bkp
from . import worker
from .plots import SimplePlot
from .resources import resource_path
from .storage import Storage, default_dir
from .tab_batches import BatchesTab
from .tab_check import CheckTab
from .tab_data import DataTab
from .tab_help import HelpTab
from .tab_live import LiveTab
from .tab_recirc import RecircTab
from .reminders import ReminderCfg, check as reminders_check, live_numbers
from . import pilot
from .theme import ensure_style_for_palette, theme_for
from .weather import WeatherSeries

from .ui_common import SEP, dspin, fmt_hours, item, parse_decimals, parse_ints, scroll_wrap  # noqa: E402,F401


class MainWindow(QMainWindow):
    def __init__(self, storage: Optional[Storage] = None):
        super().__init__()
        self._ready = False
        self.storage = storage or Storage()
        self.params: mdl.DryerParams = self.storage.load_params()
        self.plans: List[mdl.Plan] = []
        self._loading = False
        # правки журнала сохраняются сами через секунду после последнего изменения
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(1000)
        self._save_timer.timeout.connect(self._save_journal)
        self.setWindowTitle(f"Расчёт времени сушки кукурузы — шахтная сушилка Araj  v{__version__}")
        icon = resource_path("assets/dryer.png")
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))

        self._weather: Optional[WeatherSeries] = None
        self._last_backup = None
        try:    # резервная копия базы раз в сутки (до любых правок)
            self._last_backup = bkp.auto_backup(self.storage.conn, self.storage.path)
        except Exception:  # noqa: BLE001
            pass
        # автокалибровка — не чаще, чем через 10 с после последней правки журнала
        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.setInterval(10000)
        self._auto_timer.timeout.connect(self._auto_calibrate)

        tabs = QTabWidget()
        self.tabs = tabs
        self.setCentralWidget(tabs)
        tabs.addTab(self._build_simple_tab(), "По производительности")
        tabs.addTab(self._build_calc_tab(), "Расчёт по модели")
        self.tab_live = LiveTab(self)
        tabs.addTab(self.tab_live, "Сейчас (по факту)")
        self.tab_recirc = RecircTab(self)
        tabs.addTab(self.tab_recirc, "Рециркуляция")
        tabs.addTab(self._build_journal_tab(), "Журнал")
        self.tab_batches = BatchesTab(self)
        tabs.addTab(self.tab_batches, "Партии и очередь")
        self.tab_data = DataTab(self)
        tabs.addTab(self.tab_data, "Погода и влагомер")
        self.tab_check = CheckTab(self)
        tabs.addTab(self.tab_check, "Проверка и опыты")
        tabs.addTab(self._build_settings_tab(), "Сушилка и модель")
        self.tab_help = HelpTab()
        tabs.addTab(self.tab_help, "Справка")
        tabs.currentChanged.connect(self._tab_changed)

        self._load_ui_state()
        self._load_simple_state()
        self._load_journal_into_table()
        self._fill_settings()
        self._apply_theme()
        self.resize(1400, 880)
        self._ready = True
        self.recalc()
        self.recalc_simple()
        try:    # переключение темы Windows на ходу
            QApplication.styleHints().colorSchemeChanged.connect(lambda *_: QTimer.singleShot(150, self._theme_changed))
        except AttributeError:
            pass
        # напоминания сушильщику: проверка раз в минуту и сразу после записи в журнал
        self._rem_state: Dict = {}
        self._tray = None
        try:
            if QSystemTrayIcon.isSystemTrayAvailable():
                self._tray = QSystemTrayIcon(self.windowIcon(), self)
                self._tray.setToolTip("DryerCalc — напоминания сушильщику")
                self._tray.activated.connect(lambda *_: (self.showNormal(), self.raise_(), self.activateWindow()))
                self._tray.show()
        except Exception:  # noqa: BLE001
            self._tray = None
        self._rem_timer = QTimer(self)
        self._rem_timer.setInterval(60000)
        self._rem_timer.timeout.connect(self._check_reminders)
        self._rem_timer.start()
        # режим «Сушильщик / Инженер» и сбор замечаний (пилотный запуск)
        self._base_pt = QApplication.font().pointSizeF()
        corner = QWidget()
        hl = QHBoxLayout(corner)
        hl.setContentsMargins(0, 0, 4, 2)
        self.btn_feedback = QPushButton("Замечание…")
        self.btn_feedback.setToolTip("Что-то неудобно или непонятно? Напишите — инженер увидит замечание.")
        self.btn_feedback.clicked.connect(self._feedback_dialog)
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItem("Режим: сушильщик", pilot.OPERATOR)
        self.cmb_mode.addItem("Режим: инженер", pilot.ENGINEER)
        self.cmb_mode.activated.connect(self._mode_selected)
        hl.addWidget(self.btn_feedback)
        hl.addWidget(self.cmb_mode)
        tabs.setCornerWidget(corner, Qt.TopRightCorner)
        self.lbl_mode = QLabel()
        self.statusBar().addPermanentWidget(self.lbl_mode)
        cfg_mode = self.storage.get_json("ui_mode")
        self.first_run = not isinstance(cfg_mode, dict) or "mode" not in cfg_mode
        self.mode = pilot.ENGINEER
        self.apply_mode(cfg_mode.get("mode", pilot.ENGINEER) if not self.first_run else pilot.ENGINEER,
                        save=False, log=False)
        self.storage.log_event("start", {"mode": self.mode, "version": __version__})
        self.statusBar().showMessage("Готово. Справка — последняя вкладка.", 8000)

    # ------------------------------------------------------------------
    # Общие данные для вкладок
    # ------------------------------------------------------------------
    def journal_rows(self) -> List[Dict]:
        return self._table_to_rows()

    def weather_series(self) -> WeatherSeries:
        if self._weather is None:
            self._weather = WeatherSeries(self.storage.load_weather())
        return self._weather

    def weather_changed(self) -> None:
        self._weather = None
        self.tab_check.mark_dirty()

    def conditions(self) -> mdl.Conditions:
        return mdl.Conditions(self.in_tproc.value(), self.in_tamb.value(), self.in_rh.value())

    def limits(self):
        return self.in_soft.value(), self.in_hard.value()

    def fuel_unit(self) -> str:
        return self.in_fuel_unit.text().strip() or "ед."

    def fuel_lhv(self) -> Optional[float]:
        v = self.in_lhv.value()
        return v if v > 0 else None

    def set_params(self, p: mdl.DryerParams) -> None:
        self.params = p
        self.storage.save_params(p)
        self._fill_settings()
        self._update_banner()
        self.recalc()
        self.recalc_simple()
        self.tab_check.mark_dirty()

    def set_rate_sigma(self, sigma: float, info: str) -> None:
        """Допуск прогнозов по проверке на журнале (не трогает калибровку; ручное значение не перезаписывает)."""
        from dataclasses import replace as _replace
        p = self.params
        if p.rate_sigma_info.startswith("задано вручную"):
            return
        if abs(sigma - p.rate_sigma) < 5e-4 and info == p.rate_sigma_info:
            return
        self.params = _replace(p, rate_sigma=sigma, rate_sigma_info=info)
        self.storage.save_params(self.params)
        self.s_sigma.setValue(sigma * 100.0)
        self.s_sigma.setToolTip(f"Источник: {info}")
        self.recalc()

    def set_out_bias(self, bias: float, kw: float = 0.0, kt: float = 0.0) -> None:
        from dataclasses import replace as _replace
        p = _replace(self.params, out_bias=bias, out_kw=kw, out_kt=kt, calibrated=False,
                     calib_info="поправка влагомера изменена — перекалибруйте",
                     phys_info="поправка влагомера изменена — перекалибруйте")
        self.set_params(p)

    def replace_journal_rows(self, rows: List[Dict]) -> None:
        self.tbl_j.setRowCount(0)
        self._append_rows(rows)
        self._save_journal()
        self._qe_prefill()

    def _tab_changed(self, idx: int) -> None:
        wdg = self.tabs.widget(idx)
        fn = getattr(wdg, "on_show", None)
        if callable(fn):
            fn()

    # ------------------------------------------------------------------
    # Вкладка «По производительности»
    # ------------------------------------------------------------------
    def _build_simple_tab(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        left = QWidget()
        lv = QVBoxLayout(left)

        gb = QGroupBox("Партия")
        f = QFormLayout(gb)
        self.sm_mass = dspin(1, 20000, 500, 0, 10, " т")
        self.sm_w0 = dspin(10, 50, 34, 1, 0.5, " %")
        self.sm_wk = dspin(8, 30, 14, 1, 0.5, " %")
        self.sm_start = QDateTimeEdit(QDateTime.currentDateTime())
        self.sm_start.setCalendarPopup(True)
        self.sm_start.setDisplayFormat("dd.MM.yyyy HH:mm")
        f.addRow("Масса партии", self.sm_mass)
        f.addRow("Начальная влажность W₀", self.sm_w0)
        f.addRow("Целевая влажность Wк", self.sm_wk)
        f.addRow("Старт сушки", self.sm_start)
        lv.addWidget(gb)

        gb = QGroupBox("Производительность")
        f = QFormLayout(gb)
        self.sm_q = QLineEdit("18,2")
        self.sm_q.setPlaceholderText("18,2   или  D35   или по проходам:  18,2; 20,8; 18,2")
        self.sm_q_hint = QLabel()
        self.sm_q_hint.setWordWrap(True)
        f.addRow("Выгрузка, т/ч", self.sm_q)
        f.addRow(self.sm_q_hint)
        lv.addWidget(gb)

        gb = QGroupBox("Проходы")
        f = QFormLayout(gb)
        self.sm_mode = QComboBox()
        self.sm_mode.addItem("Число проходов по лимиту съёма", "auto")
        self.sm_mode.addItem("Число проходов задаю сам", "manual")
        self.sm_lim = dspin(1, 25, 6, 1, 0.5, " п.п.")
        self.sm_n = dspin(1, 10, 3, 0, 1)
        self.sm_over = dspin(0, 24, self.params.overhead_h, 2, 0.25, " ч")
        self.sm_cool = QCheckBox("Финальное охлаждение на D = 100 (горелки выкл.)")
        self.sm_cool.setChecked(True)
        f.addRow("Режим", self.sm_mode)
        f.addRow("Съём за проход ≤", self.sm_lim)
        f.addRow("Число проходов", self.sm_n)
        f.addRow("Накладные на проход", self.sm_over)
        f.addRow(self.sm_cool)
        lv.addWidget(gb)

        self.sm_note = QLabel("Время прохода = масса выгруженного зерна / производительность. Съём влаги делится между "
                              "проходами поровну. Модель сушилки и журнал не нужны. Накладные: загрузка, прогрев "
                              "горелок, переключение силосов (по журналу около 1 ч на проход).")
        self.sm_note.setWordWrap(True)
        lv.addWidget(self.sm_note)
        self.sm_status = QLabel()
        self.sm_status.setWordWrap(True)
        lv.addWidget(self.sm_status)
        lv.addStretch(1)

        right = QSplitter(Qt.Vertical)
        top = QWidget()
        tv = QVBoxLayout(top)
        self.sm_summary = QLabel()
        self.sm_summary.setWordWrap(True)
        self.sm_summary.setTextFormat(Qt.RichText)
        tv.addWidget(self.sm_summary)
        self.sm_tbl = QTableWidget(0, 10)
        self.sm_tbl.setHorizontalHeaderLabels(
            ["№", "W вх, %", "W вых, %", "Δw, п.п.", "Q, т/ч", "D ≈, с", "Масса вх, т", "Масса вых, т",
             "Время, ч", "Окончание прохода"])
        self.sm_tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.sm_tbl.horizontalHeader().setStretchLastSection(True)
        tv.addWidget(self.sm_tbl)
        self.sm_cmp = QTableWidget(0, 5)
        self.sm_cmp.setHorizontalHeaderLabels(["Проходов", "Съём за проход, п.п.", "Чистое время, ч", "Итого", "Окончание"])
        self.sm_cmp.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.sm_cmp.horizontalHeader().setStretchLastSection(True)
        tv.addWidget(QLabel("Сравнение по числу проходов (при выбранной производительности)"))
        tv.addWidget(self.sm_cmp)
        right.addWidget(top)
        self.sm_plot = SimplePlot()
        self.sm_plot.set_labels("время от старта, ч", "средняя влажность партии, %")
        self.sm_plot.include_zero(x=True)
        right.addWidget(self.sm_plot)
        right.setSizes([540, 280])

        lay.addWidget(scroll_wrap(left), 0)
        lay.addWidget(right, 1)

        for wdg in (self.sm_mass, self.sm_w0, self.sm_wk, self.sm_lim, self.sm_n, self.sm_over):
            wdg.valueChanged.connect(self.recalc_simple)
        self.sm_start.dateTimeChanged.connect(self.recalc_simple)
        self.sm_mode.currentIndexChanged.connect(self.recalc_simple)
        self.sm_cool.stateChanged.connect(self.recalc_simple)
        self.sm_q.textChanged.connect(self.recalc_simple)
        return w

    def _parse_q(self, txt: str) -> List[float]:
        """'18,2' -> [18.2]; 'D35' -> Q(35); '18,2; 20,8' -> по проходам. Запятая — десятичная."""
        out: List[float] = []
        for tok in SEP.split(txt.strip()):
            if not tok:
                continue
            m = re.fullmatch(r"[dDдД]\s*=?\s*(\d+(?:[.,]\d+)?)", tok)
            try:
                if m:
                    out.append(mdl.q_at(self.params, float(m.group(1).replace(",", "."))))
                else:
                    out.append(float(tok.replace(",", ".")))
            except ValueError:
                return []
        return [x for x in out if x > 0]

    def recalc_simple(self) -> None:
        if not getattr(self, "_ready", False):
            return
        p = self.params
        th = theme_for(self)
        m0, w0, wk = self.sm_mass.value(), self.sm_w0.value(), self.sm_wk.value()
        start: datetime = self.sm_start.dateTime().toPython()
        qs = self._parse_q(self.sm_q.text())
        self.sm_status.setText("")
        self.sm_tbl.setRowCount(0)
        self.sm_cmp.setRowCount(0)
        self.sm_plot.clear()
        self.sm_summary.setText("")
        auto = self.sm_mode.currentData() == "auto"
        self.sm_lim.setEnabled(auto)
        self.sm_n.setEnabled(not auto)
        if qs:
            self.sm_q_hint.setText("Соответствует D ≈ " + " / ".join(f"{mdl.d_at(p, q):.0f}" for q in qs) +
                                   f" с из 100 (макс. {mdl.q_full(p):g} т/ч)" +
                                   ("; выше максимума!" if any(q > mdl.q_full(p) * 1.001 for q in qs) else ""))
        else:
            self.sm_q_hint.setText("")
            self.sm_status.setText("Введите производительность: число (18,2), D35 или список по проходам (18,2; 20,8).")
            return
        if w0 <= wk:
            self.sm_status.setText("Начальная влажность не выше целевой: сушка не нужна.")
            return
        overhead = self.sm_over.value()
        cool = self.sm_cool.isChecked()
        base = mdl.DryerParams.from_dict({**p.to_dict(), "overhead_h": overhead})
        n = int(-(-(w0 - wk) // self.sm_lim.value())) if auto else int(self.sm_n.value())
        n = max(1, n)
        plan = mdl.plan_simple(base, m0, w0, wk, qs, n, start, cool)

        work = plan.work_h
        end = plan.end
        warn = ""
        if plan.max_dw > 10.0:
            warn = f" <span style='color:{th.error}'>съём за проход выше 10 п.п.</span>"
        cool_txt = f" + охлаждение {plan.cool_h:.1f} ч" if cool else ""
        self.sm_summary.setText(
            f"<b>Итого: {fmt_hours(plan.total_time_h)}</b> &nbsp;·&nbsp; окончание <b>{end:%d.%m.%Y %H:%M}</b><br>"
            f"Проходов: {n}, съём за проход {plan.passes[0].dw:.1f} п.п.{warn}<br>"
            f"Чистое время {fmt_hours(work)} + накладные {n} × {overhead:g} ч{cool_txt} &nbsp;·&nbsp; "
            f"вода {plan.water_total:.1f} т &nbsp;·&nbsp; масса после сушки {plan.m_final:.1f} т")

        t = 0.0
        self.sm_tbl.setRowCount(len(plan.passes))
        for i, ps in enumerate(plan.passes):
            t += ps.time_h + overhead
            fin = start + timedelta(hours=t)
            cells = [str(ps.k), f"{ps.w_in:.1f}", f"{ps.w_out:.1f}", f"{ps.dw:.1f}", f"{ps.q:.1f}",
                     f"{ps.d:.0f}", f"{ps.m_in:.1f}", f"{ps.m_out:.1f}", f"{ps.time_h:.1f}", f"{fin:%d.%m %H:%M}"]
            for j, txt in enumerate(cells):
                self.sm_tbl.setItem(i, j, item(txt, align_right=(j < 9)))

        n_show = sorted({1, 2, 3, 4, 5, n})
        self.sm_cmp.setRowCount(len(n_show))
        for i, nn in enumerate(n_show):
            alt = mdl.plan_simple(base, m0, w0, wk, qs, nn, start, cool)
            cells = [str(nn), f"{alt.passes[0].dw:.1f}", f"{alt.work_h:.1f}",
                     fmt_hours(alt.total_time_h), f"{alt.end:%d.%m %H:%M}"]
            for j, txt in enumerate(cells):
                it = item(txt, align_right=(j < 4))
                if nn == n:
                    f_ = QFont()
                    f_.setBold(True)
                    it.setFont(f_)
                    it.setBackground(QBrush(QColor(th.highlight[0])))
                    it.setForeground(QBrush(QColor(th.highlight[1])))
                self.sm_cmp.setItem(i, j, it)

        pts = plan.timeline()
        self.sm_plot.add_line([x for x, _ in pts], [y for _, y in pts], "line", 2)
        self.sm_plot.add_hline(wk, "target")
        self.sm_plot.set_title(f"Влажность партии по времени, красная линия — цель {wk:g} %")
        self._save_simple_state()

    def _save_simple_state(self) -> None:
        self.storage.set_json("ui_simple", {
            "mass": self.sm_mass.value(), "w0": self.sm_w0.value(), "wk": self.sm_wk.value(),
            "q": self.sm_q.text(), "mode": self.sm_mode.currentData(), "lim": self.sm_lim.value(),
            "n": self.sm_n.value(), "over": self.sm_over.value(), "cool": self.sm_cool.isChecked()})

    def _load_simple_state(self) -> None:
        d = self.storage.get_json("ui_simple", {}) or {}
        for key, wdg in (("mass", self.sm_mass), ("w0", self.sm_w0), ("wk", self.sm_wk),
                         ("lim", self.sm_lim), ("n", self.sm_n), ("over", self.sm_over)):
            if key in d:
                wdg.setValue(float(d[key]))
        if "q" in d:
            self.sm_q.setText(str(d["q"]))
        if d.get("mode") == "manual":
            self.sm_mode.setCurrentIndex(1)
        if "cool" in d:
            self.sm_cool.setChecked(bool(d["cool"]))

    # ------------------------------------------------------------------
    # Вкладка «Расчёт по модели»
    # ------------------------------------------------------------------
    def _build_calc_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        lay.addWidget(self.banner)

        split = QSplitter(Qt.Horizontal)
        lay.addWidget(split, 1)

        left = QWidget()
        lv = QVBoxLayout(left)

        gb = QGroupBox("Партия")
        f = QFormLayout(gb)
        self.in_mass = dspin(1, 20000, 500, 0, 10, " т")
        self.in_w0 = dspin(10, 50, 34, 1, 0.5, " %")
        self.in_wk = dspin(8, 30, 14, 1, 0.5, " %")
        self.in_start = QDateTimeEdit(QDateTime.currentDateTime())
        self.in_start.setCalendarPopup(True)
        self.in_start.setDisplayFormat("dd.MM.yyyy HH:mm")
        f.addRow("Масса партии", self.in_mass)
        f.addRow("Начальная влажность W₀", self.in_w0)
        f.addRow("Целевая влажность Wк (после охлаждения)", self.in_wk)
        f.addRow("Старт сушки", self.in_start)
        lv.addWidget(gb)

        gb = QGroupBox("Условия")
        f = QFormLayout(gb)
        self.lbl_tproc = QLabel("T блоков (средняя уставка)")
        self.in_tproc = dspin(20, 150, 60, 1, 5, " °C")
        self.in_tlist = QLineEdit()
        self.in_tlist.setPlaceholderText("по проходам, например 65; 50; 60")
        self.in_tamb = dspin(-40, 45, 10, 1, 1, " °C")
        self.in_rh = dspin(10, 100, 70, 0, 5, " %")
        f.addRow(self.lbl_tproc, self.in_tproc)
        f.addRow("T по проходам", self.in_tlist)
        f.addRow("Наружный воздух, T", self.in_tamb)
        f.addRow("Наружный воздух, RH", self.in_rh)
        self.btn_calc_w = QPushButton("T и RH: среднее за 3 сут из таблицы погоды")
        self.btn_calc_w.clicked.connect(self._calc_weather)
        f.addRow(self.btn_calc_w)
        lv.addWidget(gb)

        gb = QGroupBox("Ограничения качества и привычный режим")
        f = QFormLayout(gb)
        self.in_soft = dspin(1, 20, 6, 1, 0.5, " п.п.")
        self.in_hard = dspin(1, 25, 10, 1, 0.5, " п.п.")
        self.in_dfix = dspin(1, 100, 35, 0, 1, " с")
        self.in_manual = QLineEdit()
        self.in_manual.setPlaceholderText("например 40,35,35 или 75")
        self.in_cool = QCheckBox("Финальное охлаждение на D = 100")
        self.in_cool.setChecked(True)
        f.addRow("Желаемый съём за проход ≤", self.in_soft)
        f.addRow("Допустимый съём за проход ≤", self.in_hard)
        f.addRow("Привычная разгрузка D", self.in_dfix)
        f.addRow("D по проходам (с), ручной", self.in_manual)
        f.addRow(self.in_cool)
        lv.addWidget(gb)

        gb = QGroupBox("Топливо (необязательно)")
        f = QFormLayout(gb)
        self.in_fuel_unit = QLineEdit("м³")
        self.in_fuel = dspin(0, 100000, 0, 1, 1)
        self.in_fuel_price = dspin(0, 1000000, 0, 2, 1, " ₽")
        f.addRow("Единица топлива", self.in_fuel_unit)
        f.addRow("Расход на 1 т испарённой воды", self.in_fuel)
        f.addRow("Цена за единицу", self.in_fuel_price)
        self.in_lhv = dspin(0, 1000, 0, 1, 0.5, " МДж/ед.")
        self.in_lhv.setSpecialValueText("не задано")
        self.in_lhv.setToolTip("Теплота сгорания топлива для теплового КПД: природный газ ≈ 33–35 МДж/м³, "
                               "дизель ≈ 42,6 МДж/кг (≈ 35,8 МДж/л), пропан-бутан ≈ 46 МДж/кг")
        f.addRow("Теплота сгорания (для КПД)", self.in_lhv)
        lv.addWidget(gb)

        self.btn_calc = QPushButton("Рассчитать")
        self.btn_calc.clicked.connect(self.recalc)
        lv.addWidget(self.btn_calc)
        self.status = QLabel()
        self.status.setWordWrap(True)
        lv.addWidget(self.status)
        lv.addStretch(1)
        split.addWidget(scroll_wrap(left))

        right = QSplitter(Qt.Vertical)
        self.tbl_sc = QTableWidget(0, 9)
        self.tbl_sc.setHorizontalHeaderLabels(
            ["Сценарий", "Съём по\nпроходам, п.п.", "D по\nпроходам, с", "Время\n(90 % допуск)", "Окончание",
             "Вода, т", "Топливо", "Вторая модель:\nW итог", "Оценка"])
        self.tbl_sc.horizontalHeaderItem(7).setToolTip(
            "Влажность в конце при тех же D по второй модели (эмпирическая ↔ физическая). Большое расхождение "
            "(> 1 п.п.) — план вне того, что видно в журнале; доверять ему меньше.")
        self.tbl_sc.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_sc.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tbl_sc.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl_sc.horizontalHeader().setStretchLastSection(True)
        self.tbl_sc.itemSelectionChanged.connect(self._on_scenario_selected)
        right.addWidget(self.tbl_sc)

        self.tbl_det = QTableWidget(0, 13)
        self.tbl_det.setHorizontalHeaderLabels(
            ["№", "W вх, %", "W вых, %", "Показание на выходе, %", "Δw, п.п.", "D, с", "Q, т/ч", "T блоков, °C",
             "T зерна ≈, °C", "Время, ч", "В сушилке, мин", "Масса вых, т", "Примечание"])
        self.tbl_det.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl_det.horizontalHeader().setStretchLastSection(True)
        right.addWidget(self.tbl_det)

        self.plot = SimplePlot()
        self.plot.set_labels("время от старта, ч", "средняя влажность партии, %")
        self.plot.include_zero(x=True)
        right.addWidget(self.plot)
        right.setSizes([240, 200, 290])
        split.addWidget(right)
        split.setSizes([380, 1000])

        for wdg in (self.in_mass, self.in_w0, self.in_wk, self.in_tproc, self.in_tamb, self.in_rh,
                    self.in_soft, self.in_hard, self.in_dfix, self.in_fuel, self.in_fuel_price):
            wdg.valueChanged.connect(self.recalc)
        self.in_start.dateTimeChanged.connect(self.recalc)
        self.in_cool.stateChanged.connect(self.recalc)
        for le in (self.in_manual, self.in_tlist, self.in_fuel_unit):
            le.editingFinished.connect(self.recalc)
        return w

    def recalc(self) -> None:
        if not self._ready:
            return
        p = self.params
        th = theme_for(self)
        m0, w0, wk = self.in_mass.value(), self.in_w0.value(), self.in_wk.value()
        c = mdl.Conditions(self.in_tproc.value(), self.in_tamb.value(), self.in_rh.value())
        start: datetime = self.in_start.dateTime().toPython()
        cool = self.in_cool.isChecked()
        t_list = parse_decimals(self.in_tlist.text(), 0, 200) or None
        self.status.setText("")
        self.tbl_sc.setRowCount(0)
        self.tbl_det.setRowCount(0)
        self.plot.clear()
        if wk <= p.w_eq + 0.5:
            self.status.setText(f"Целевая влажность должна быть выше равновесной ({p.w_eq:g} %) хотя бы на 0,5 п.п.")
            return
        if w0 <= wk:
            self.status.setText("Начальная влажность не выше целевой: сушка не нужна.")
            return

        plans = mdl.scenarios(p, c, m0, w0, wk, 6, start, t_list, cool)
        uniq: List[mdl.Plan] = []   # одинаковые сценарии (когда лишние проходы не нужны) показываем один раз
        for pl in plans:
            if not uniq or len(pl.passes) != len(uniq[-1].passes):
                uniq.append(pl)
        plans = uniq
        soft, hard = self.in_soft.value(), self.in_hard.value()
        rec = mdl.recommend(plans, soft, hard)
        extra: List[mdl.Plan] = [mdl.plan_manual(p, c, m0, w0, wk, [self.in_dfix.value()], start, t_list=t_list,
                                                 cool=cool, name=f"Как у вас: D={self.in_dfix.value():g}")]
        manual = parse_ints(self.in_manual.text())
        if manual:
            extra.append(mdl.plan_manual(p, c, m0, w0, wk, manual, start, t_list=t_list, cool=cool))
        n_auto = len(plans)
        # допуск времени: та же схема при скорости сушки ×(1 ± 1,645·σ)
        builders = [(lambda pp, n=len(pl.passes): mdl.plan_by_passes(pp, c, m0, w0, wk, n, start, t_list, cool))
                    for pl in plans]
        builders.append(lambda pp: mdl.plan_manual(pp, c, m0, w0, wk, [self.in_dfix.value()], start, t_list=t_list,
                                                   cool=cool))
        if manual:
            builders.append(lambda pp: mdl.plan_manual(pp, c, m0, w0, wk, manual, start, t_list=t_list, cool=cool))
        plans = plans + extra
        self.plans = plans
        # честный допуск: σ скорости сушки по каждому сценарию — проверка на журнале + разброс коэффициентов +
        # расхождение моделей сверх обычного (растёт вне изученных режимов)
        from dataclasses import replace as _replace
        self.plan_other = [mdl.other_model_final(p, pl, c, t_list) for pl in plans]
        self.plan_sigmas = [mdl.plan_sigma(p, pl, c, wo) for pl, wo in zip(plans, self.plan_other)]
        self.plan_ranges = [mdl.time_range(b, _replace(p, rate_sigma=sg)) for b, (sg, _) in zip(builders, self.plan_sigmas)]

        fuel, price, unit = self.in_fuel.value(), self.in_fuel_price.value(), self.in_fuel_unit.text().strip()
        self.tbl_sc.setRowCount(len(plans))
        bold = QFont()
        bold.setBold(True)
        for i, pl in enumerate(plans):
            level, text = pl.verdict(soft, hard)
            is_auto = i < n_auto
            title = pl.name + (" ★" if i == rec and is_auto else "")
            end = pl.end.strftime("%d.%m %H:%M") if pl.end else "—"
            if fuel > 0:
                amount = pl.water_total * fuel
                fuel_txt = f"{amount:,.0f} {unit}".replace(",", " ")
                if price > 0:
                    fuel_txt += f" / {amount * price:,.0f} ₽".replace(",", " ")
            else:
                fuel_txt = "—"
            lo, hi = self.plan_ranges[i]
            time_txt = fmt_hours(pl.total_time_h) + f" ({lo:.0f}–{hi:.0f})"
            level, text = self._limit_check(pl, level, text)
            w_other = self.plan_other[i]
            other_txt = f"{w_other:.1f} %" + (" ⚠" if abs(w_other - pl.w_final) > 1.0 else "")
            sig, sig_info = self.plan_sigmas[i]
            if sig_info["flags"]:
                text += "; вне изученного: " + ", ".join(f.split(" (")[0] for f in sig_info["flags"])
            cells = [
                title,
                " / ".join(f"{x.dw:.1f}" for x in pl.passes),
                " / ".join(f"{x.d:.0f}" for x in pl.passes),
                time_txt, end, f"{pl.water_total:.1f}", fuel_txt, other_txt, text,
            ]
            bg, fg = th.level(level)
            tip = mdl.sigma_text(sig_info)
            for j, txt in enumerate(cells):
                it = item(txt)
                it.setBackground(QBrush(bg))
                it.setForeground(QBrush(fg))
                if j in (3, 7, 8):
                    it.setToolTip(tip)
                if i == rec and is_auto:
                    it.setFont(bold)
                self.tbl_sc.setItem(i, j, it)
        self.tbl_sc.resizeColumnsToContents()
        pick = rec if rec is not None else 0
        if manual:
            pick = len(plans) - 1
        self.tbl_sc.selectRow(pick)
        self._save_ui_state()

    def _limit_check(self, pl: mdl.Plan, level: int, text: str):
        """Ограничения по T агента и T зерна (задаются на вкладке «Сушилка и модель»)."""
        p = self.params
        if p.t_agent_max and any(x.t_proc > p.t_agent_max + 1e-9 for x in pl.passes):
            level = max(level, 2)
            text += f"; T блоков > {p.t_agent_max:g} °C"
        if p.t_grain_max and any(x.t_grain > p.t_grain_max + 1e-9 for x in pl.passes):
            level = max(level, 2)
            text += f"; T зерна ≈ {max(x.t_grain for x in pl.passes):.0f} > {p.t_grain_max:g} °C"
        return level, text

    def _other_model_final(self, pl: mdl.Plan, c: mdl.Conditions, t_list) -> float:
        return mdl.other_model_final(self.params, pl, c, t_list)

    def _calc_weather(self) -> None:
        ws = self.weather_series()
        if not len(ws):
            QMessageBox.information(self, "Погода", "Таблица погоды пуста (вкладка «Погода и влагомер»).")
            return
        end = ws.last_dt() or datetime.now()
        t, rh, n = ws.mean_between(end - timedelta(days=3), end)
        if t is not None:
            self.in_tamb.setValue(t)
        if rh is not None:
            self.in_rh.setValue(rh)
        self.btn_calc_w.setText(f"T и RH: среднее за 3 сут ({n} замеров до {end:%d.%m %H:%M})")

    def _on_scenario_selected(self) -> None:
        rows = self.tbl_sc.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self.plans):
            return
        pl = self.plans[rows[0].row()]
        rng = self.plan_ranges[rows[0].row()] if rows[0].row() < len(getattr(self, "plan_ranges", [])) else None
        sig = getattr(self, "plan_sigmas", [])
        if rows[0].row() < len(sig):
            self.status.setText(mdl.sigma_text(sig[rows[0].row()][1]) + ".")
        self.tbl_det.setRowCount(len(pl.passes))
        for i, ps in enumerate(pl.passes):
            read = mdl.read_from_true(self.params, ps.w_out, ps.t_grain or None)
            cells = [str(ps.k), f"{ps.w_in:.1f}", f"{ps.w_out:.1f}", f"{read:.1f}", f"{ps.dw:.1f}",
                     f"{ps.d:.0f}", f"{ps.q:.1f}", f"{ps.t_proc:.0f}", f"{ps.t_grain:.0f}" if ps.t_grain else "—",
                     f"{ps.time_h:.1f}", f"{ps.residence_min:.0f}", f"{ps.m_out:.1f}", ps.note]
            for j, txt in enumerate(cells):
                self.tbl_det.setItem(i, j, item(txt, align_right=(j < 12)))
        self.plot.clear()
        pts = pl.timeline()
        self.plot.add_line([x for x, _ in pts], [y for _, y in pts], "line", 2)
        self.plot.add_hline(pl.w_target, "target")
        span = ""
        if rng and pl.start:
            a, b = pl.start + timedelta(hours=rng[0]), pl.start + timedelta(hours=rng[1])
            span = f" (90 %: {a:%d.%m %H:%M} … {b:%d.%m %H:%M})"
        self.plot.set_title(f"{pl.name}: итого {fmt_hours(pl.total_time_h)}, окончание {pl.end:%d.%m %H:%M}{span}; "
                            f"красная — цель {pl.w_target:g} %" if pl.end else f"{pl.name}: итого {fmt_hours(pl.total_time_h)}")

    # ------------------------------------------------------------------
    # Вкладка «Журнал»
    # ------------------------------------------------------------------
    def _build_journal_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        hint = QLabel(
            "Колонки как в бумажном журнале: время пробы, влага вход, влага выход, температура зерна (нижний датчик, "
            "верхний), температура блоков (пишется только при изменении, пустое значение = прежнее), разгрузка D, "
            "примечание. Время можно вводить как «08:30» (дата и переход через полночь определяются сами) или "
            "«21.10.26 08:30». Строки-примечания («Остановка», «Запуск», «Закончился …», «открыли с …») делят "
            "журнал на проходы. Таблицу можно править прямо здесь. Быстрый ввод: время само сдвигается на 30 мин, "
            "T блоков, D, партия и проход переносятся с прошлой строки — меняйте их, только когда меняются.")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        bar = QHBoxLayout()
        self.j_buttons: Dict[str, QPushButton] = {}
        for text, slot in (("Импорт CSV/XLSX…", self._import_journal),
                           ("Сохранить шаблон…", self._save_template),
                           ("Сохранить пример…", self._save_sample),
                           ("Бланк журнала PDF…", self._blank_pdf),
                           ("Добавить пустую строку", self._add_row),
                           ("Удалить выделенные", self._delete_rows),
                           ("Калибровать модель", self._calibrate)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            bar.addWidget(b)
            self.j_buttons[text] = b
        bar.addStretch(1)
        lay.addLayout(bar)

        qe = QGroupBox("Быстрый ввод пробы — Enter добавляет строку")
        qe.setToolTip("Время само сдвигается на 30 мин; T блоков, D, партия и проход переносятся с прошлой строки — "
                      "меняйте их, только когда меняются.")
        g = QGridLayout(qe)
        g.setVerticalSpacing(2)
        self.qe_time = QDateTimeEdit(QDateTime.currentDateTime())
        self.qe_time.setDisplayFormat("dd.MM.yy HH:mm")
        self.qe_time.setCalendarPopup(True)
        g.addWidget(QLabel("Время пробы"), 0, 0)
        g.addWidget(self.qe_time, 1, 0)
        self.qe: Dict[str, QLineEdit] = {}
        col = 1
        for key, cap, ph, width in (("w_in", "Влага вход, %", "Вход, %", 70), ("w_out", "Влага выход, %", "Выход, %", 70),
                                    ("t_a1", "T зерна низ", "T низ", 56), ("t_a2", "T зерна верх", "T верх", 56),
                                    ("t_up", "T блока 1", "Блок 1", 60), ("t_low", "T блока 2", "Блок 2", 60),
                                    ("d", "Разгрузка D", "D, с", 52), ("batch", "Партия", "Партия", 64),
                                    ("pass_no", "Проход", "Проход", 58),
                                    ("note", "Примечание", "«Остановка», «Запуск», «Закончился …», «рециркуляция»", 0)):
            le = QLineEdit()
            le.setPlaceholderText(ph)
            le.setToolTip(cap)
            le.setProperty("base_width", width)
            if width:
                le.setFixedWidth(width)
            le.returnPressed.connect(self._qe_add)
            self.qe[key] = le
            g.addWidget(QLabel(cap), 0, col)
            g.addWidget(le, 1, col)
            if not width:
                g.setColumnStretch(col, 1)
            col += 1
        b = QPushButton("Добавить")
        b.clicked.connect(self._qe_add)
        g.addWidget(b, 1, col)
        self.qe_add_btn = b
        self.qe_box = qe
        lay.addWidget(qe)

        self.order_bar = QWidget()
        ob = QHBoxLayout(self.order_bar)
        ob.setContentsMargins(0, 0, 0, 0)
        self.order_lbl = QLabel()
        self.order_lbl.setWordWrap(True)
        ob.addWidget(self.order_lbl, 1)
        b_sort = QPushButton("Упорядочить по времени")
        b_sort.clicked.connect(self._sort_journal)
        ob.addWidget(b_sort)
        self.order_bar.setVisible(False)
        lay.addWidget(self.order_bar)

        split = QSplitter(Qt.Vertical)
        self.tbl_j = QTableWidget(0, len(FIELDS))
        self.tbl_j.setHorizontalHeaderLabels([HEADERS_RU[f] for f in FIELDS])
        self.tbl_j.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl_j.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_j.itemChanged.connect(self._journal_changed)
        self._rows_cache: Optional[List[Dict]] = None
        jm = self.tbl_j.model()
        for sig in (jm.dataChanged, jm.rowsInserted, jm.rowsRemoved, jm.rowsMoved, jm.modelReset, jm.layoutChanged):
            sig.connect(self._invalidate_rows)
        split.addWidget(self.tbl_j)

        bottom = QSplitter(Qt.Horizontal)
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.Monospace)
        self.report.setFont(mono)
        self.report.setPlaceholderText("Здесь появится отчёт калибровки и сводка по проходам.")
        bottom.addWidget(self.report)
        self.plot_cal = SimplePlot()
        self.plot_cal.set_labels("R по журналу, т воды/ч", "R по модели, т воды/ч")
        self.plot_cal.include_zero(x=True, y=True)
        bottom.addWidget(self.plot_cal)
        bottom.setSizes([760, 420])
        split.addWidget(bottom)
        split.setSizes([430, 300])
        self.j_bottom = bottom
        lay.addWidget(split, 1)
        return w

    def _load_journal_into_table(self) -> None:
        self._append_rows(self.storage.load_journal())
        self._check_order()
        self._qe_prefill()
        QTimer.singleShot(0, self.tbl_j.scrollToBottom)      # свежие записи — внизу, их и показываем

    def _qe_prefill(self) -> None:
        """Время следующей пробы и переносимые значения — по последним строкам журнала."""
        from .calibration import prepare
        rows = self._table_to_rows()
        if not rows:
            now = datetime.now()
            self.qe_time.setDateTime(now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0))
            return
        prep, reliable = prepare(rows)
        last = prep[-1]
        if reliable and last.get("dt") is not None:
            self.qe_time.setDateTime(last["dt"] + timedelta(minutes=30))
        for key, val in (("t_up", last.get("t_up_f")), ("t_low", last.get("t_low_f")), ("d", last.get("d_f")),
                         ("pass_no", last.get("pass_no"))):
            if val is not None and not self.qe[key].text().strip():
                self.qe[key].setText(f"{val:g}".replace(".", ","))
        if last.get("batch") and not self.qe["batch"].text().strip():
            self.qe["batch"].setText(str(last["batch"]))

    def _qe_add(self) -> None:
        vals = {k: le.text().strip() for k, le in self.qe.items()}
        if not any(vals[k] for k in ("w_in", "w_out", "note")):
            self.qe["w_in"].setFocus()
            return
        row: Dict = {f: None for f in FIELDS}
        for f in TEXT_FIELDS:
            row[f] = ""
        row["ts"] = self.qe_time.dateTime().toString("dd.MM.yy HH:mm")
        bad = []
        for key, txt in vals.items():
            if key in ("batch", "note"):
                row[key] = txt
            elif txt:
                v = to_float(txt)
                if v is None:
                    bad.append(self.qe[key].placeholderText())
                row[key] = v
        if bad:
            QMessageBox.warning(self, "Быстрый ввод", "Не число: " + ", ".join(bad))
            return
        if row["note"] and not self._confirm_note(row["note"]):
            self.qe["note"].setFocus()
            self.qe["note"].selectAll()
            return
        rows_now = self._table_to_rows()
        pos = insert_position(rows_now, row["ts"])
        if pos < len(rows_now):
            self._insert_rows(pos, [row])
            self.statusBar().showMessage(
                f"Строка {row['ts']} вставлена на своё место по времени (строка {pos + 1} журнала).", 15000)
            self.tbl_j.scrollToItem(self.tbl_j.item(pos, 0))
            self.tbl_j.selectRow(pos)
        else:
            self._append_rows([row])
            self.tbl_j.scrollToBottom()
        self._save_journal()
        self.storage.log_event("sample", {"ts": row["ts"], "late_min": round(
            (datetime.now() - self.qe_time.dateTime().toPython()).total_seconds() / 60.0, 1)})
        if pos < len(rows_now):
            self._qe_prefill_time()
        else:
            self.qe_time.setDateTime(self.qe_time.dateTime().addSecs(30 * 60))
        for key in ("w_in", "w_out", "t_a1", "t_a2", "note"):
            self.qe[key].clear()
        self.qe["w_in"].setFocus()

    def _confirm_note(self, note: str) -> bool:
        """Примечание со словом-командой не по образцу события — переспросить (слова-ловушки)."""
        meaning = note_trap(note)
        if not meaning:
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Примечание")
        box.setText(f"В примечании «{note}» есть слово, которое программа понимает как событие:")
        box.setInformativeText(
            "\n".join("• " + m for m in meaning)
            + "\n\nЕсли это просто комментарий — перепишите его другими словами (например, «на выходе холодное» "
              "вместо «охлаждено», «чистка люка» вместо «открыли люк», «Запуск подачи с 1 с.» вместо «закончили "
              "рециркуляцию»). Подробно — «Справка» → «Как правильно работать», раздел 5.")
        yes = box.addButton("Да, это событие", QMessageBox.YesRole)
        box.addButton("Нет, исправлю примечание", QMessageBox.NoRole)
        box.exec()
        return box.clickedButton() is yes

    def _qe_prefill_time(self) -> None:
        """Время следующей пробы = последняя запись журнала + 30 мин."""
        from .calibration import assign_times
        times, reliable = assign_times(self._table_to_rows())
        last = max((t for t in times if t is not None), default=None)
        if reliable and last is not None:
            self.qe_time.setDateTime(last + timedelta(minutes=30))

    def _insert_rows(self, pos: int, rows: List[Dict]) -> None:
        self._loading = True
        try:
            for k, r in enumerate(rows):
                i = pos + k
                self.tbl_j.insertRow(i)
                for j, f in enumerate(FIELDS):
                    v = r.get(f)
                    txt = "" if v is None else (str(v) if f in TEXT_FIELDS else f"{v:g}")
                    self.tbl_j.setItem(i, j, QTableWidgetItem(txt))
        finally:
            self._loading = False

    def _check_order(self, rows: Optional[List[Dict]] = None) -> None:
        """Показать предупреждение, если в журнале есть строки «из прошлого» не на своём месте."""
        rows = self._table_to_rows() if rows is None else rows
        bad = chronological_issues(rows)
        if bad:
            th = theme_for(self)
            shown = ", ".join(f"{i + 1} ({rows[i].get('ts')})" for i in bad[:4]) + (" …" if len(bad) > 4 else "")
            self.order_lbl.setText(
                f"⚠ Строки не по порядку времени: {shown}. Программа читает журнал сверху вниз — такая строка режет "
                f"проход на части. Нажмите «Упорядочить по времени».")
            self.order_lbl.setStyleSheet(f"color:{th.error}; font-weight:bold;")
        self.order_bar.setVisible(bool(bad))

    def _sort_journal(self) -> None:
        rows = self._table_to_rows()
        order = chronological_order(rows)
        if order == list(range(len(rows))):
            self.order_bar.setVisible(False)
            return
        moved = sum(1 for k, i in enumerate(order) if k != i)
        self.tbl_j.setRowCount(0)
        self._append_rows([rows[i] for i in order])
        self._save_journal()
        self.statusBar().showMessage(f"Журнал упорядочен по времени (переставлено строк: {moved}).", 10000)

    def _blank_pdf(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Бланк журнала сушки (PDF)")
        f = QFormLayout(dlg)
        start = QDateTimeEdit(QDateTime.currentDateTime())
        start.setDisplayFormat("dd.MM.yyyy HH:mm")
        start.setCalendarPopup(True)
        t = start.dateTime().time()
        start.setTime(t.fromString("08:00", "HH:mm"))
        hours = QComboBox()
        hours.addItem("12 часов (смена, 1 лист)", 12)
        hours.addItem("24 часа (сутки, 2 листа)", 24)
        with_time = QCheckBox("Проставить время в строках")
        with_time.setChecked(True)
        batch = QLineEdit(self.qe["batch"].text())
        pass_no = QLineEdit(self.qe["pass_no"].text())
        f.addRow("Начало", start)
        f.addRow("Длительность", hours)
        f.addRow(with_time)
        f.addRow("Партия", batch)
        f.addRow("Проход", pass_no)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        st = start.dateTime().toPython()
        path, _ = QFileDialog.getSaveFileName(self, "Бланк журнала", f"бланк_журнала_{st:%d.%m.%Y}.pdf", "PDF (*.pdf)")
        if not path:
            return
        from .reports import journal_blank_pdf
        n = journal_blank_pdf(path, st if with_time.isChecked() else None, int(hours.currentData()), 30,
                              batch.text().strip(), pass_no.text().strip())
        QMessageBox.information(self, "Бланк журнала", f"Сохранено: {path}\nЛистов: {n}. Откройте файл и распечатайте.")

    def _journal_changed(self, it=None, *_args) -> None:
        if not self._loading:
            self._save_timer.start()
            if it is not None and FIELDS[it.column()] == "note":
                meaning = note_trap(it.text())
                if meaning:
                    self.statusBar().showMessage(
                        f"⚠ Примечание «{it.text()}» программа поймёт как событие: {meaning[0]}. Если это просто "
                        f"комментарий — перепишите другими словами (Справка → «Как правильно работать», раздел 5).",
                        30000)

    def _append_rows(self, rows: List[Dict]) -> None:
        cache = getattr(self, "_rows_cache", None)
        first = self.tbl_j.rowCount()
        self._loading = True
        try:
            for r in rows:
                i = self.tbl_j.rowCount()
                self.tbl_j.insertRow(i)
                for j, f in enumerate(FIELDS):
                    v = r.get(f)
                    if v is None:
                        txt = ""
                    elif f in TEXT_FIELDS:
                        txt = str(v)
                    else:
                        txt = f"{v:g}"
                    self.tbl_j.setItem(i, j, QTableWidgetItem(txt))
        finally:
            self._loading = False
        if cache is not None:               # дописали в конец — дочитать только новые строки
            self._rows_cache = cache + self._read_table_rows(first)

    def _invalidate_rows(self, *_a) -> None:
        self._rows_cache = None

    def _table_to_rows(self) -> List[Dict]:
        """Строки журнала из таблицы. Разбор таблицы кэшируется до её следующего изменения (на журнале сезона —
        тысячи строк, а читают его и напоминания, и «Сейчас», и сохранение); наружу — копии строк."""
        if getattr(self, "_rows_cache", None) is None:
            self._rows_cache = self._read_table_rows()
        return [dict(r) for r in self._rows_cache]

    def _read_table_rows(self, start: int = 0) -> List[Dict]:
        rows: List[Dict] = []
        for i in range(start, self.tbl_j.rowCount()):
            row: Dict = {}
            empty = True
            for j, f in enumerate(FIELDS):
                it = self.tbl_j.item(i, j)
                txt = it.text().strip() if it else ""
                if txt:
                    empty = False
                if f in TEXT_FIELDS:
                    row[f] = txt
                else:
                    row[f] = to_float(txt) if txt else None
            if not empty:
                rows.append(row)
        return rows

    def _save_journal(self) -> None:
        rows = self._table_to_rows()
        self.storage.replace_journal(rows)
        self._check_order(rows)
        if not getattr(self, "_ready", False):
            return
        self.tab_check.mark_dirty()
        if self.tabs.currentWidget() is self.tab_live:
            self.tab_live.refresh()
        if self.tab_check.auto_enabled():
            self._auto_timer.start()
        QTimer.singleShot(300, self._check_reminders)

    # ------------------------------------------------------------------
    # Напоминания
    # ------------------------------------------------------------------
    def reminder_cfg(self) -> ReminderCfg:
        return ReminderCfg.from_dict(self.storage.get_json("reminders", {}))

    def _check_reminders(self, now: Optional[datetime] = None) -> list:
        if not getattr(self, "_ready", False):
            return []
        cfg = self.reminder_cfg()
        if not cfg.enabled:
            return []
        now = now or datetime.now()
        rows = self._table_to_rows()
        try:
            soft, hard = self.limits()
            pass_end, target_read = live_numbers(rows, self.params, self.storage.get_json("ui_live", {}) or {},
                                                 soft, hard, self.weather_series(), now=now)
        except Exception:  # noqa: BLE001
            pass_end, target_read = None, None
        msgs = reminders_check(rows, now, self._rem_state, cfg, pass_end, target_read)
        for key, title, text in msgs:
            self._notify(title, text)
            self.storage.log_event("reminder", {"key": key})
        return msgs

    # ------------------------------------------------------------------
    # Режим «Сушильщик / Инженер», замечания, пилот
    # ------------------------------------------------------------------
    def apply_mode(self, mode: str, save: bool = True, log: bool = True) -> None:
        """Сушильщик: «Сейчас», «Рециркуляция», «Журнал», «Справка», крупный шрифт, без инженерных кнопок."""
        op = mode == pilot.OPERATOR
        self.mode = mode
        to_journal = op and self.tabs.tabText(self.tabs.currentIndex()) not in pilot.OPERATOR_TABS
        for i in range(self.tabs.count()):
            vis = (not op) or self.tabs.tabText(i) in pilot.OPERATOR_TABS
            self.tabs.setTabVisible(i, vis)
            # скрытые вкладки не должны раздувать минимальный размер окна
            pol = QSizePolicy.Preferred if vis else QSizePolicy.Ignored
            self.tabs.widget(i).setSizePolicy(pol, pol)
        if to_journal:
            self.tabs.setCurrentIndex(next(i for i in range(self.tabs.count()) if self.tabs.tabText(i) == "Журнал"))
        f = QApplication.font()
        f.setPointSizeF(self._base_pt + (2.0 if op else 0.0))
        QApplication.setFont(f)
        if getattr(self, "_ready", False):
            self._apply_theme()       # сводки с размером шрифта от текущего
        for name in ("Импорт CSV/XLSX…", "Сохранить шаблон…", "Сохранить пример…", "Калибровать модель"):
            self.j_buttons[name].setVisible(not op)
        self.j_bottom.setVisible(not op)
        k = 1.35 if op else 1.0
        for le in self.qe.values():
            bw = le.property("base_width") or 0
            if bw:
                le.setFixedWidth(int(bw * k))
        self.qe_add_btn.setMinimumHeight(36 if op else 0)
        self.tab_live.cb_now.setVisible(not op)
        self.tab_live.cb_corr.setVisible(not op)
        self.cmb_mode.blockSignals(True)
        self.cmb_mode.setCurrentIndex(0 if op else 1)
        self.cmb_mode.blockSignals(False)
        self.lbl_mode.setText("Режим сушильщика" if op else "Режим инженера")
        self.setWindowTitle(f"Расчёт времени сушки кукурузы — шахтная сушилка Araj  v{__version__}"
                            + ("  — режим сушильщика" if op else ""))
        if save:
            cfg = self.storage.get_json("ui_mode", {}) or {}
            cfg["mode"] = mode
            self.storage.set_json("ui_mode", cfg)
        if log:
            self.storage.log_event("mode", {"mode": mode})

    def _mode_selected(self, idx: int) -> None:
        want = self.cmb_mode.itemData(idx)
        if want == self.mode:
            return
        if want == pilot.ENGINEER:
            stored = (self.storage.get_json("ui_mode", {}) or {}).get("pin")
            if stored:
                pin, ok = QInputDialog.getText(self, "Режим инженера", "PIN режима инженера:", QLineEdit.Password)
                if not ok or not pilot.pin_ok(pin, stored):
                    if ok:
                        QMessageBox.warning(self, "Режим инженера", "Неверный PIN.")
                    self.apply_mode(self.mode, save=False, log=False)
                    return
        self.apply_mode(want)

    def ask_mode(self) -> None:
        """Первый запуск: кто будет работать с программой."""
        box = QMessageBox(self)
        box.setWindowTitle("Добро пожаловать в DryerCalc")
        box.setText("Кто будет работать с программой на этом компьютере?")
        box.setInformativeText(
            "Сушильщик — только нужное на смене: журнал проб, подсказка разгрузки «Сейчас», рециркуляция, справка; "
            "крупный шрифт.\n\nИнженер — все вкладки: планирование партий, калибровка, партии и отчёты, погода, "
            "настройки.\n\nРежим можно сменить в любой момент — переключатель справа вверху.")
        b_op = box.addButton("Сушильщик", QMessageBox.AcceptRole)
        box.addButton("Инженер", QMessageBox.RejectRole)
        box.exec()
        self.apply_mode(pilot.OPERATOR if box.clickedButton() is b_op else pilot.ENGINEER)
        self.first_run = False

    def _feedback_dialog(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Замечание по программе")
        f = QFormLayout(dlg)
        info = QLabel("Что неудобно, непонятно, работает не так или чего не хватает? Пишите как есть — "
                      "инженер увидит замечание в отчёте пилота.")
        info.setWordWrap(True)
        f.addRow(info)
        last_who = (self.storage.get_json("ui_feedback", {}) or {}).get("who", "")
        who = QLineEdit(last_who)
        who.setPlaceholderText("имя или смена (необязательно)")
        text = QPlainTextEdit()
        text.setPlaceholderText("Например: «на вкладке Сейчас не понял, какую массу вписывать на втором проходе»")
        text.setMinimumHeight(140)
        f.addRow("Кто", who)
        f.addRow("Замечание", text)
        tab_name = self.tabs.tabText(self.tabs.currentIndex())
        f.addRow(QLabel(f"Вкладка: {tab_name}; режим: {'сушильщик' if self.mode == pilot.OPERATOR else 'инженер'}"))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Сохранить")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        dlg.resize(560, 320)
        if dlg.exec() != QDialog.Accepted or not text.toPlainText().strip():
            return
        self.add_feedback(who.text().strip(), text.toPlainText().strip(), tab_name)
        self.statusBar().showMessage("Замечание сохранено. Спасибо!", 8000)

    def add_feedback(self, who: str, text: str, tab: str) -> None:
        self.storage.set_json("ui_feedback", {"who": who})
        self.storage.log_event("feedback", {"who": who, "text": text, "tab": tab, "mode": self.mode})

    def _save_pin(self) -> None:
        pin = self.s_pin.text().strip()
        cfg = self.storage.get_json("ui_mode", {}) or {}
        if not pin:
            QMessageBox.information(self, "PIN", "Впишите PIN (цифры или слово), затем «Сохранить PIN».")
            return
        cfg["pin"] = pilot.pin_hash(pin)
        self.storage.set_json("ui_mode", cfg)
        self.s_pin.clear()
        self._update_pin_info()
        QMessageBox.information(self, "PIN", "PIN сохранён: переход из режима сушильщика в режим инженера будет "
                                             "спрашивать его. Запомните его — восстановить нельзя (только удалить базу "
                                             "настроек или сбросить из режима инженера).")

    def _clear_pin(self) -> None:
        cfg = self.storage.get_json("ui_mode", {}) or {}
        cfg.pop("pin", None)
        self.storage.set_json("ui_mode", cfg)
        self._update_pin_info()

    def _update_pin_info(self) -> None:
        has = bool((self.storage.get_json("ui_mode", {}) or {}).get("pin"))
        self.s_pin_info.setText("PIN задан: переход в режим инженера защищён." if has else
                                "PIN не задан: любой может переключиться в режим инженера.")

    def _pilot_dialog(self) -> None:
        self._save_timer.stop()
        self._save_journal()
        events = self.storage.load_events()
        st = pilot.pilot_stats(events, self._table_to_rows())
        dlg = QDialog(self)
        dlg.setWindowTitle("Пилотный запуск: статистика и замечания")
        v = QVBoxLayout(dlg)
        lab = QLabel(pilot.stats_text(st))
        lab.setWordWrap(True)
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(lab)
        v.addWidget(QLabel(f"Замечания ({len(st['feedback'])}):"))
        t = QTableWidget(len(st["feedback"]), 4)
        t.setHorizontalHeaderLabels(["Когда", "Кто", "Вкладка", "Замечание"])
        t.horizontalHeader().setStretchLastSection(True)
        t.setWordWrap(True)
        for i, e in enumerate(reversed(st["feedback"])):
            d = e["detail"] if isinstance(e["detail"], dict) else {"text": str(e["detail"])}
            for j, txt in enumerate([f"{e['ts']:%d.%m %H:%M}", d.get("who", ""), d.get("tab", ""), d.get("text", "")]):
                t.setItem(i, j, item(txt))
        t.resizeColumnsToContents()
        t.resizeRowsToContents()
        v.addWidget(t, 1)
        bar = QHBoxLayout()
        b1 = QPushButton("Сохранить в Excel…")
        b2 = QPushButton("Очистить статистику (замечания останутся)")
        b3 = QPushButton("Закрыть")
        bar.addWidget(b1)
        bar.addWidget(b2)
        bar.addStretch(1)
        bar.addWidget(b3)
        v.addLayout(bar)

        def save():
            path, _ = QFileDialog.getSaveFileName(dlg, "Отчёт пилота", f"DryerCalc_пилот_{datetime.now():%Y-%m-%d}.xlsx",
                                                  "Excel (*.xlsx)")
            if path:
                pilot.export_xlsx(path, st, events)
                QMessageBox.information(dlg, "Отчёт пилота", f"Сохранено: {path}")

        def clear():
            if QMessageBox.question(dlg, "Статистика", "Удалить журнал событий (запуски, пробы, напоминания)? "
                                                       "Замечания пользователей сохранятся.") == QMessageBox.Yes:
                self.storage.clear_events(keep_feedback=True)
                dlg.accept()

        b1.clicked.connect(save)
        b2.clicked.connect(clear)
        b3.clicked.connect(dlg.accept)
        dlg.resize(900, 600)
        dlg.exec()

    def _notify(self, title: str, text: str) -> None:
        self.statusBar().showMessage(f"Напоминание — {title}: {text}", 15 * 60 * 1000)
        if self._tray is not None:
            self._tray.showMessage(title, text, QSystemTrayIcon.Information, 20000)
        QApplication.alert(self, 0)

    def _auto_calibrate(self) -> None:
        """Тихая перекалибровка после правок журнала (включается на вкладке «Проверка модели»), в фоне."""
        if getattr(self, "_cal_job", None) is not None:
            self._auto_timer.start()          # идёт другая калибровка — попробовать позже
            return
        base = self.params

        def done(res) -> None:
            self._cal_job = None
            new_p, rep = res
            if rep["n_used"] < 12 or self.params is not base:    # мало данных или параметры уже поменяли
                return
            from dataclasses import replace as _replace
            self.set_params(_replace(new_p, calib_info=new_p.calib_info + " (автоматически)"))
            self.report.setPlainText(rep["text"])

        def failed(_e) -> None:
            self._cal_job = None

        self._cal_job = worker.start(calibrate, self._table_to_rows(), base, half_life_days=self.tab_check.half_life(),
                                     weather=self.weather_series(), on_done=done, on_error=failed)

    def _import_journal(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Импорт журнала", "", "Журнал (*.csv *.xlsx *.xlsm)")
        if not path:
            return
        try:
            rows, info = read_journal(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Импорт не удался", str(e))
            return
        self._append_rows(rows)
        self._save_journal()
        self._qe_prefill()
        names = dict(HEADERS_RU, block_pair="T блоков (пара)", grain_pair="T зерна (пара)")
        mapped = "\n".join(f"  {names.get(k, k)}  ←  {v}" for k, v in info["mapped"].items())
        extra = f"\nНе распознаны колонки: {', '.join(info['unmapped'])}" if info["unmapped"] else ""
        QMessageBox.information(
            self, "Импорт выполнен",
            f"Добавлено строк: {len(rows)}; пустых пропущено: {info['skipped']}.\n\nСопоставление колонок:\n{mapped}{extra}")

    def _save_template(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Шаблон журнала", "журнал_шаблон.csv", "CSV (*.csv)")
        if path:
            write_template(path)

    def _save_sample(self) -> None:
        src = resource_path("examples/journal_sample_oct2026.csv")
        if not src.exists():
            QMessageBox.warning(self, "Пример", "Файл примера не найден.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Пример журнала (18–25.10.2026)", "журнал_пример.csv", "CSV (*.csv)")
        if path:
            shutil.copyfile(src, path)

    def _add_row(self) -> None:
        self._loading = True
        try:
            self.tbl_j.insertRow(self.tbl_j.rowCount())
            for j in range(len(FIELDS)):
                self.tbl_j.setItem(self.tbl_j.rowCount() - 1, j, QTableWidgetItem(""))
        finally:
            self._loading = False
        self.tbl_j.scrollToBottom()
        self.tbl_j.setCurrentCell(self.tbl_j.rowCount() - 1, 0)

    def _delete_rows(self) -> None:
        rows = sorted({i.row() for i in self.tbl_j.selectedIndexes()}, reverse=True)
        if rows and self.mode == pilot.OPERATOR and QMessageBox.question(
                self, "Удалить строки", f"Удалить строк журнала: {len(rows)}? Это нельзя отменить "
                                        f"(только восстановлением из резервной копии).") != QMessageBox.Yes:
            return
        for r in rows:
            self.tbl_j.removeRow(r)
        if rows:
            self._save_timer.start()

    def _calibrate(self) -> None:
        """Калибровка в фоне: окно не замирает; по готовности — отчёт и вопрос «Применить?»."""
        if getattr(self, "_cal_job", None) is not None:
            return
        rows = self._table_to_rows()
        self._save_journal()
        btn = self.j_buttons["Калибровать модель"]
        btn.setEnabled(False)
        self.statusBar().showMessage("Калибровка модели по журналу…")
        self._auto_timer.stop()

        def finish() -> None:
            self._cal_job = None
            btn.setEnabled(True)
            self.statusBar().clearMessage()

        def failed(e) -> None:
            finish()
            QMessageBox.warning(self, "Калибровка", str(e))

        def done(res) -> None:
            finish()
            self._calibrated(*res)

        self._cal_job = worker.start(calibrate, rows, self.params, half_life_days=self.tab_check.half_life(),
                                     weather=self.weather_series(), on_done=done, on_error=failed)

    def _calibrated(self, new_p: mdl.DryerParams, rep: Dict) -> None:
        self.report.setPlainText(rep["text"])
        self.plot_cal.clear()
        if rep["pairs"]:
            xs = [a for a, _ in rep["pairs"]]
            ys = [b for _, b in rep["pairs"]]
            hi = max(xs + ys) * 1.05
            self.plot_cal.add_line([0, hi], [0, hi], "ref", 1, dashed=True)
            self.plot_cal.add_points(xs, ys, "dot", 7)
            self.plot_cal.set_title("чем ближе точки к пунктиру, тем точнее модель")
        ans = QMessageBox.question(self, "Калибровка", rep["text"] + "\n\nПрименить новые параметры (обе модели)?")
        if ans == QMessageBox.Yes:
            self.set_params(new_p)
            self.storage.log_event("calibrate", {"rows": rep["n_used"], "mae": round(rep["mae"], 3),
                                                 "seg_mae": round(rep["seg_mae"], 3)})

    # ------------------------------------------------------------------
    # Вкладка «Сушилка и модель»
    # ------------------------------------------------------------------
    def _build_settings_tab(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)

        left = QVBoxLayout()
        gb = QGroupBox("Производительность выгрузки Q(D), т/ч (лист пересчёта, кукуруза, натура 0,65)")
        gv = QVBoxLayout(gb)
        self.s_qtbl = QTableWidget(0, 2)
        self.s_qtbl.setHorizontalHeaderLabels(["Разгрузка D, с", "Q, т/ч"])
        self.s_qtbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        gv.addWidget(self.s_qtbl)
        b = QPushButton("Сбросить таблицу (Q = 0,52 · D)")
        b.clicked.connect(self._reset_qtable)
        gv.addWidget(b)
        left.addWidget(gb)
        lay.addLayout(left, 1)

        inner = QWidget()
        right = QVBoxLayout(inner)
        self.s_mdryer = dspin(1, 500, 60, 0, 1, " т")
        self.s_overhead = dspin(0, 24, 1.0, 2, 0.25, " ч")
        self.s_bias = dspin(-5, 5, 0.8, 2, 0.1, " п.п.")
        self.s_kw = dspin(-0.5, 0.5, 0.0, 3, 0.01)
        self.s_kt = dspin(-0.5, 0.5, 0.0, 3, 0.01)
        self.s_sigma = dspin(0, 50, 5, 1, 0.5, " %")
        self.s_lag = dspin(0, 3, 1.0, 2, 0.05)
        self.s_src = QComboBox()
        self.s_src.addItem("Блоки: (T блока 1 + T блока 2) / 2", "block")
        self.s_src.addItem("Зерно: (T низ + T верх) / 2", "agent")
        self.s_model = QComboBox()
        self.s_model.addItem("Эмпирическая (по журналу; точнее на ваших данных)", "empirical")
        self.s_model.addItem("Физическая (кинетика сушки кукурузы + КПД слоя)", "physics")
        self.s_rref = dspin(0.05, 100, 1.83, 3, 0.1, " т воды/ч")
        self.s_wref = dspin(10, 50, 22, 1, 1, " %")
        self.s_tref = dspin(20, 150, 60, 1, 0.5, " °C")
        self.s_tambref = dspin(-40, 45, 10, 1, 1, " °C")
        self.s_rhref = dspin(10, 100, 70, 0, 5, " %")
        self.s_weq = dspin(0, 20, 8, 1, 0.5, " %")
        self.s_bw = dspin(0, 3, 0.69, 3, 0.05)
        self.s_bdt = dspin(0, 3, 0.87, 3, 0.05)
        self.s_bamb = dspin(-1, 1, 0.03, 3, 0.01)
        self.s_brh = dspin(-3, 3, -0.15, 3, 0.05)
        self.s_eta = dspin(0.01, 10, 0.52, 3, 0.05)
        self.s_fhot = dspin(0.3, 1.0, 0.8, 2, 0.05)
        self.s_kappa = dspin(0.1, 1.0, 1.0, 2, 0.05)
        self.s_tga = dspin(-50, 80, 5.1, 2, 0.5, " °C")
        self.s_tgb = dspin(0, 2, 0.42, 3, 0.01)
        self.s_tgc = dspin(-3, 3, -0.46, 3, 0.05)
        self.s_tagmax = dspin(0, 200, 0, 0, 1, " °C")
        self.s_tagmax.setSpecialValueText("не задано")
        self.s_tgmax = dspin(0, 100, 0, 0, 1, " °C")
        self.s_tgmax.setSpecialValueText("не задано")

        gb = QGroupBox("Сушилка")
        f = QFormLayout(gb)
        f.addRow("Вместимость сушилки", self.s_mdryer)
        f.addRow("Накладные на каждый проход", self.s_overhead)
        f.addRow("Поправка влагомера на выходе (показание − после охлаждения), a", self.s_bias)
        f.addRow("  + k_w · (показание − 20)", self.s_kw)
        f.addRow("  + k_t · (T зерна по датчикам − 30)", self.s_kt)
        f.addRow("Ошибка скорости сушки σ (для допусков ± в прогнозах)", self.s_sigma)
        f.addRow("Лаг выхода: доля времени пребывания", self.s_lag)
        f.addRow("Температура в модели", self.s_src)
        f.addRow("Модель для расчётов", self.s_model)
        right.addWidget(gb)

        gb = QGroupBox("Эмпирическая модель: R = R_ref · x_w^b_w · x_T^b_dt · e^(b_нар·ΔT_нар/10) · e^(b_rh·ΔRH/30)")
        f = QFormLayout(gb)
        f.addRow("R_ref — расход влаги в опорных условиях", self.s_rref)
        f.addRow("  опорная влажность w_ref", self.s_wref)
        f.addRow("  опорная T процесса", self.s_tref)
        f.addRow("  опорная T наружная (и база температуры)", self.s_tambref)
        f.addRow("  опорная RH наружная", self.s_rhref)
        f.addRow("Равновесная влажность w_eq", self.s_weq)
        f.addRow("Показатель по влажности b_w", self.s_bw)
        f.addRow("Показатель по температуре b_dt", self.s_bdt)
        f.addRow("Коэффициент по T наружной b_нар (на 10 °C)", self.s_bamb)
        f.addRow("Коэффициент по RH b_rh (на 30 %)", self.s_brh)
        right.addWidget(gb)

        gb = QGroupBox("Физическая модель (Thompson, 1968: тонкий слой кукурузы) — см. «Справка»")
        f = QFormLayout(gb)
        f.addRow("КПД слоя η (калибруется по журналу)", self.s_eta)
        f.addRow("Доля горячей зоны в объёме сушилки", self.s_fhot)
        f.addRow("κ — доля перепада T агента − T мокр. в слое (1 = T агента)", self.s_kappa)
        right.addWidget(gb)

        gb = QGroupBox("Температура зерна и ограничения")
        f = QFormLayout(gb)
        f.addRow("T зерна ≈ a + b · T блоков + c · (W вх − 22):  a", self.s_tga)
        f.addRow("  b", self.s_tgb)
        f.addRow("  c", self.s_tgc)
        b = QPushButton("Подобрать a, b, c по журналу (датчики T зерна)")
        b.clicked.connect(self._fit_grain_temp)
        f.addRow(b)
        f.addRow("Предел T блоков (агента) — по инструкции для вашего назначения зерна", self.s_tagmax)
        f.addRow("Предел нагрева зерна", self.s_tgmax)
        right.addWidget(gb)

        bar = QHBoxLayout()
        b1 = QPushButton("Применить и сохранить")
        b1.clicked.connect(self._apply_settings)
        b2 = QPushButton("Сбросить модель к стартовым значениям")
        b2.clicked.connect(self._reset_settings)
        bar.addWidget(b1)
        bar.addWidget(b2)
        bar.addStretch(1)
        right.addLayout(bar)
        self.s_info = QLabel()
        self.s_info.setWordWrap(True)
        right.addWidget(self.s_info)

        gb = QGroupBox("Напоминания сушильщику (уведомление Windows у часов, пока программа открыта)")
        f = QFormLayout(gb)
        cfg = ReminderCfg.from_dict(self.storage.get_json("reminders", {}))
        self.r_on = QCheckBox("Включены")
        self.r_on.setChecked(cfg.enabled)
        self.r_sample = dspin(5, 240, cfg.sample_min, 0, 5, " мин")
        self.r_grace = dspin(0, 120, cfg.grace_min, 0, 5, " мин")
        self.r_lead = dspin(0, 600, cfg.pass_lead_min, 0, 10, " мин")
        self.r_dev = dspin(0.1, 10, cfg.deviation_pp, 1, 0.1, " п.п.")
        f.addRow(self.r_on)
        f.addRow("«Пора внести пробу»: интервал проб", self.r_sample)
        f.addRow("  напомнить, если проба опоздала больше чем на", self.r_grace)
        f.addRow("«Проход скоро закончится»: предупредить за", self.r_lead)
        f.addRow("«Выход ушёл от цели»: если отличие больше", self.r_dev)
        b = QPushButton("Проверить сейчас")
        b.clicked.connect(self._test_reminders)
        f.addRow(b)
        for wdg in (self.r_sample, self.r_grace, self.r_lead, self.r_dev):
            wdg.valueChanged.connect(self._save_reminders)
        self.r_on.stateChanged.connect(self._save_reminders)
        right.addWidget(gb)

        gb = QGroupBox("Режим сушильщика и пилотный запуск")
        f = QFormLayout(gb)
        lab = QLabel("Режим сушильщика (переключатель справа вверху) оставляет только «Сейчас», «Рециркуляция», "
                     "«Журнал» и «Справка», крупным шрифтом. PIN защищает от случайного перехода в режим инженера. "
                     "Кнопка «Замечание…» справа вверху — для отзывов сушильщиков; их и статистику использования "
                     "(покрытие проб, реакция на напоминания) показывает «Статистика и замечания пилота».")
        lab.setWordWrap(True)
        f.addRow(lab)
        self.s_pin = QLineEdit()
        self.s_pin.setEchoMode(QLineEdit.Password)
        self.s_pin.setPlaceholderText("новый PIN")
        hb = QHBoxLayout()
        hb.addWidget(self.s_pin)
        for text, slot in (("Сохранить PIN", self._save_pin), ("Убрать PIN", self._clear_pin)):
            bb = QPushButton(text)
            bb.clicked.connect(slot)
            hb.addWidget(bb)
        f.addRow("PIN режима инженера", hb)
        self.s_pin_info = QLabel()
        f.addRow(self.s_pin_info)
        bb = QPushButton("Статистика и замечания пилота…")
        bb.clicked.connect(self._pilot_dialog)
        f.addRow(bb)
        right.addWidget(gb)
        self._update_pin_info()

        gb = QGroupBox("Резервные копии (журнал, погода, партии, настройки — всё в одном файле базы)")
        gv = QVBoxLayout(gb)
        self.s_backup_info = QLabel()
        self.s_backup_info.setWordWrap(True)
        gv.addWidget(self.s_backup_info)
        hb = QHBoxLayout()
        for text, slot in (("Сохранить копию базы… (на флешку)", self._backup_save),
                           ("Восстановить из копии…", self._backup_restore),
                           ("Показать папку данных", self._open_data_dir)):
            bb = QPushButton(text)
            bb.clicked.connect(slot)
            hb.addWidget(bb)
        hb.addStretch(1)
        gv.addLayout(hb)
        right.addWidget(gb)
        right.addStretch(1)

        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(inner)
        lay.addWidget(sc, 2)
        self._update_backup_info()
        return w

    def _save_reminders(self, *_a) -> None:
        cfg = ReminderCfg(enabled=self.r_on.isChecked(), sample_min=self.r_sample.value(), grace_min=self.r_grace.value(),
                          pass_lead_min=self.r_lead.value(), deviation_pp=self.r_dev.value())
        self.storage.set_json("reminders", cfg.to_dict())

    def _test_reminders(self) -> None:
        self._rem_state.clear()
        msgs = self._check_reminders()
        if not msgs:
            QMessageBox.information(self, "Напоминания",
                                    "Сейчас напоминать нечего: сушка не идёт (последняя запись журнала старше 8 ч или "
                                    "проход закончен), проба не опоздала, выход в пределах порога.")

    def _update_backup_info(self) -> None:
        try:
            items = bkp.list_backups(self.storage.path)
        except OSError:
            items = []
        last = items[0].name if items else "нет"
        self.s_backup_info.setText(
            f"База: {self.storage.path}\nАвтоматические копии: раз в сутки при запуске, хранятся последние {bkp.KEEP_DAYS} "
            f"(папка backup рядом с базой). Последняя: {last}; всего копий: {len(items)}.")

    def _backup_save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Копия базы DryerCalc", f"DryerCalc_{datetime.now():%Y-%m-%d}.db",
                                              "База DryerCalc (*.db)")
        if not path:
            return
        self._save_timer.stop()
        self._save_journal()
        try:
            bkp.copy_db(self.storage.conn, path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Копия базы", f"Не удалось сохранить: {e}")
            return
        QMessageBox.information(self, "Копия базы", f"Сохранено: {path}\nНа другом компьютере: «Восстановить из копии…».")

    def _backup_restore(self) -> None:
        start_dir = str(bkp.backup_dir(self.storage.path))
        path, _ = QFileDialog.getOpenFileName(self, "Восстановить базу из копии", start_dir, "База DryerCalc (*.db)")
        if not path:
            return
        err = bkp.check_db(path)
        if err:
            QMessageBox.warning(self, "Восстановление", f"Файл не подходит: {err}.")
            return
        if QMessageBox.question(self, "Восстановление",
                                "Заменить журнал, погоду, партии и настройки содержимым копии?\n"
                                "Текущее состояние перед этим будет сохранено в папку backup.") != QMessageBox.Yes:
            return
        self._save_timer.stop()
        self._save_journal()
        safety = bkp.safety_copy(self.storage.conn, self.storage.path)
        try:
            bkp.restore_into(self.storage.conn, path)
            self.storage.migrate()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Восстановление", f"Не удалось: {e}\nТекущая база сохранена: {safety}")
            return
        self._reload_all()
        QMessageBox.information(self, "Восстановление", f"Готово. Прежнее состояние сохранено в:\n{safety}")

    def _reload_all(self) -> None:
        self._ready = False
        self.params = self.storage.load_params()
        self.tbl_j.setRowCount(0)
        self._load_journal_into_table()
        self._load_ui_state()
        self._load_simple_state()
        self._fill_settings()
        self._weather = None
        self.tab_data._load()
        self.tab_batches._load()
        self.tab_live._load()
        self._ready = True
        self._update_banner()
        self._update_backup_info()
        self.recalc()
        self.recalc_simple()
        self.tab_check.mark_dirty()

    def _open_data_dir(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.storage.path.parent)))

    def _fit_grain_temp(self) -> None:
        from .calibration import _solve, prepare
        prep, _ = prepare(self._table_to_rows())
        xs, ys = [], []
        for r in prep:
            if None in (r.get("t_a1"), r.get("t_a2"), r.get("t_up_f"), r.get("t_low_f"), r.get("w_in")):
                continue
            xs.append((1.0, 0.5 * (r["t_up_f"] + r["t_low_f"]), r["w_in"] - 22.0))
            ys.append(0.5 * (r["t_a1"] + r["t_a2"]))
        if len(xs) < 10:
            QMessageBox.information(self, "T зерна", f"Строк с T зерна, T блоков и влагой на входе: {len(xs)} (нужно ≥ 10).")
            return
        a = [[sum(x[i] * x[j] for x in xs) + (1e-6 if i == j else 0.0) for j in range(3)] for i in range(3)]
        b = [sum(x[i] * y for x, y in zip(xs, ys)) for i in range(3)]
        try:
            k = _solve(a, b)
        except ValueError:
            QMessageBox.information(self, "T зерна", "Нет разброса T блоков или влажности — подобрать нельзя.")
            return
        res = [y - (k[0] + k[1] * x[1] + k[2] * x[2]) for x, y in zip(xs, ys)]
        rmse = (sum(r * r for r in res) / len(res)) ** 0.5
        self.s_tga.setValue(k[0])
        self.s_tgb.setValue(k[1])
        self.s_tgc.setValue(k[2])
        QMessageBox.information(self, "T зерна", f"По {len(xs)} строкам: T зерна ≈ {k[0]:.1f} + {k[1]:.3f}·T блоков "
                                                 f"{k[2]:+.2f}·(W вх − 22), разброс ±{rmse:.1f} °C.\n"
                                                 f"Нажмите «Применить и сохранить».")

    def _fill_qtable(self, table: Optional[List[List[float]]]) -> None:
        pts = table or [[d, round(0.52 * d, 2)] for d in range(10, 101, 5)]
        self.s_qtbl.setRowCount(len(pts))
        for i, (d, q) in enumerate(pts):
            self.s_qtbl.setItem(i, 0, QTableWidgetItem(f"{d:g}"))
            self.s_qtbl.setItem(i, 1, QTableWidgetItem(f"{q:g}"))

    def _reset_qtable(self) -> None:
        self._fill_qtable(None)

    def _read_qtable(self) -> Optional[List[List[float]]]:
        pts: List[List[float]] = []
        for i in range(self.s_qtbl.rowCount()):
            a, b = self.s_qtbl.item(i, 0), self.s_qtbl.item(i, 1)
            d = to_float(a.text()) if a else None
            q = to_float(b.text()) if b else None
            if d is not None and q is not None:
                pts.append([d, q])
        pts.sort()
        if len(pts) < 2 or any(pts[i][1] >= pts[i + 1][1] for i in range(len(pts) - 1)):
            return None
        return pts

    def _fill_settings(self) -> None:
        p = self.params
        self._fill_qtable(p.q_table)
        self.s_mdryer.setValue(p.m_dryer)
        self.s_overhead.setValue(p.overhead_h)
        self.s_bias.setValue(p.out_bias)
        self.s_kw.setValue(p.out_kw)
        self.s_kt.setValue(p.out_kt)
        self.s_sigma.setValue(p.rate_sigma * 100.0)
        self.s_sigma.setToolTip(f"Источник: {p.rate_sigma_info}")
        self.s_lag.setValue(p.lag_k)
        self.s_src.setCurrentIndex(0 if p.temp_source == "block" else 1)
        self.s_rref.setValue(p.r_ref)
        self.s_wref.setValue(p.w_ref)
        self.s_tref.setValue(p.t_ref)
        self.s_tambref.setValue(p.t_amb_ref)
        self.s_rhref.setValue(p.rh_ref)
        self.s_weq.setValue(p.w_eq)
        self.s_bw.setValue(p.b_w)
        self.s_bdt.setValue(p.b_dt)
        self.s_bamb.setValue(p.b_amb)
        self.s_brh.setValue(p.b_rh)
        self.s_model.setCurrentIndex(1 if p.model_kind == "physics" else 0)
        self.s_eta.setValue(p.eta)
        self.s_fhot.setValue(p.f_hot)
        self.s_kappa.setValue(p.kappa)
        self.s_tga.setValue(p.tg_a)
        self.s_tgb.setValue(p.tg_b)
        self.s_tgc.setValue(p.tg_c)
        self.s_tagmax.setValue(p.t_agent_max)
        self.s_tgmax.setValue(p.t_grain_max)

    # всё, от чего зависит смысл R_ref: смена любого пункта делает калибровку неактуальной
    _MODEL_KEYS = ("r_ref", "w_ref", "t_ref", "t_amb_ref", "rh_ref", "w_eq", "b_w", "b_dt", "b_rh", "b_amb",
                   "temp_source", "out_bias", "out_kw", "out_kt", "lag_k", "m_dryer")

    _PHYS_KEYS = ("eta", "f_hot", "kappa", "temp_source", "out_bias", "out_kw", "out_kt", "lag_k", "m_dryer")

    def _num_fields(self) -> Dict[str, QDoubleSpinBox]:
        return {"m_dryer": self.s_mdryer, "overhead_h": self.s_overhead, "out_bias": self.s_bias, "lag_k": self.s_lag,
                "r_ref": self.s_rref, "w_ref": self.s_wref, "t_ref": self.s_tref, "t_amb_ref": self.s_tambref,
                "rh_ref": self.s_rhref, "w_eq": self.s_weq, "b_w": self.s_bw, "b_dt": self.s_bdt, "b_rh": self.s_brh,
                "b_amb": self.s_bamb, "eta": self.s_eta, "f_hot": self.s_fhot, "kappa": self.s_kappa,
                "tg_a": self.s_tga, "tg_b": self.s_tgb, "tg_c": self.s_tgc, "t_agent_max": self.s_tagmax,
                "t_grain_max": self.s_tgmax, "out_kw": self.s_kw, "out_kt": self.s_kt}

    def _apply_settings(self) -> None:
        old = self.params
        table = self._read_qtable()
        if table is None:
            QMessageBox.warning(self, "Таблица Q(D)", "Нужно минимум две строки, Q должна строго расти с D.")
            return
        # окошки показывают округлённые значения: если пользователь поле не трогал, оставляем точное
        # (например, откалиброванный R_ref = 1,8344, а не 1,83)
        vals = {}
        for key, spin in self._num_fields().items():
            cur = getattr(old, key)
            vals[key] = cur if abs(round(cur, spin.decimals()) - spin.value()) < 1e-9 else spin.value()
        tmp = mdl.DryerParams(q_table=table)
        sigma = old.rate_sigma if abs(round(old.rate_sigma * 100, 1) - self.s_sigma.value()) < 1e-9 \
            else self.s_sigma.value() / 100.0
        sigma_info = old.rate_sigma_info if sigma == old.rate_sigma else "задано вручную"
        new = mdl.DryerParams(q100=mdl.q_at(tmp, 100.0), q_table=table, temp_source=self.s_src.currentData(),
                              calibrated=old.calibrated, calib_info=old.calib_info, phys_info=old.phys_info,
                              model_kind=self.s_model.currentData(), rate_sigma=sigma, rate_sigma_info=sigma_info,
                              # данные для допусков описывают журнал, а не поля формы — переносим как есть
                              unc_feats=old.unc_feats, unc_cov=old.unc_cov, unc_lev=old.unc_lev,
                              struct_base=old.struct_base, explored=old.explored,
                              **vals)
        q_changed = any(abs(mdl.q_at(new, d) - mdl.q_at(old, d)) > 1e-6 for d in (20, 35, 60, 100))
        if q_changed or any(getattr(new, k) != getattr(old, k) for k in self._MODEL_KEYS):
            new.calibrated = False
            new.calib_info = "параметры изменены вручную"
        if q_changed or any(getattr(new, k) != getattr(old, k) for k in self._PHYS_KEYS):
            new.phys_info = "параметры изменены вручную — перекалибруйте"
        if new.model_kind == "physics" and new.temp_source != "block":
            QMessageBox.information(self, "Физическая модель",
                                    "Физической модели нужна температура агента сушки — источник температуры "
                                    "переключён на «Блоки».")
            new.temp_source = "block"
        self.sm_over.setValue(new.overhead_h)
        self.set_params(new)

    def _reset_settings(self) -> None:
        if QMessageBox.question(self, "Сброс", "Вернуть параметры модели к стартовым значениям "
                                "(ваша калибровка будет потеряна)?") == QMessageBox.Yes:
            self.set_params(mdl.DryerParams())

    def _update_banner(self) -> None:
        p = self.params
        self.lbl_tproc.setText("T блоков (средняя уставка)" if p.temp_source == "block" else "T зерна (низ + верх) / 2")
        if p.model_kind == "physics":
            manual = "вручную" in p.phys_info
            self.banner.setText(
                f"Расчёт по ФИЗИЧЕСКОЙ модели (тонкий слой Thompson + КПД слоя η = {p.eta:.2f}): {p.phys_info}. "
                f"На журнале октября она точнее эмпирической не оказалась — сравнивайте на вкладке «Проверка модели».")
            bg, fg = theme_for(self).banner("red" if manual else "amber")
            self.banner.setStyleSheet(f"background:{bg}; color:{fg}; padding:6px;")
            self.s_info.setText(f"Физическая модель: {p.phys_info}. Эмпирическая: {p.calib_info}.")
            return
        if p.calibrated and p.calib_info.startswith("стартовая"):
            self.banner.setText(
                f"Стартовая калибровка ({p.calib_info[len('стартовая: '):]}). Для своего режима загрузите журнал "
                f"на вкладке «Журнал» и нажмите «Калибровать модель».")
            bg, fg = theme_for(self).banner("amber")
            self.banner.setStyleSheet(f"background:{bg}; color:{fg}; padding:6px;")
            self.s_info.setText("Статус: " + p.calib_info.rstrip(".") + ".")
        elif p.calibrated:
            self.banner.setText(f"Модель откалибрована по журналу: {p.calib_info}")
            bg, fg = theme_for(self).banner("green")
            self.banner.setStyleSheet(f"background:{bg}; color:{fg}; padding:6px;")
            self.s_info.setText(f"Статус: откалибровано ({p.calib_info}).")
        else:
            self.banner.setText(
                "⚠ Параметры модели изменены вручную и не проверены по журналу: прогноз съёма приблизительный. "
                "Нажмите «Калибровать модель» на вкладке «Журнал» или сбросьте к стартовым значениям.")
            bg, fg = theme_for(self).banner("red")
            self.banner.setStyleSheet(f"background:{bg}; color:{fg}; padding:6px; font-weight:bold;")
            self.s_info.setText("Статус: не откалибровано (изменено вручную).")

    # ------------------------------------------------------------------
    # Тема (светлая / тёмная)
    # ------------------------------------------------------------------
    def _apply_theme(self) -> None:
        """Цвета надписей, сводки и баннера под текущую системную тему; таблицы и графики перерисовываются."""
        th = theme_for(self)
        self._theme_dark = th.dark
        muted = f"color:{th.muted};"
        err = f"color:{th.error};"
        self.sm_q_hint.setStyleSheet(muted)
        self.sm_note.setStyleSheet(muted)
        self.sm_status.setStyleSheet(err)
        self.status.setStyleSheet(err)
        self.sm_summary.setStyleSheet(
            f"background:{th.summary_bg}; color:{th.summary_fg}; padding:10px; font-size:{QApplication.font().pointSizeF() + 2:.1f}pt;")
        self._update_banner()
        if getattr(self, "_ready", False):
            self.recalc()
            self.recalc_simple()
            for plot in (self.sm_plot, self.plot, self.plot_cal):
                plot.update()
            self.tab_check.mark_dirty()
            self._tab_changed(self.tabs.currentIndex())

    def _theme_changed(self) -> None:
        ensure_style_for_palette()
        if theme_for(self).dark != getattr(self, "_theme_dark", None):
            self._apply_theme()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.PaletteChange, QEvent.ThemeChange) and getattr(self, "_ready", False):
            QTimer.singleShot(120, self._theme_changed)     # палитра применяется чуть позже самого события

    # ------------------------------------------------------------------
    # Состояние интерфейса
    # ------------------------------------------------------------------
    def _ui_widgets(self) -> Dict[str, QDoubleSpinBox]:
        return {"mass": self.in_mass, "w0": self.in_w0, "wk": self.in_wk, "tproc": self.in_tproc,
                "tamb": self.in_tamb, "rh": self.in_rh, "soft": self.in_soft, "hard": self.in_hard,
                "dfix": self.in_dfix, "fuel": self.in_fuel, "price": self.in_fuel_price, "lhv": self.in_lhv}

    def _save_ui_state(self) -> None:
        d = {k: w.value() for k, w in self._ui_widgets().items()}
        d["manual"] = self.in_manual.text()
        d["tlist"] = self.in_tlist.text()
        d["fuel_unit"] = self.in_fuel_unit.text()
        d["cool"] = self.in_cool.isChecked()
        self.storage.set_json("ui", d)

    def _load_ui_state(self) -> None:
        d = self.storage.get_json("ui", {}) or {}
        for k, w in self._ui_widgets().items():
            if k in d:
                w.setValue(float(d[k]))
        self.in_manual.setText(d.get("manual", ""))
        self.in_tlist.setText(d.get("tlist", ""))
        self.in_fuel_unit.setText(d.get("fuel_unit", "м³"))
        if "cool" in d:
            self.in_cool.setChecked(bool(d["cool"]))

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            worker.cancel_all()
            worker.wait_all(5000)
            self._save_timer.stop()
            self._save_journal()
            self._save_ui_state()
            self._save_simple_state()
        finally:
            super().closeEvent(event)


def _install_excepthook() -> None:
    """В окне без консоли необработанные ошибки иначе пропадают молча: показываем и пишем в error.log."""
    def hook(exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log = None
        try:
            log = default_dir() / "error.log"
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S}  v{__version__} ---\n{text}")
        except OSError:
            pass
        try:
            where = f"\n\nПодробности записаны в файл:\n{log}" if log else ""
            QMessageBox.critical(None, "Ошибка в программе",
                                 f"Операция не выполнена: {exc_type.__name__}: {exc}.{where}\n\n"
                                 f"Журнал и настройки сохранены в базе, программу можно продолжать использовать.")
        except Exception:  # noqa: BLE001
            pass
    sys.excepthook = hook


def main() -> None:
    if sys.platform == "win32":
        try:  # своя иконка в панели задач, а не иконка Python
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ExQII.DryerCalc")
        except Exception:  # noqa: BLE001
            pass
    app = QApplication.instance() or QApplication(sys.argv)
    ensure_style_for_palette(app)
    _install_excepthook()
    win = MainWindow()
    win.show()
    if win.first_run:
        QTimer.singleShot(400, win.ask_mode)
    sys.exit(app.exec())
