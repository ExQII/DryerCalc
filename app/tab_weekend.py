"""Раздел «Масса на выходные»: сколько сырого зерна намолотить, чтобы сушилка работала, пока хозяйство отдыхает.

Слева — окно работы сушилки (начало сушки выходной партии, выход хозяйства на работу, плановые остановки), зерно
(влажность с поля, цель, уже намолочено, ёмкости) и как считать. Начало можно взять по журналу (когда закончится
текущая партия, как в «Текущий проход») или после очереди партий. Справа — ответ крупно, четыре плитки (окно,
проходы, темп, сколько сырое зерно ждёт 1-го прохода) и вкладками: варианты массы с допуском 90 %, проходы по
времени и полосы на шкале времени с отметкой выхода хозяйства. Расчёт — weekend.weekend_mass.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtGui import QBrush, QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDateTimeEdit, QFormLayout, QFrame, QGroupBox, QHBoxLayout,
                               QLabel, QPushButton, QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from . import model as mdl
from .batches import QueueEntry
from .tab_batches import GanttChart
from .theme import theme_for
from .ui_common import (Collapsible, Tile, dspin, fmt_hours, item, mute, parse_decimals, scroll_wrap, set_role,
                        size_by_current, table)
from .weekend import STORAGE_SOURCE, WeekendInput, WeekendResult, next_weekday_time, weekend_mass
from .widgets import Glass

if TYPE_CHECKING:
    from .gui import MainWindow

DOW = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def when(t: datetime) -> str:
    """«пт 06.11 18:00»."""
    return f"{DOW[t.weekday()]} {t:%d.%m %H:%M}"


def tonnes(m: float, how: str = "round") -> str:
    """Масса для решения — до 10 т: «точно успеть» вниз, «без простоя» вверх, остальное — до ближайших."""
    f = {"down": math.floor, "up": math.ceil}.get(how, round)
    return f"{int(f(m / 10.0) * 10):,}".replace(",", " ")


def _days(d: float) -> str:
    return f"{d:.1f}" if d < 10 else f"{d:.0f}"


class WeekendTab(QWidget):
    TINT = {0: "green", 1: "gold", 2: "rust"}

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.result: Optional[WeekendResult] = None
        self.inp: Optional[WeekendInput] = None
        self._start_auto = True          # начало подставляет программа, пока человек не задал его сам
        self._end_auto = True
        self._prog = False               # поля меняет программа — это не правка человека
        self._j_end: Optional[datetime] = None
        self._q_end: Optional[datetime] = None
        d = win.storage.get_json("ui_weekend", {}) or {}
        self._ret = (int(d.get("ret_dow", 0)), int(d.get("ret_h", 8)), int(d.get("ret_m", 0)))

        lay = QHBoxLayout(self)
        left = QWidget()
        lv = QVBoxLayout(left)

        gb = QGroupBox("Окно работы сушилки")
        f = QFormLayout(gb)
        self.start = QDateTimeEdit(QDateTime.currentDateTime())
        self.start.setCalendarPopup(True)
        self.start.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.lbl_start_src = mute(QLabel())
        self.lbl_start_src.setWordWrap(True)
        self.b_journal = set_role(QPushButton(), "link")
        self.b_journal.setCursor(Qt.PointingHandCursor)
        self.b_journal.clicked.connect(lambda: self._take_start("journal"))
        self.b_queue = set_role(QPushButton(), "link")
        self.b_queue.setCursor(Qt.PointingHandCursor)
        self.b_queue.clicked.connect(lambda: self._take_start("queue"))
        self.end = QDateTimeEdit(QDateTime.currentDateTime())
        self.end.setCalendarPopup(True)
        self.end.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.stops = dspin(0, 48, float(d.get("stops", 0.0)), 1, 0.5, " ч")
        self.stops.setToolTip("Зачистка, ТО, другие остановки сушилки за выходные — их время вычитается из окна.")
        f.addRow("Начало сушки партии", self.start)
        f.addRow(self.lbl_start_src)
        f.addRow(self.b_journal)
        f.addRow(self.b_queue)
        f.addRow("Выход хозяйства на работу", self.end)
        f.addRow("Остановки сушилки", self.stops)
        lv.addWidget(gb)

        gb = QGroupBox("Зерно")
        f = QFormLayout(gb)
        self.w0 = dspin(6, 50, float(d.get("w0", win.in_w0.value())), 1, 0.5, " %")
        self.wk = dspin(5, 30, float(d.get("wk", win.in_wk.value())), 1, 0.5, " %")
        self.already = dspin(0, 20000, 0, 0, 10, " т")
        self.already.setToolTip("Сколько зерна для этой партии уже лежит в ёмкостях — программа скажет, сколько "
                                "домолотить.")
        self.bins = QSpinBox()
        self.bins.setRange(0, 20)
        self.bins.setValue(int(d.get("bins", 2)))
        self.bins.setSpecialValueText("не проверять")
        self.bins.setToolTip("Сколько оперативных ёмкостей (силосов) под сырое зерно этой партии и сколько тонн в "
                             "каждой — программа проверит, поместится ли масса.")
        self.bin_t = dspin(10, 5000, float(d.get("bin_t", 500.0)), 0, 50, " т")
        hb = QHBoxLayout()
        hb.setContentsMargins(0, 0, 0, 0)
        hb.addWidget(self.bins)
        hb.addWidget(QLabel("×"))
        hb.addWidget(self.bin_t, 1)
        f.addRow("Влажность с поля W₀", self.w0)
        f.addRow("Цель Wк (после охлаждения)", self.wk)
        f.addRow("Уже намолочено", self.already)
        f.addRow("Ёмкости под партию", hb)
        lv.addWidget(gb)

        gb = QGroupBox("Как считать")
        f = QFormLayout(gb)
        self.method = QComboBox()
        self.method.addItem("По модели сушилки", "model")
        self.method.addItem("По производительности", "q")
        if d.get("method") == "q":
            self.method.setCurrentIndex(1)
        self.q = dspin(1, 200, float(d.get("q", 18.2)), 1, 0.5, " т/ч")
        self.lim = dspin(1, 25, float(d.get("lim", 6.0)), 1, 0.5, " п.п.")
        self.cool = QCheckBox("Финальное охлаждение на D = 100")
        self.cool.setChecked(bool(d.get("cool", True)))
        self.npass = QSpinBox()
        self.npass.setRange(0, 10)
        self.npass.setSpecialValueText("авто — по лимиту съёма")
        self.npass.setSuffix(" прох.")
        self.npass.setValue(int(d.get("passes", 0) or 0))
        self.npass.setToolTip("Сколько проходов сделаете с выходной партией. «Авто» — наименьшее число проходов в "
                              "желаемом лимите съёма (по модели) или по лимиту съёма (по производительности).")
        c = win.conditions()
        self.t_amb = dspin(-40, 45, c.t_amb, 1, 1, " °C")
        self.rh = dspin(5, 100, c.rh, 0, 5, " %")
        self.b_weather = set_role(QPushButton("T и RH на выходные — из таблицы погоды"), "link")
        self.b_weather.setCursor(Qt.PointingHandCursor)
        self.b_weather.clicked.connect(self._from_weather)
        self.lbl_cond = mute(QLabel())
        self.lbl_cond.setWordWrap(True)
        f.addRow("Метод", self.method)
        f.addRow("Q выгрузки", self.q)
        f.addRow("Лимит съёма за проход", self.lim)
        f.addRow("Число проходов", self.npass)
        f.addRow(self.cool)
        f.addRow("Воздух на выходных, T", self.t_amb)
        f.addRow("Воздух на выходных, RH", self.rh)
        f.addRow(self.b_weather)
        f.addRow(self.lbl_cond)
        lv.addWidget(gb)
        self.help = Collapsible(
            "Как считается",
            "Обратная задача плана партии: не «сколько часов сушить эту массу», а «сколько тонн сушилка успеет "
            "высушить за окно». Окно — от начала сушки выходной партии до выхода хозяйства минус плановые остановки. "
            "Время партии растёт с массой по прямой: часы на тонну по проходам (масса на выходе прохода ÷ Q) плюс "
            "охлаждение, и постоянные накладные на каждый проход. Число проходов и D от массы не зависят — их "
            "выбирает съём за проход, как в «План сушки партии». Поэтому масса = (окно − накладные) ÷ часы на "
            "тонну.<br><br>Три варианта массы — из допуска 90 % на скорость сушки (тот же, что в «План сушки "
            "партии»). «Точно успеть» — масса, которую сушилка закончит к выходу даже при медленной сушке: ёмкости "
            "к понедельнику свободны, но при обычной сушке она освободится раньше и постоит. «Без простоя» — "
            "сушилка не встанет даже при быстрой сушке, но при обычной закончит позже выхода, и свежее зерно "
            "подождёт в силосе.<br><br>Сырое зерно до 1-го прохода лежит и портится. Ориентир — допустимый срок "
            "хранения сырого зерна при температуре зерна ≈ наружного воздуха: " + STORAGE_SOURCE + ". Таблица — "
            "для зерна, которое держат холодным (вентиляция); без вентиляции сырое зерно греется и портится "
            "быстрее, а время до начала партии (от уборки) тоже идёт в зачёт.")
        mute(self.help.content)
        lv.addWidget(self.help)
        lv.addStretch(1)
        left.setMaximumWidth(420)

        # ---- ответ: стеклянная карточка, плитки, вкладки ----
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(4)
        self.card = Glass(radius=18, blur=10, dy=3)
        self.card.setObjectName("weekendCard")
        gl, gt, gr, gbm = self.card.shadow_margins()
        cv = QVBoxLayout(self.card)
        cv.setContentsMargins(gl + 16, gt + 8, gr + 16, gbm + 8)
        cv.setSpacing(4)
        head = QHBoxLayout()
        self.lbl_head = set_role(QLabel("—"), "adviceHead")
        self.lbl_head.setWordWrap(True)
        head.addWidget(self.lbl_head, 1, Qt.AlignVCenter)
        self.b_add = QPushButton("Добавить в очередь партий")
        self.b_add.setToolTip("Расчётная масса — строкой в «Партии и очередь» → «Очередь и смешивание», готова к "
                              "сушке с начала окна.")
        self.b_add.clicked.connect(self._add_to_queue)
        head.addWidget(self.b_add, 0, Qt.AlignVCenter)
        cv.addLayout(head)
        self.lbl_sub = set_role(QLabel(), "adviceSub")
        self.lbl_sub.setWordWrap(True)
        self.lbl_sub.setTextFormat(Qt.RichText)
        cv.addWidget(self.lbl_sub)
        self.lbl_warn = set_role(QLabel(), "warn")
        self.lbl_warn.setWordWrap(True)
        self.lbl_warn.setTextFormat(Qt.RichText)
        self.lbl_warn.setVisible(False)
        cv.addWidget(self.lbl_warn)
        rv.addWidget(self.card)
        tiles = QHBoxLayout()
        tiles.setContentsMargins(gl, 0, gr, 0)
        tiles.setSpacing(8)
        self.t_win = Tile("Сушилке работать")
        self.t_pass = Tile("Проходы")
        self.t_rate = Tile("Темп сушки партии")
        self.t_wet = Tile("Сырое ждёт 1-го прохода")
        self.tiles = (self.t_win, self.t_pass, self.t_rate, self.t_wet)
        for t in self.tiles:
            tiles.addWidget(t, 1)
        rv.addLayout(tiles)
        rv.addSpacing(6)
        self.views = QTabWidget()
        self.views.setDocumentMode(True)
        ow = QWidget()
        ov = QVBoxLayout(ow)
        ov.setContentsMargins(0, 0, 0, 0)
        ov.setSpacing(6)
        self.tbl_opt = table(["Вариант", "Масса, т", "Домолотить, т", "Окончание", "90 %: быстрее … медленнее"])
        self.tbl_opt.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        ov.addWidget(self.tbl_opt)
        self.lbl_opt = QLabel()                 # что будет при каждом варианте — строками под таблицей
        self.lbl_opt.setWordWrap(True)
        self.lbl_opt.setTextFormat(Qt.RichText)
        self.lbl_opt.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        ov.addWidget(Collapsible("Чем варианты отличаются", self.lbl_opt))
        ov.addStretch(1)
        osc = QScrollArea()                     # на низком экране — прокрутка, а не наложение подписи на таблицу
        osc.setWidget(ow)
        osc.setWidgetResizable(True)
        osc.setFrameShape(QFrame.NoFrame)
        self.views.addTab(osc, "Варианты массы")
        self.tbl_pass = table(["Проход", "W вход → выход, %", "D, с", "Q, т/ч", "Начало", "Окончание", "Часов",
                               "Масса на выходе, т"])
        self.views.addTab(self.tbl_pass, "Проходы по времени")
        gw = QWidget()
        gv = QVBoxLayout(gw)
        gv.setContentsMargins(0, 0, 0, 0)
        gv.setSpacing(4)
        lg = mute(QLabel("Полосы — проходы (через один — светлее) и охлаждение; усик — окончание при быстрой … "
                         "медленной сушке (90 %); пунктир — выход хозяйства."))
        lg.setWordWrap(True)
        gv.addWidget(lg)
        self.gantt = GanttChart("Нет расчёта")
        gv.addWidget(self.gantt, 1)
        self.views.addTab(gw, "На шкале времени")
        size_by_current(self.views)
        box = QVBoxLayout()
        box.setContentsMargins(gl, 0, gr, 0)
        box.addWidget(self.views)
        rv.addLayout(box, 1)

        lay.addWidget(scroll_wrap(left, 430), 0)
        lay.addWidget(right, 1)

        for wd in (self.stops, self.w0, self.wk, self.already, self.bin_t, self.q, self.lim, self.t_amb, self.rh):
            wd.valueChanged.connect(self.recalc)
        self.bins.valueChanged.connect(self.recalc)
        self.npass.valueChanged.connect(self.recalc)
        self.method.currentIndexChanged.connect(self.recalc)
        self.cool.stateChanged.connect(self.recalc)
        self.start.dateTimeChanged.connect(self._start_edited)
        self.end.dateTimeChanged.connect(self._end_edited)

    # ------------------------------------------------------------------
    def _set_dt(self, w: QDateTimeEdit, t: datetime) -> None:
        self._prog = True
        try:
            w.setDateTime(QDateTime(t.year, t.month, t.day, t.hour, t.minute, 0))
        finally:
            self._prog = False

    def _start_edited(self, *_a) -> None:
        if not self._prog:
            self._start_auto = False
            self.lbl_start_src.setText("Начало задано вручную.")
        if self._end_auto:
            self._set_dt(self.end, next_weekday_time(self.start.dateTime().toPython(), *self._ret))
        self.recalc()

    def _end_edited(self, *_a) -> None:
        if not self._prog:
            self._end_auto = False
            t = self.end.dateTime().toPython()
            self._ret = (t.weekday(), t.hour, t.minute)       # в следующий раз — тот же день недели и час
        self.recalc()

    def _sources(self) -> None:
        """Откуда можно взять начало: конец текущей партии по журналу и конец очереди партий."""
        lt = self.win.tab_live
        try:
            lt.refresh()
        except Exception:  # noqa: BLE001 — без журнала начало задаётся вручную
            pass
        fc, st = getattr(lt, "fc", None), getattr(lt, "state", None)
        self._j_end = fc.batch_end if fc is not None and fc.batch_end else None
        if self._j_end:
            name = (st.batch if st is not None and st.batch else "без названия")
            self.b_journal.setText(f"По журналу: партия «{name}» закончится ≈ {when(self._j_end)} — взять")
        self.b_journal.setVisible(self._j_end is not None)
        try:
            self._q_end = self.win.tab_batches.queue_end()
        except Exception:  # noqa: BLE001
            self._q_end = None
        if self._q_end:
            self.b_queue.setText(f"После очереди партий: {when(self._q_end)} — взять")
        self.b_queue.setVisible(self._q_end is not None)

    def _take_start(self, src: str, auto: bool = False) -> None:
        now = datetime.now()
        if src == "journal" and self._j_end:
            t, txt = self._j_end, "Начало — когда закончится текущая партия (по журналу, «Текущий проход»)."
        elif src == "queue" and self._q_end:
            t, txt = self._q_end, "Начало — когда сушилка освободится после очереди партий."
        else:
            t = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
            txt = ("Начало — ближайший час: конец партии по журналу уже прошёл, очередь пуста." if self._j_end else
                   "Начало — ближайший час: в журнале нет текущей партии, очередь пуста.")
        self._set_dt(self.start, t)
        self._start_auto = auto
        self.lbl_start_src.setText(txt)
        if self._end_auto:
            self._set_dt(self.end, next_weekday_time(t, *self._ret))
        self.recalc()

    def on_show(self) -> None:
        self._sources()
        if self._start_auto:
            # очередь — то, что запланировано после текущей партии; без очереди — конец текущей по журналу
            now = datetime.now()
            if self._q_end:
                self._take_start("queue", auto=True)
            elif self._j_end and self._j_end > now - timedelta(hours=1):
                self._take_start("journal", auto=True)
            else:
                self._take_start("now", auto=True)
        else:
            self.recalc()

    def _from_weather(self) -> None:
        ws = self.win.weather_series()
        if not len(ws):
            self.b_weather.setText("Таблица погоды пуста — введите T и RH на выходные вручную")
            return
        a, b = self.start.dateTime().toPython(), self.end.dateTime().toPython()
        t, rh, n = ws.mean_between(a, b)
        what = f"среднее на окно ({n} замеров)"
        if t is None and rh is None:
            last = ws.last_dt() or datetime.now()
            t, rh, n = ws.mean_between(last - timedelta(days=3), last)
            what = f"прогноза на окно нет — среднее за 3 сут до {last:%d.%m %H:%M} ({n} замеров)"
        if t is not None:
            self.t_amb.setValue(t)
        if rh is not None:
            self.rh.setValue(rh)
        self.b_weather.setText(f"T и RH из таблицы погоды: {what}")

    def _load(self) -> None:
        """Поля раздела из базы — после смены культуры или восстановления копии (у каждой культуры свои)."""
        d = self.win.storage.get_json("ui_weekend", {}) or {}
        self._prog = True
        try:
            for key, w in (("w0", self.w0), ("wk", self.wk), ("stops", self.stops), ("bin_t", self.bin_t),
                           ("q", self.q), ("lim", self.lim)):
                if key in d:
                    w.setValue(float(d[key]))
            if "bins" in d:
                self.bins.setValue(int(d["bins"]))
            self.method.setCurrentIndex(1 if d.get("method") == "q" else 0)
            self.cool.setChecked(bool(d.get("cool", True)))
            self.npass.setValue(int(d.get("passes", 0) or 0))
        finally:
            self._prog = False

    def _save(self) -> None:
        self.win.storage.set_json("ui_weekend", {
            "w0": self.w0.value(), "wk": self.wk.value(), "stops": self.stops.value(), "bins": self.bins.value(),
            "bin_t": self.bin_t.value(), "method": self.method.currentData(), "q": self.q.value(),
            "lim": self.lim.value(), "cool": self.cool.isChecked(), "passes": self.npass.value(),
            "ret_dow": self._ret[0], "ret_h": self._ret[1],
            "ret_m": self._ret[2]})

    def _add_to_queue(self) -> None:
        r, inp = self.result, self.inp
        if r is None or not r.ok or inp is None:
            return
        name = f"Выходные {inp.start:%d.%m}"
        self.win.tab_batches.add_to_queue(name, round(r.mass / 10.0) * 10.0, inp.w0, inp.wk, inp.start,
                                          passes=inp.passes or None)
        self.win.statusBar().showMessage(f"Партия «{name}», {tonnes(r.mass)} т, добавлена в очередь — раздел "
                                         f"«Партии и очередь» → «Очередь и смешивание».", 15000)

    # ------------------------------------------------------------------
    def recalc(self) -> None:
        if not getattr(self.win, "_ready", False):
            return
        p = self.win.params
        th = theme_for(self)
        self.q.setEnabled(self.method.currentData() == "q")
        self.lim.setEnabled(self.method.currentData() == "q" and not self.npass.value())
        self.bin_t.setEnabled(self.bins.value() > 0)
        c0 = self.win.conditions()
        c = mdl.Conditions(c0.t_proc, self.t_amb.value(), self.rh.value())
        soft, hard = self.win.limits()
        t_list = parse_decimals(self.win.in_tlist.text(), 0, 200) or None
        if self.method.currentData() == "q":
            self.lbl_cond.setText("Время прохода = масса на выходе ÷ Q; съём делится поровну на проходы не больше "
                                  "лимита — как в «Быстрая оценка».")
        else:
            tl = (" (по проходам: " + "; ".join(f"{x:g}" for x in t_list) + ")") if t_list else ""
            self.lbl_cond.setText(f"T блоков {c0.t_proc:g} °C{tl}, съём за проход ≤ {soft:g} п.п. (допустимо "
                                  f"{hard:g}) — из «План сушки партии».")
        inp = WeekendInput(
            start=self.start.dateTime().toPython(), end=self.end.dateTime().toPython(), w0=self.w0.value(),
            wk=self.wk.value(), stops_h=self.stops.value(), already=self.already.value(),
            capacity=self.bins.value() * self.bin_t.value(), method=self.method.currentData(), q=self.q.value(),
            lim=self.lim.value(), soft=soft, hard=hard, cool=self.cool.isChecked(), t_list=t_list,
            passes=self.npass.value())
        r = weekend_mass(p, c, inp)
        self.result, self.inp = r, inp
        self._save()
        self.tbl_opt.setRowCount(0)
        self.tbl_pass.setRowCount(0)
        self.b_add.setEnabled(r.ok)
        if not r.ok:
            self.card.set_tint("rust")
            self.lbl_head.setText("Посчитать нельзя")
            self.lbl_sub.setText(r.message)
            self.lbl_warn.setVisible(False)
            for t in self.tiles:
                t.set("—")
            self.gantt.set_entries([])
            return
        warn, level = [], 0
        # ---- ответ ----
        self.lbl_head.setText(f"Намолотить ≈ {tonnes(r.mass)} т")
        sub = [f"Сырого зерна {inp.w0:g} %, чтобы высушить его до {inp.wk:g} % к выходу хозяйства."]
        if inp.already > 0:
            rest = r.mass - inp.already
            if rest >= 5:
                sub.append(f"Уже намолочено {inp.already:.0f} т — домолотить ещё <b>{tonnes(rest)} т</b>.")
            else:
                over_h = -rest * r.h_per_t
                sub.append(f"Уже намолочено {inp.already:.0f} т — это не меньше, чем сушилка успеет к выходу; "
                           f"лишние {-rest:.0f} т досохнут после выхода (≈ {over_h:.1f} ч).")
        stops = f", без {inp.stops_h:g} ч остановок" if inp.stops_h > 0 else ""
        sub.append(f"Сушилка работает {fmt_hours(r.hours)}: с <b>{when(inp.start)}</b> до выхода хозяйства "
                   f"<b>{when(inp.end)}</b>{stops}.")
        if r.mass_lo is not None and r.mass_hi is not None and len(r.options) > 1:
            sub.append(f"Допуск 90 %: от {tonnes(r.mass_lo, 'down')} т (точно успеть к выходу) до "
                       f"{tonnes(r.mass_hi, 'up')} т (без простоя сушилки) — «Варианты массы».")
        self.lbl_sub.setText("<br>".join(sub))
        if r.capacity_short > 0:
            level = max(level, 1)
            need = math.ceil(r.mass / self.bin_t.value()) if self.bin_t.value() > 0 else 0
            warn.append(f"В ёмкости {inp.capacity:.0f} т не поместится: не хватает {r.capacity_short:.0f} т "
                        f"(нужно {need} × {self.bin_t.value():.0f} т) — или намолотить по ёмкостям, и сушилка "
                        f"встанет раньше выхода (вариант «Сколько вместят ёмкости»).")
        early = (r.harvest_not_before is not None and r.storage_level == 0
                 and r.harvest_not_before > inp.start - timedelta(hours=24))
        if early:
            level = max(level, 1)
            warn.append(f"Сырое зерно, убранное раньше {when(r.harvest_not_before)}, пролежит до 1-го прохода дольше "
                        f"допустимого ≈ {_days(r.storage_days)} сут при {c.t_amb:.0f} °C и {inp.w0:g} %.")
        if r.storage_days is not None and r.storage_level >= 1:
            level = max(level, r.storage_level)
            warn.append(f"Сырое зерно {inp.w0:g} % ждёт 1-го прохода до {when(r.pass1_end)} ({fmt_hours(r.wet_wait_h)}) "
                        f"— это {'дольше' if r.storage_level == 2 else 'больше половины'} допустимого срока ≈ "
                        f"{_days(r.storage_days)} сут при {c.t_amb:.0f} °C. Меньше масса, раньше начало или "
                        f"вентилировать ёмкости.")
        for n in r.notes:
            level = max(level, 1)
            warn.append(n)
        self.lbl_warn.setText("<br>".join("⚠ " + w for w in warn))
        self.lbl_warn.setVisible(bool(warn))
        self.card.set_tint(self.TINT[level])
        tip = mdl.sigma_text(r.sigma_info) + "." if r.sigma_info else "По производительности: без допуска модели."
        self.card.setToolTip(tip)
        # ---- плитки ----
        self.t_win.set(fmt_hours(r.hours).split(" (")[0],
                       f"{when(inp.start)} → {when(inp.end)}" + (f"; остановки {inp.stops_h:g} ч" if inp.stops_h else ""))
        pl = r.plan
        dws = " / ".join(f"{x.dw:.1f}" for x in pl.passes)
        if inp.method == "q":
            self.t_pass.set(str(len(pl.passes)), f"съём {dws} п.п.; Q {inp.q:g} т/ч")
        else:
            self.t_pass.set(str(len(pl.passes)), f"съём {dws} п.п.; D {' / '.join(f'{x.d:.0f}' for x in pl.passes)} с")
        self.t_rate.set(f"{r.mass / r.hours:.1f} т/ч",
                        f"{100 * r.h_per_t:.1f} ч на 100 т сырого + {r.fixed_h:.1f} ч на смену проходов",
                        "Тонн сырого зерна за час окна: все проходы, охлаждение и накладные.")
        if r.storage_days is not None:
            mark = "" if r.storage_level == 0 and not early else " ⚠"
            self.t_wet.set(fmt_hours(r.wet_wait_h).split(" (")[0] + mark,
                           f"до {when(r.pass1_end)}; допустимо ≈ {_days(r.storage_days)} сут ({c.t_amb:.0f} °C, "
                           f"{inp.w0:g} %)",
                           f"Допустимый срок хранения сырого зерна (потеря 0,5 % сухого вещества): {STORAGE_SOURCE}. "
                           f"Температура зерна принята ≈ наружного воздуха. Зерно, убранное раньше "
                           f"{when(r.harvest_not_before)}, до конца 1-го прохода пролежит дольше срока. "
                           + (r.storage_flag or ""))
        else:
            self.t_wet.set(fmt_hours(r.wet_wait_h).split(" (")[0], f"до {when(r.pass1_end)}")
        # ---- варианты ----
        bold = QFont()
        bold.setBold(True)
        how = {"safe": "down", "full": "up", "bins": "down"}
        self.tbl_opt.setRowCount(len(r.options))
        for i, o in enumerate(r.options):
            rest = o.mass - inp.already
            rng = "—"
            if o.end_fast and o.end_slow:
                a, b = min(o.end_fast, o.end_slow), max(o.end_fast, o.end_slow)
                rng = f"{when(a)} … " + (f"{b:%H:%M}" if b.date() == a.date() else when(b))
            cells = [o.title + (" ★" if o.key == "calc" else ""), tonnes(o.mass, how.get(o.key, "round")),
                     tonnes(max(rest, 0.0), how.get(o.key, "round")) if inp.already > 0 else "—", when(o.end), rng]
            lvl = 1 if o.key == "bins" else 0
            bg, fg = th.level(lvl)
            for j, txt in enumerate(cells):
                it = item(txt, align_right=j in (1, 2))
                if o.key == "calc":
                    it.setFont(bold)
                if o.key == "bins":
                    it.setBackground(QBrush(bg))
                    it.setForeground(QBrush(fg))
                it.setToolTip(tip)
                self.tbl_opt.setItem(i, j, it)
        self.tbl_opt.resizeColumnsToContents()
        self.tbl_opt.resizeRowsToContents()
        hh = self.tbl_opt.horizontalHeader().sizeHint().height() + self.tbl_opt.verticalHeader().length()
        self.tbl_opt.setFixedHeight(hh + 2 * self.tbl_opt.frameWidth() + 4)
        self.lbl_opt.setText("<br>".join(f"<b>{o.title}</b> — {o.what}." for o in r.options)
                             + f"<br><span style='color:{th.muted}'>Окончание — при обычной (расчётной) скорости "
                             f"сушки; рядом — при быстрой … медленной в пределах допуска 90 %.</span>")
        # ---- проходы по времени ----
        rows = []
        t = inp.start
        for ps in pl.passes:
            a, b = t, t + timedelta(hours=ps.time_h)
            rows.append([str(ps.k), f"{ps.w_in:.1f} → {ps.w_out:.1f}", f"{ps.d:.0f}", f"{ps.q:.1f}",
                         when(a), when(b), f"{ps.time_h:.1f}", f"{ps.m_out:.0f}"])
            t = b + timedelta(hours=pl.overhead_h)
        if pl.cool_h > 0:
            a = inp.start + timedelta(hours=pl.total_time_h - pl.cool_h)
            rows.append(["охлаждение", "—", "100", f"{mdl.q_full(p):.1f}", when(a),
                         when(a + timedelta(hours=pl.cool_h)), f"{pl.cool_h:.1f}", f"{pl.m_final:.0f}"])
        if inp.stops_h > 0:
            rows.append(["остановки", "—", "—", "—", "—", when(inp.end), f"{inp.stops_h:.1f}", "—"])
        self.tbl_pass.setRowCount(len(rows))
        for i, cells in enumerate(rows):
            for j, txt in enumerate(cells):
                self.tbl_pass.setItem(i, j, item(txt, align_right=j in (2, 3, 6, 7)))
        self.tbl_pass.resizeColumnsToContents()
        # ---- шкала времени ----
        short = {"safe": "Точно успеть", "calc": "Расчётная", "full": "Без простоя", "bins": "По ёмкостям"}
        entries = []
        for o in r.options:
            e = QueueEntry(name=f"{short[o.key]}, {tonnes(o.mass, how.get(o.key, 'round'))} т", mass=o.mass,
                           w0=inp.w0, wk=inp.wk, ready=None, start=inp.start, end=o.end, plan=o.plan)
            if o.end_fast and o.end_slow:
                e.end_range = (min(o.end_fast, o.end_slow), max(o.end_fast, o.end_slow))
            entries.append(e)
        self.gantt.set_entries(entries, [(inp.end, "выход хозяйства")])
