"""Раздел «Текущий проход»: что делать с разгрузкой D прямо сейчас, по свежим пробам журнала.

Сверху — стеклянная карточка с одним действием («Поставьте D = 42 с», «D не менять», «Подождите до 09:30») и шкалой
D (точка — сейчас, ручка — что поставить), под ней пять плиток с главными цифрами (выход со спарклайном проб, цель,
приёмный силос, конец прохода с полосой выгрузки, конец партии), ниже вкладками — график проб, «что будет при
другом D» и полный расчёт; что программа взяла из журнала и задание на проход — на вкладке «Данные прохода и
задание». Логика совета — live.live_advice (правила работы, 7.3).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QWidget)

from . import model as mdl
from .live import LiveState, live_advice, live_forecast, live_state, outlet_soon
from .plots import SimplePlot
from .theme import theme_for
from .ui_common import (Collapsible, Tile, dspin, fmt_hours, fnum, item, mute, set_role, size_by_current, table)
from .widgets import DDial, Glass, Legend, ProgressLine, Sparkline

if TYPE_CHECKING:
    from .gui import MainWindow


def _passes(n: int) -> str:
    """1 проход, 2 прохода, 5 проходов."""
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return f"{n} проход"
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return f"{n} прохода"
    return f"{n} проходов"


class LiveTab(QWidget):
    TINT = {"change": "gold", "keep": "green", "wait": "steel", "info": "steel"}

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.state: Optional[LiveState] = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.fc = None
        self.advice = None
        # ---- вкладка «Данные прохода и задание»: что известно по журналу, задание на проход, условия ----
        setup = QWidget()
        sg = QGridLayout(setup)
        sg.setContentsMargins(0, 2, 6, 0)
        sg.setHorizontalSpacing(12)
        sg.setVerticalSpacing(6)
        cols = [QVBoxLayout() for _ in range(3)]
        for c, cl in enumerate(cols):
            cl.setSpacing(6)
            sg.addLayout(cl, 0, c)
            sg.setColumnStretch(c, 1)
        gb = QGroupBox("Проход по журналу")
        fv = QVBoxLayout(gb)
        self.facts = QGridLayout()
        self.facts.setHorizontalSpacing(10)
        self.facts.setVerticalSpacing(3)
        self.facts.setColumnStretch(1, 1)
        self.fact_lbl = {}
        for i, (key, cap) in enumerate((("batch", "Партия"), ("src", "Подача из силосов"),
                                        ("start", "Начало прохода"), ("last", "Последняя запись"),
                                        ("w_in", "Вход сейчас"), ("w_out", "Выход, влагомер"), ("d", "Разгрузка D"),
                                        ("t", "T блоков / зерна"), ("unl", "Выгружено"), ("silo", "В силосе"),
                                        ("corr", "Поправка модели"))):
            k = set_role(QLabel(cap), "factKey")
            v = set_role(QLabel("—"), "factVal")
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.facts.addWidget(k, i, 0)
            self.facts.addWidget(v, i, 1)
            self.fact_lbl[key] = (k, v)
        fv.addLayout(self.facts)
        self.lbl_state = QLabel("")            # сообщение, если прохода в журнале нет, и «идёт рециркуляция»
        self.lbl_state.setWordWrap(True)
        self.lbl_state.setTextFormat(Qt.RichText)
        self.lbl_state.setVisible(False)
        fv.addWidget(self.lbl_state)
        cols[0].addWidget(gb)

        gb = QGroupBox("Задание на проход")
        f = QFormLayout(gb)
        self.m_pass = dspin(1, 20000, 500, 0, 10, " т")
        self.w_target = dspin(8, 40, 24, 1, 0.5, " %")
        self.lbl_target_read = mute(QLabel())
        self.lbl_target_read.setWordWrap(True)
        self.wk_final = dspin(8, 30, 14, 1, 0.5, " %")
        self.t_amb = dspin(-40, 45, 10, 1, 1, " °C")
        self.rh = dspin(5, 100, 70, 0, 5, " %")
        self.d_try = dspin(0, 100, 0, 0, 1, " с")
        self.d_try.setSpecialValueText("подобрать")
        self.cb_corr = QCheckBox("Поправка по последним замерам выхода")
        self.cb_corr.setChecked(True)
        self.cb_now = QCheckBox("Считать до текущего времени, а не до последней пробы")
        self.cb_silo = QCheckBox("Выравнивать среднюю по силосу")
        self.cb_silo.setChecked(True)
        self.cb_silo.setToolTip(
            "Всё, что выгружено за проход, ложится в один силос — важна его средняя влажность. Если часть уже ушла "
            "суше или сырее цели, программа подберёт D так, чтобы оставшееся зерно выровняло среднее, но не дальше "
            "чем на заданное число п.п. от цели прохода.")
        self.silo_lim = dspin(0.2, 3.0, 1.0, 1, 0.1, " п.п.")
        b_mass = set_role(QPushButton("Взять массу партии из «План сушки партии»"), "link")
        b_mass.setCursor(Qt.PointingHandCursor)
        b_mass.clicked.connect(lambda: self.m_pass.setValue(self.win.in_mass.value()))
        b_w = set_role(QPushButton("Взять из таблицы погоды"), "link")
        b_w.setCursor(Qt.PointingHandCursor)
        b_w.setToolTip("Наружный воздух из таблицы погоды на время последней пробы.")
        b_w.clicked.connect(self._weather)
        self.m_pass.setToolTip("Масса зерна на входе этого прохода, т (на первом проходе — масса партии).")
        self.w_target.setToolTip("Желаемая влажность на выходе этого прохода — средняя по приёмному силосу, после "
                                 "охлаждения (как в лаборатории).")
        f.addRow("Масса на входе", self.m_pass)
        f.addRow(b_mass)
        f.addRow("Цель прохода", self.w_target)
        f.addRow(self.lbl_target_read)
        f.addRow("Цель партии Wк", self.wk_final)
        f.addRow(self.cb_silo)
        f.addRow("…не дальше от цели, ±", self.silo_lim)
        cols[1].addWidget(gb)
        gb = QGroupBox("Наружный воздух и проверка D")
        f = QFormLayout(gb)
        air = QHBoxLayout()
        air.setSpacing(6)
        air.addWidget(self.t_amb, 1)
        air.addWidget(self.rh, 1)
        f.addRow("T и RH", air)
        f.addRow(b_w)
        f.addRow("Что будет при D", self.d_try)
        cols[2].addWidget(gb)
        self.gb_calc = QGroupBox("Расчёт")
        f = QFormLayout(self.gb_calc)
        f.addRow(self.cb_corr)
        f.addRow(self.cb_now)
        cols[2].addWidget(self.gb_calc)
        b = QPushButton("Обновить по журналу")
        b.setToolTip("Раздел обновляется сам, когда вы его открываете и когда в журнал добавлена проба.")
        b.clicked.connect(self.refresh)
        cols[0].addWidget(b, 0, Qt.AlignLeft)
        self.hint = Collapsible(
            "Как пользоваться",
            "1. Внесите пробу в «Журнал проб».<br>2. Посмотрите сюда: крупная строка сверху — что сделать с D.<br>"
            "3. «Поставьте D = …» — поставьте и запишите изменение («Изменить режим…» в журнале). «D не менять» "
            "или «Подождите» — ничего не трогайте.<br><br>Шкала слева от совета: точка — D сейчас, ручка — что "
            "поставить, золотая дуга — насколько крутить.<br><br>Почему не чаще: зерно, уже находящееся в шахте, "
            "выходит со старой скоростью — смена D видна на выходе только через время «Заметно через …». Разница "
            "меньше 3 с — это ≈ 0,5 п.п. на выходе, на уровне точности влагомера. Подробно — «Справка» → «Правила "
            "работы», раздел 7.")
        self.lbl_hint = mute(self.hint.content)
        cols[1].addWidget(self.hint)
        for cl in cols:
            cl.addStretch(1)

        # ---- страница: совет, главные цифры, вкладки (график, данные и задание, другое D, расчёт) ----
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(4)
        self.card = Glass(radius=18, blur=10, dy=3)
        self.card.setObjectName("adviceCard")
        gl, gt, gr, gbm = self.card.shadow_margins()
        ch = QHBoxLayout(self.card)
        ch.setContentsMargins(gl + 10, gt + 8, gr + 16, gbm + 8)
        ch.setSpacing(14)
        self.dial = DDial()
        ch.addWidget(self.dial, 0, Qt.AlignVCenter)
        cv = QVBoxLayout()
        cv.setSpacing(4)
        head = QHBoxLayout()
        self.lbl_head = set_role(QLabel("—"), "adviceHead")
        self.lbl_head.setWordWrap(True)
        head.addWidget(self.lbl_head, 1, Qt.AlignVCenter)
        self.btn_go = set_role(QPushButton(), "primary")
        self.btn_go.setVisible(False)
        self.btn_go.clicked.connect(self._go)
        head.addWidget(self.btn_go, 0, Qt.AlignVCenter)
        cv.addLayout(head)
        self.lbl_sub = set_role(QLabel(), "adviceSub")
        self.lbl_sub.setWordWrap(True)
        self.lbl_sub.setTextFormat(Qt.RichText)
        cv.addWidget(self.lbl_sub)
        self.lbl_warn = set_role(QLabel(), "warn")
        self.lbl_warn.setWordWrap(True)
        self.lbl_warn.setVisible(False)
        cv.addWidget(self.lbl_warn)
        cv.addStretch(1)
        ch.addLayout(cv, 1)
        rv.addWidget(self.card)
        # главные цифры — выровнены по краю стекла
        tiles = QHBoxLayout()
        tiles.setContentsMargins(gl, 0, gr, 0)
        tiles.setSpacing(8)
        self.t_out = Tile("Выход, влагомер")
        self.spark = Sparkline()
        self.spark.setToolTip("Пробы выхода этого прохода, цель на влагомере (пунктир) и прогноз по зерну, которое уже "
                              "в шахте (бледный пунктир).")
        self.t_out.set_below(self.spark)
        self.t_goal = Tile("Цель на влагомере")
        self.t_silo = Tile("Приёмный силос")
        self.t_pass = Tile("Конец прохода")
        self.pass_bar = ProgressLine()
        self.pass_bar.setToolTip("Сколько массы прохода уже выгружено.")
        self.t_pass.set_below(self.pass_bar)
        self.t_batch = Tile("Конец партии")
        self.tiles = (self.t_out, self.t_goal, self.t_silo, self.t_pass, self.t_batch)
        for t in self.tiles:
            tiles.addWidget(t, 1)
        rv.addLayout(tiles)
        rv.addSpacing(6)
        # график, варианты D и полный расчёт — вкладками-«пилюлями»
        self.views = QTabWidget()
        self.views.setDocumentMode(True)
        pw = QWidget()
        pv = QVBoxLayout(pw)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(4)
        self.legend = Legend()
        pv.addWidget(self.legend)
        self.plot = SimplePlot()
        self.plot.set_labels("часы от начала прохода", "влажность, %")
        pv.addWidget(self.plot, 1)
        self.views.addTab(pw, "График проб")
        sc_setup = QScrollArea()
        sc_setup.setWidget(setup)
        sc_setup.setWidgetResizable(True)
        sc_setup.setFrameShape(QFrame.NoFrame)
        self.setup = sc_setup
        self.views.addTab(sc_setup, "Данные прохода и задание")
        tw = QWidget()
        tv = QVBoxLayout(tw)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(4)
        tv.addWidget(mute(QLabel("Вход и температура — как сейчас; выделено D, которое советуем (или ваше).")))
        self.tbl = table(["D, с", "Q, т/ч", "W вых, %", "Влагомер покажет, %", "Съём, п.п.", "Видно через, ч",
                          "Конец прохода", "Среднее по силосу, %", "T зерна ≈, °C"])
        tv.addWidget(self.tbl, 1)
        self.views.addTab(tw, "Если поставить другое D")
        self.summary = set_role(QLabel(), "summary")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.RichText)
        self.summary.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        sc = QScrollArea()                 # полный текст расчёта длинный — в прокрутке, чтобы не обрезался
        sc.setWidget(self.summary)
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        self.details = sc
        self.views.addTab(sc, "Расчёт подробно")
        size_by_current(self.views)
        tabs_box = QVBoxLayout()
        tabs_box.setContentsMargins(gl, 0, gr, 0)
        tabs_box.addWidget(self.views)
        rv.addLayout(tabs_box, 1)
        lay.addWidget(right, 1)

        for w in (self.m_pass, self.w_target, self.t_amb, self.rh, self.d_try, self.silo_lim):
            w.valueChanged.connect(self.recalc)
        self.wk_final.valueChanged.connect(self.refresh)       # от цели партии зависит, досушка ли рециркуляция
        self.cb_corr.stateChanged.connect(self.recalc)
        self.cb_silo.stateChanged.connect(self.recalc)
        self.cb_now.stateChanged.connect(self.refresh)
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        d = self.win.storage.get_json("ui_live", {}) or {}
        for k, w in (("m_pass", self.m_pass), ("w_target", self.w_target), ("wk", self.wk_final),
                     ("t_amb", self.t_amb), ("rh", self.rh)):
            if k in d:
                w.setValue(float(d[k]))
        self.cb_corr.setChecked(bool(d.get("corr", True)))
        self.cb_silo.setChecked(bool(d.get("silo", True)))
        if "silo_lim" in d:
            self.silo_lim.setValue(float(d["silo_lim"]))

    def _save(self) -> None:
        self.win.storage.set_json("ui_live", {
            "m_pass": self.m_pass.value(), "w_target": self.w_target.value(), "wk": self.wk_final.value(),
            "t_amb": self.t_amb.value(), "rh": self.rh.value(), "corr": self.cb_corr.isChecked(),
            "silo": self.cb_silo.isChecked(), "silo_lim": self.silo_lim.value()})

    def _weather(self) -> None:
        ws = self.win.weather_series()
        if not len(ws):
            self.summary.setText("Таблица погоды пуста (раздел «Погода и влагомер»).")
            self.views.setCurrentWidget(self.details)
            return
        end = (self.state.last if self.state and self.state.last else datetime.now())
        t, rh = ws.at(end)
        if t is None or rh is None:
            mt, mrh, n = ws.mean_between(end - timedelta(hours=24), end)
            t = t if t is not None else mt
            rh = rh if rh is not None else mrh
        if t is not None:
            self.t_amb.setValue(t)
        if rh is not None:
            self.rh.setValue(rh)

    def on_show(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        p = self.win.params
        now = datetime.now() if self.cb_now.isChecked() else None
        # рециркуляция, когда выход уже у цели партии (Wк + 1 п.п.), — досушка и конец партии
        self.state = live_state(self.win.journal_rows(), p, now=now, weather=self.win.weather_series(),
                                final_w=self.wk_final.value() + 1.0)
        st = self.state
        if st.t_amb is not None:
            self.t_amb.blockSignals(True)
            self.t_amb.setValue(st.t_amb)
            self.t_amb.blockSignals(False)
        if st.rh is not None:
            self.rh.blockSignals(True)
            self.rh.setValue(st.rh)
            self.rh.blockSignals(False)
        self._fill_facts(p, st)
        self.recalc()

    # ------------------------------------------------------------------
    def set_operator(self, op: bool) -> None:
        """Режим сушильщика: без инженерных настроек расчёта."""
        self.gb_calc.setVisible(not op)

    def _go(self) -> None:
        name = (self.advice or {}).get("go")
        if name:
            self.win.tabs.setCurrentIndex(self.win.tabs.indexOfText(name))

    def _fill_facts(self, p, st) -> None:
        """Карточка «Проход по журналу»: что программа взяла из журнала — сверить перед тем, как крутить D."""
        ok = st.start is not None
        tg = f"{st.t_grain:.0f}" if st.t_grain is not None else "—"
        tp = f"{st.t_proc:.0f}" if st.t_proc is not None else "—"
        q = mdl.q_at(p, st.d) if st.d else None
        auto = " (по рециркуляциям)" if st.pass_auto else ""
        vals = {
            "batch": (f"{st.batch or 'без названия'}, проход {st.pass_no:g}{auto}" if st.pass_no is not None
                      else f"{st.batch or '—'}"),
            "src": " → ".join(f"{x} с." for x in st.sources) if st.sources else "",
            "start": f"{st.start:%d.%m %H:%M}" if ok else "—",
            "last": (f"{st.last:%d.%m %H:%M}, через {fmt_hours(st.hours)}" if ok and st.last else "—"),
            "w_in": f"{fnum(st.w_in)} %", "w_out": f"{fnum(st.w_out_read)} %",
            "d": (f"{fnum(st.d, 0)} с, Q = {fnum(q)} т/ч" if st.d else "—"),
            "t": f"{tp} / {tg} °C", "unl": f"{fnum(st.unloaded, 0)} т",
            "silo": (f"{st.silo_w:.1f} % (влагомер {st.silo_w_read:.1f} %)" if st.silo_w is not None else "—"),
            "corr": f"{st.corr:+.2f} п.п. по {st.corr_n} замерам" if ok else "—",
        }
        for key, (k, v) in self.fact_lbl.items():
            v.setText(vals[key] if ok else "—")
            show = (ok or key in ("batch",)) and (key != "src" or bool(vals["src"]))
            k.setVisible(show)
            v.setVisible(show)
        msg = ""
        if not ok:
            msg = st.message or "—"
        elif st.recirc and st.batch_end:
            msg = "Рециркуляция у цели партии — досушка остатка в шахте и конец партии (раздел «Досушка»)."
        elif st.recirc:
            msg = ("Подача прохода закончилась — рециркуляция. Следующий проход начнётся с подачи из силоса "
                   "(«сушим с N с.»).")
        self.lbl_state.setText(msg)
        self.lbl_state.setVisible(bool(msg))

    def _show_advice(self, adv: dict) -> None:
        """Стеклянная карточка с одним действием: оттенок по смыслу (золото — поставить D, зелень — не менять,
        сталь — подождать или сведения), шкала D слева, предупреждения — отдельной строкой."""
        self.advice = adv
        kind = adv["kind"]
        self.card.set_tint(self.TINT.get(kind))
        self.lbl_head.setText(adv["head"])
        self.lbl_sub.setText("<br>".join(adv["sub"]))
        self.lbl_warn.setText("<br>".join(f"⚠ {w}" for w in adv["warn"]))
        self.lbl_warn.setVisible(bool(adv["warn"]))
        go = adv.get("go")
        self.btn_go.setVisible(bool(go))
        if go:
            self.btn_go.setText(f"Открыть «{go}»")
        d_now, d_rec = adv.get("d_now"), adv.get("d_rec")
        if kind == "change":
            self.dial.set_state("change", d_now, d_rec, f"было {d_now:.0f}" if d_now is not None else "")
        elif kind == "keep":
            self.dial.set_state("keep", d_now, d_now, "не менять")
        elif kind == "wait":
            until = adv.get("until")
            self.dial.set_state("wait", d_now, d_now, f"до {until:%H:%M}" if until else "подождать")
        elif kind == "info" and adv.get("d_try"):
            self.dial.set_state("info", d_now, adv["d_try"], "проверка")
        elif kind == "info":
            self.dial.set_state("info", d_now, d_now, "рециркуляция" if adv.get("go") else "до конца прохода")
        else:
            self.dial.set_state("nodata", d_now, None, "нет данных")

    def _clear_tiles(self) -> None:
        for t in self.tiles:
            t.set("—")
        self.spark.set_data([], [])
        self.pass_bar.set_value(None)
        self.legend.set_items([])

    def recalc(self) -> None:
        p = self.win.params
        th = theme_for(self)
        tg_now = self.state.t_grain if self.state is not None else None
        target_read = mdl.read_from_true(p, self.w_target.value(), tg_now)
        self.lbl_target_read.setText(f"После охлаждения, как в лаборатории. На выходном влагомере это "
                                     f"≈ {target_read:.1f} % (поправка {target_read - self.w_target.value():+.1f} п.п.)")
        self.tbl.setRowCount(0)
        self.plot.clear()
        self._save()
        st = self.state
        if st is None:
            return
        if not st.ok:
            self.fc = None
            self.summary.setText(st.message)
            self._show_advice(live_advice(p, st, None))
            self._clear_tiles()
            return

        soft, hard = self.win.limits()
        c = mdl.Conditions(st.t_proc, self.t_amb.value(), self.rh.value())
        now = datetime.now() if self.cb_now.isChecked() else None
        d_try = self.d_try.value() or None
        fc = live_forecast(p, c, st, self.m_pass.value(), self.w_target.value(), self.wk_final.value(), soft, hard,
                           use_corr=self.cb_corr.isChecked(), now=now, d_override=d_try,
                           silo=self.cb_silo.isChecked(), silo_lim=self.silo_lim.value())
        self.fc = fc
        d_use = d_try or fc.d_rec
        lines = [f"Сейчас при D = {st.d:.0f}: на выходе ≈ <b>{fc.w_out_now:.1f} %</b> "
                 f"(влагомер покажет {fc.w_out_now_read:.1f} %)."]
        sl = fc.silo
        if sl:
            lines.append(f"В приёмный силос ушло {sl['m_done']:.0f} т со средней влажностью <b>{sl['w_done']:.1f} %</b> "
                         f"(влагомер {sl['w_done_read']:.1f} %)"
                         + (f"; ещё ≈ {sl['m_tr']:.0f} т уже в шахте — выйдут ≈ {sl['w_tr']:.1f} %." if sl["m_tr"] >= 1
                            else "."))
            if sl["late"]:
                lines.append(("До конца прохода осталось только зерно, которое уже в шахте" if sl["m_tr"] >= 1 else
                              "Масса прохода уже выгружена") +
                             f": среднее по силосу выйдет ≈ <b>{sl['avg']:.1f} %</b> — менять D ради этого прохода поздно.")
            elif abs(fc.w_goal - self.w_target.value()) >= 0.05:
                lim_txt = (f" (нужно бы {sl['need']:.1f} %, ограничено ±{sl['lim']:g} п.п.)" if sl["limited"] else "")
                lines.append(f"Чтобы среднее по силосу вышло {self.w_target.value():g} %, зерну, которое войдёт с этого "
                             f"момента ({sl['m_new']:.0f} т), нужно <b>{fc.w_goal:.1f} %</b>{lim_txt}.")
        goal_txt = f"{fc.w_goal:.1f}" if abs(fc.w_goal - self.w_target.value()) >= 0.05 else f"{self.w_target.value():g}"
        if d_try:
            lines.append(f"При вашем D = <b>{d_try:.0f}</b>: на выходе ≈ <b>{fc.w_out_rec:.1f} %</b> "
                         f"(подобранное для {goal_txt} %: D = {fc.d_rec:.0f}).")
        else:
            change = "оставить как есть" if abs(fc.d_rec - st.d) < 0.5 else f"было {st.d:.0f}"
            lines.append(f"Для {goal_txt} % на выходе: <b>D = {fc.d_rec:.0f}</b> ({change}); "
                         f"ожидается {fc.w_out_rec:.1f} % (влагомер {mdl.read_from_true(p, fc.w_out_rec, st.t_grain):.1f} %).")
        if sl and not sl["late"]:
            lines.append(f"Ожидаемая средняя влажность приёмного силоса после прохода: <b>{sl['avg']:.1f} %</b>.")
        if fc.d_note:
            lines.append(f"<span style='color:{th.error}'>{fc.d_note}.</span>")
        if fc.visible_at:
            lines.append(f"Изменение D будет видно на выходе через <b>{fc.tau_h:.1f} ч</b> — к {fc.visible_at:%d.%m %H:%M}.")
        soon = self._soon(p, st, fc)
        if soon:
            lines.append(f"На выходе в ближайшие часы (влагомер): {soon}. Это зерно уже в шахте — прогноз учитывает "
                         f"все D и T блоков, при которых оно шло, и новое D до выхода.")
        if fc.pass_end:
            rem = "проход фактически закончен (выгружено больше расчётной массы)" if fc.remaining <= 0.05 else \
                f"осталось выгрузить {fc.remaining:.0f} т"
            lines.append(f"Проход закончится ≈ <b>{fc.pass_end:%d.%m %H:%M}</b> ({rem}).")
        if fc.rest_plan is not None:
            ds = " / ".join(f"{x.d:.0f}" for x in fc.rest_plan.passes)
            rng = (f" (90 %: {fc.batch_end_range[0]:%d.%m %H:%M} … {fc.batch_end_range[1]:%d.%m %H:%M})"
                   if fc.batch_end_range else "")
            lines.append(f"Дальше до {self.wk_final.value():g} %: ещё {len(fc.rest_plan.passes)} прох. (D {ds}), "
                         f"партия закончится ≈ <b>{fc.batch_end:%d.%m %H:%M}</b>{rng}.")
            if fc.sigma_info and (fc.sigma_info["param"] > 0.005 or fc.sigma_info["struct"] > 0.005
                                  or fc.sigma_info["flags"]):
                lines.append(f"<span style='color:{th.muted}'>{mdl.sigma_text(fc.sigma_info)}.</span>")
        elif fc.batch_end:
            lines.append(f"Это последний проход: партия с охлаждением закончится ≈ <b>{fc.batch_end:%d.%m %H:%M}</b>.")
        for wtxt in fc.warnings:
            lines.append(f"<span style='color:{th.error}'>⚠ {wtxt}.</span>")
        self.summary.setText("<br>".join(lines))
        self._show_advice(live_advice(p, st, fc, d_try, now=datetime.now()))
        self._fill_tiles(p, st, fc, target_read)

        self._table(p, st, fc, c, d_use, now, th, target_read)

    def _soon(self, p, st, fc, hours=(1.0, 2.0)) -> str:
        return outlet_soon(p, st, fc, hours)

    def _fill_tiles(self, p, st, fc, target_read: float) -> None:
        goal_read = mdl.read_from_true(p, fc.w_goal, st.t_grain)
        if st.w_out_read is not None:
            dev = st.w_out_read - goal_read
            soon = "" if st.recirc else self._soon(p, st, fc, (1.0,))   # при рециркуляции прогноз подачи ни к чему
            when = f", проба {st.last_sample:%H:%M}" if st.last_sample else ""
            self.t_out.set(f"{st.w_out_read:.1f} %", f"{dev:+.1f} п.п. к цели{when}" + (f"\n{soon}" if soon else ""),
                           "Последняя проба выходного влагомера (как показал прибор) и её отличие от цели прохода на "
                           "влагомере; «через 1 ч» — прогноз по зерну, которое уже в шахте (со всеми режимами, при "
                           "которых оно шло). График — пробы выхода этого прохода, цель и прогноз.")
        else:
            self.t_out.set("—", "проб выхода ещё нет")
        # спарклайн: последние 12 проб выхода этого прохода, цель, прогноз на 2 ч
        pts = [(t, wo) for t, _wi, wo in st.series if wo is not None][-12:]
        if len(pts) >= 2:
            t0 = pts[0][0]
            xs = [(t - t0).total_seconds() / 3600.0 for t, _ in pts]
            ys = [w for _, w in pts]
            fx, fy = [], []
            tr = None if st.recirc else fc.transit
            if tr is not None and len(tr["t"]):
                for tt, w in zip(tr["t"], tr["w"]):
                    h = (tt - t0).total_seconds() / 3600.0
                    if xs[-1] < h <= xs[-1] + 2.0:
                        fx.append(h)
                        fy.append(mdl.read_from_true(p, float(w), st.t_grain))
            self.spark.set_data(xs, ys, goal_read, fx, fy)
        else:
            self.spark.set_data([], [])
        sub = f"после охлаждения {self.w_target.value():g} %"
        if abs(fc.w_goal - self.w_target.value()) >= 0.05:
            sub = f"чтобы в силосе вышло {self.w_target.value():g} %, сейчас нужно {fc.w_goal:.1f} %"
        self.t_goal.set(f"{goal_read:.1f} %", sub, "С чем сравнивать выходной влагомер: цель прохода в его показаниях "
                                                 "(поправка на горячее зерно — «Погода и влагомер»).")
        sl = fc.silo
        if st.silo_w is not None:
            self.t_silo.set(f"{st.silo_w:.1f} %",
                            f"{st.unloaded:.0f} т" + (f", к концу прохода ≈ {sl['avg']:.1f} %"
                                                     if sl and not st.recirc else ""),
                            "Средняя по массе влажность всего, что ушло в приёмный силос с начала прохода (истинная, "
                            "с поправкой влагомера), и ожидаемая к концу прохода.")
        else:
            self.t_silo.set("—", f"выгружено {st.unloaded:.0f} т")
        if fc.pass_end:
            rem = "масса прохода выгружена" if fc.remaining <= 0.05 else f"осталось {fc.remaining:.0f} т"
            self.t_pass.set(f"{fc.pass_end:%d.%m %H:%M}", rem, "Когда закончится выгрузка массы этого прохода при "
                                                               "советуемом D. Полоса — сколько уже выгружено.")
            self.pass_bar.set_value(st.unloaded / fc.m_out_pass if fc.m_out_pass > 0 else None)
        else:
            self.t_pass.set("—")
            self.pass_bar.set_value(None)
        if fc.batch_end:
            n = len(fc.rest_plan.passes) if fc.rest_plan is not None else 0
            rng = (f"90 %: {fc.batch_end_range[0]:%d.%m %H:%M} … {fc.batch_end_range[1]:%d.%m %H:%M}"
                   if fc.batch_end_range else "")
            self.t_batch.set(f"{fc.batch_end:%d.%m %H:%M}",
                             (f"ещё {_passes(n)} до {self.wk_final.value():g} %" if n else "это последний проход"),
                             ("С охлаждением. " + (f"Допуск {rng}." if rng else "")).strip())
        else:
            self.t_batch.set("—")
        if st.recirc:
            # подача прохода закончилась: выгрузки в силос нет, зерно в шахте ходит по кругу
            since = f"рециркуляция с {st.recirc_since:%H:%M}" if st.recirc_since else "рециркуляция"
            self.t_pass.set("закончен", since, "Подача из силосов этого прохода закончилась (в журнале "
                                               "«рециркуляция»): «выгружено» больше не растёт.")
            self.pass_bar.set_value(1.0)
            if st.batch_end:
                self.t_batch.set("высушена", "досушка, затем охлаждение",
                                 "Рециркуляция, когда выход уже у цели партии, — досушка остатка в шахте и конец партии. "
                                 "Сколько досушивать — раздел «Досушка».")

    def _table(self, p, st, fc, c, d_use, now, th, target_read: float) -> None:
        sl = fc.silo
        # таблица вариантов D
        cands = sorted({max(1, min(100, round(x))) for x in (fc.d_rec - 10, fc.d_rec - 5, fc.d_rec, fc.d_rec + 5,
                                                             fc.d_rec + 10, st.d, d_use)})
        corr = st.corr if self.cb_corr.isChecked() else 0.0
        t_now = max(now, st.last) if now else st.last
        self.tbl.setRowCount(len(cands))
        for i, d in enumerate(cands):
            ps = mdl.solve_pass(p, c, st.w_in, 1.0, d)
            w_out = ps.w_out + corr
            q = mdl.q_at(p, d)
            m_out = self.m_pass.value() * (100 - st.w_in) / (100 - w_out)
            rem = max(m_out - st.unloaded, 0.0)
            end = t_now + timedelta(hours=rem / q) if q > 0 else None
            tau = p.lag_k * p.m_dryer / q if q > 0 else 0.0
            if sl:
                part = w_out * sl["m_new"] if sl["m_new"] > 0 else 0.0
                avg = (sl["w_done"] * sl["m_done"] + sl["w_tr"] * sl["m_tr"] + part) / max(
                    sl["m_done"] + sl["m_tr"] + max(sl["m_new"], 0.0), 1e-9)
                avg_txt = f"{avg:.1f}"
            else:
                avg_txt = "—"
            cells = [f"{d:.0f}", f"{q:.1f}", f"{w_out:.1f}", f"{mdl.read_from_true(p, w_out, st.t_grain):.1f}",
                     f"{st.w_in - w_out:.1f}",
                     f"{tau:.1f}", f"{end:%d.%m %H:%M}" if end else "—", avg_txt, f"{ps.t_grain:.0f}"]
            for j, txt in enumerate(cells):
                it = item(txt, align_right=j < 8)
                if abs(d - d_use) < 0.5:                 # советуемое (или ваше) D — «выбранное», золотистым
                    it.setBackground(QBrush(QColor(th.highlight[0])))
                    it.setForeground(QBrush(QColor(th.highlight[1])))
                self.tbl.setItem(i, j, it)

        # график
        xs_in, ys_in, xs_out, ys_out = [], [], [], []
        for t, wi, wo in st.series:
            h = (t - st.start).total_seconds() / 3600.0
            if wi is not None:
                xs_in.append(h)
                ys_in.append(wi)
            if wo is not None:
                xs_out.append(h)
                ys_out.append(wo)
        self.plot.add_points(xs_in, ys_in, "ref", 6)
        self.plot.add_points(xs_out, ys_out, "dot", 7)
        self.plot.add_hline(mdl.read_from_true(p, fc.w_goal, st.t_grain), "target")
        tr = fc.transit
        steady = mdl.read_from_true(p, fc.w_out_rec, st.t_grain)
        legend = [("dot", "ref", "вход"), ("dot", "dot", "выход по влагомеру"), ("dash", "target", "цель на влагомере")]
        if st.recirc:
            pass                            # подача закончилась — прогноз выхода «при этом D» не рисуем
        elif tr is not None and len(tr["t"]):
            # ожидаемый выход по часам: зерно, которое уже в шахте (с его историей), затем новое зерно при этом D
            xs = [(x - st.start).total_seconds() / 3600.0 for x in tr["t"]]
            ys = [mdl.read_from_true(p, float(w), st.t_grain) for w in tr["w"]]
            self.plot.add_line(xs + [xs[-1], xs[-1] + max(1.0, 0.3 * (xs[-1] - xs[0]))], ys + [steady, steady], "line", 3)
            legend.append(("line", "line", "ожидаемый выход при этом D"))
        elif fc.visible_at:
            h0 = (fc.visible_at - st.start).total_seconds() / 3600.0
            self.plot.add_line([h0, h0 + max(fc.tau_h, 1.0)], [steady] * 2, "line", 3)
            legend.append(("line", "line", "ожидаемый выход при этом D"))
        self.legend.set_items(legend)
