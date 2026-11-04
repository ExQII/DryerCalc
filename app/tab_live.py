"""Раздел «Текущий проход»: что делать с разгрузкой D прямо сейчас, по свежим пробам журнала.

Сверху крупно — одно действие («Поставьте D = 42 с», «D не менять», «Подождите до 09:30»), под ним пять плиток
с главными цифрами (выход, цель, приёмный силос, конец прохода, конец партии), подробности — по «Подробнее»,
ниже таблица «что будет при другом D» и график проб. Логика совета — live.live_advice (правила работы, 7.3).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import (QApplication, QCheckBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QSplitter, QVBoxLayout, QWidget)

from . import model as mdl
from .live import LiveState, live_advice, live_forecast, live_state, outlet_soon
from .plots import SimplePlot
from .theme import theme_for
from .ui_common import Collapsible, Tile, scroll_wrap, dspin, fmt_hours, fnum, item, table

if TYPE_CHECKING:
    from .gui import MainWindow


class LiveTab(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.state: Optional[LiveState] = None
        lay = QHBoxLayout(self)

        self.fc = None
        self.advice = None
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 6, 0)
        gb = QGroupBox("По журналу")
        self.lbl_state = QLabel("—")
        self.lbl_state.setWordWrap(True)
        self.lbl_state.setTextFormat(Qt.RichText)
        QVBoxLayout(gb).addWidget(self.lbl_state)
        lv.addWidget(gb)

        gb = QGroupBox("Задание на проход")
        f = QFormLayout(gb)
        self.m_pass = dspin(1, 20000, 500, 0, 10, " т")
        self.w_target = dspin(8, 40, 24, 1, 0.5, " %")
        self.lbl_target_read = QLabel()
        self.lbl_target_read.setWordWrap(True)
        self.wk_final = dspin(8, 30, 14, 1, 0.5, " %")
        self.t_amb = dspin(-40, 45, 10, 1, 1, " °C")
        self.rh = dspin(5, 100, 70, 0, 5, " %")
        self.d_try = dspin(0, 100, 0, 0, 1, " с")
        self.d_try.setSpecialValueText("подобрать")
        self.cb_corr = QCheckBox("Поправка по последним замерам выхода")
        self.cb_corr.setChecked(True)
        self.cb_now = QCheckBox("Считать до текущего времени (а не до последней пробы)")
        self.cb_silo = QCheckBox("Выравнивать среднюю по силосу")
        self.cb_silo.setChecked(True)
        self.cb_silo.setToolTip(
            "Всё, что выгружено за проход, ложится в один силос — важна его средняя влажность. Если часть уже ушла "
            "суше или сырее цели, программа подберёт D так, чтобы оставшееся зерно выровняло среднее, но не дальше "
            "чем на заданное число п.п. от цели прохода.")
        self.silo_lim = dspin(0.2, 3.0, 1.0, 1, 0.1, " п.п.")
        b_mass = QPushButton("Масса партии — из «План сушки партии»")
        b_mass.clicked.connect(lambda: self.m_pass.setValue(self.win.in_mass.value()))
        b_w = QPushButton("T и RH из таблицы погоды")
        b_w.setToolTip("Наружный воздух из таблицы погоды на время последней пробы.")
        b_w.clicked.connect(self._weather)
        self.m_pass.setToolTip("Масса зерна на входе этого прохода, т (на первом проходе — масса партии).")
        self.w_target.setToolTip("Желаемая влажность на выходе этого прохода — средняя по приёмному силосу, после "
                                 "охлаждения (как в лаборатории).")
        f.addRow("Масса на входе прохода", self.m_pass)
        f.addRow(b_mass)
        f.addRow("Цель прохода (после охл.)", self.w_target)
        f.addRow(self.lbl_target_read)
        f.addRow("Цель партии Wк", self.wk_final)
        lv.addWidget(gb)
        gb = QGroupBox("Приёмный силос")
        f = QFormLayout(gb)
        f.addRow(self.cb_silo)
        f.addRow("…не дальше от цели, ±", self.silo_lim)
        lv.addWidget(gb)
        gb = QGroupBox("Наружный воздух")
        f = QFormLayout(gb)
        f.addRow("T", self.t_amb)
        f.addRow("RH", self.rh)
        f.addRow(b_w)
        lv.addWidget(gb)
        gb = QGroupBox("Проверить своё D")
        f = QFormLayout(gb)
        f.addRow("Что будет при D", self.d_try)
        lv.addWidget(gb)
        self.gb_calc = QGroupBox("Расчёт")
        f = QFormLayout(self.gb_calc)
        f.addRow(self.cb_corr)
        f.addRow(self.cb_now)
        lv.addWidget(self.gb_calc)
        b = QPushButton("Обновить по журналу")
        b.clicked.connect(self.refresh)
        lv.addWidget(b)
        self.hint = Collapsible(
            "Как пользоваться",
            "1. Внесите пробу в «Журнал проб».<br>2. Посмотрите сюда: крупная строка сверху — что сделать с D.<br>"
            "3. «Поставьте D = …» — поставьте и запишите изменение («Изменить режим…» в журнале). «D не менять» "
            "или «Подождите» — ничего не трогайте.<br><br>Почему не чаще: зерно, уже находящееся в шахте, выходит "
            "со старой скоростью — смена D видна на выходе только через время «Заметно через …». Разница меньше 3 с "
            "— это ≈ 0,5 п.п. на выходе, на уровне точности влагомера. Подробно — «Справка» → «Правила работы», "
            "раздел 7.")
        self.lbl_hint = self.hint.content
        lv.addWidget(self.hint)
        lv.addStretch(1)

        right = QSplitter(Qt.Vertical)
        top = QWidget()
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 0, 0, 0)
        # главное действие
        self.card = QFrame()
        self.card.setObjectName("adviceCard")
        cv = QVBoxLayout(self.card)
        cv.setContentsMargins(14, 10, 14, 10)
        cv.setSpacing(4)
        ch = QHBoxLayout()
        self.lbl_head = QLabel("—")
        self.lbl_head.setWordWrap(True)
        ch.addWidget(self.lbl_head, 1)
        self.btn_go = QPushButton()
        self.btn_go.setVisible(False)
        self.btn_go.clicked.connect(self._go)
        ch.addWidget(self.btn_go, 0, Qt.AlignTop)
        cv.addLayout(ch)
        self.lbl_sub = QLabel()
        self.lbl_sub.setWordWrap(True)
        self.lbl_sub.setTextFormat(Qt.RichText)
        cv.addWidget(self.lbl_sub)
        tv.addWidget(self.card)
        # главные цифры
        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        self.t_out = Tile("Выход (влагомер)")
        self.t_goal = Tile("Цель на влагомере")
        self.t_silo = Tile("Приёмный силос")
        self.t_pass = Tile("Конец прохода")
        self.t_batch = Tile("Конец партии")
        self.tiles = (self.t_out, self.t_goal, self.t_silo, self.t_pass, self.t_batch)
        for t in self.tiles:
            tiles.addWidget(t, 1)
        tv.addLayout(tiles)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.RichText)
        sc = QScrollArea()                 # полный текст расчёта длинный — в прокрутке, чтобы не обрезался
        sc.setWidget(self.summary)
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setMinimumHeight(170)
        self.details = Collapsible("Подробнее: силос, ближайшие часы, допуск, расчёт", sc)
        tv.addWidget(self.details)
        tv.addWidget(QLabel("Что будет при другом D (вход и температура — как сейчас)"))
        self.tbl = table(["D, с", "Q, т/ч", "W вых, %", "Влагомер покажет, %", "Съём, п.п.", "Видно через, ч",
                          "Конец прохода", "Среднее по силосу, %", "T зерна ≈, °C"])
        tv.addWidget(self.tbl)
        right.addWidget(top)
        self.plot = SimplePlot()
        self.plot.set_labels("часы от начала прохода", "влажность, %")
        right.addWidget(self.plot)
        right.setSizes([470, 290])
        lay.addWidget(scroll_wrap(left, 370), 0)
        lay.addWidget(right, 1)
        left.setMaximumWidth(350)

        for w in (self.m_pass, self.w_target, self.wk_final, self.t_amb, self.rh, self.d_try, self.silo_lim):
            w.valueChanged.connect(self.recalc)
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
            self.details.set_expanded(True)
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
        self.state = live_state(self.win.journal_rows(), p, now=now, weather=self.win.weather_series())
        st = self.state
        if st.t_amb is not None:
            self.t_amb.blockSignals(True)
            self.t_amb.setValue(st.t_amb)
            self.t_amb.blockSignals(False)
        if st.rh is not None:
            self.rh.blockSignals(True)
            self.rh.setValue(st.rh)
            self.rh.blockSignals(False)
        if st.start is not None:
            tg = f"{st.t_grain:.0f} °C" if st.t_grain is not None else "—"
            tp = f"{st.t_proc:.0f} °C" if st.t_proc is not None else "—"
            self.lbl_state.setText(
                f"Партия <b>{st.batch or '—'}</b>, проход <b>{(f'{st.pass_no:g}' if st.pass_no is not None else '—')}</b><br>"
                f"Начало прохода: <b>{st.start:%d.%m %H:%M}</b>; последняя запись: <b>{st.last:%d.%m %H:%M}</b> "
                f"({fmt_hours(st.hours)} от начала)<br>"
                f"Вход сейчас: <b>{fnum(st.w_in)} %</b>; выход (влагомер): <b>{fnum(st.w_out_read)} %</b><br>"
                f"D: <b>{fnum(st.d, 0)} с</b> (Q = {fnum(mdl.q_at(p, st.d) if st.d else None)} т/ч); "
                f"T блоков: <b>{tp}</b>; T зерна: <b>{tg}</b><br>"
                f"Выгружено с начала прохода: <b>{fnum(st.unloaded, 0)} т</b>"
                + (f", средняя влажность в силосе <b>{st.silo_w:.1f} %</b> (влагомер {st.silo_w_read:.1f} %)"
                   if st.silo_w is not None else "") + "<br>"
                f"Поправка модели по {st.corr_n} последним замерам: <b>{st.corr:+.2f} п.п.</b>"
                + ("<br><b>Сейчас рециркуляция</b> (из силоса не подаётся) — время досушки в разделе «Досушка»."
                   if st.recirc else ""))
        else:
            self.lbl_state.setText(st.message or "—")
        self.recalc()

    # ------------------------------------------------------------------
    def set_operator(self, op: bool) -> None:
        """Режим сушильщика: без инженерных настроек расчёта."""
        self.gb_calc.setVisible(not op)

    def _go(self) -> None:
        name = (self.advice or {}).get("go")
        if name:
            self.win.tabs.setCurrentIndex(self.win.tabs.indexOfText(name))

    def _show_advice(self, adv: dict) -> None:
        """Карточка с одним действием: цвет по смыслу (зелёная — не менять, жёлтая — поставить / подождать,
        голубая — сведения), предупреждения — красным."""
        self.advice = adv
        th = theme_for(self)
        pt = QApplication.font().pointSizeF()
        kind = adv["kind"]
        if kind == "keep":
            bg, fg = th.banner("green")
        elif kind in ("change", "wait"):
            bg, fg = th.banner("amber")
        else:
            bg, fg = th.summary_bg, th.summary_fg
        self.card.setStyleSheet(f"#adviceCard {{ background:{bg}; border-radius:8px; }} "
                                f"#adviceCard QLabel {{ color:{fg}; background:transparent; }}")
        self.lbl_head.setStyleSheet(f"font-size:{pt + 9:.1f}pt; font-weight:700;")
        self.lbl_sub.setStyleSheet(f"font-size:{pt + 1:.1f}pt;")
        self.lbl_head.setText(adv["head"])
        err = "#ffb4b4" if th.dark else "#8a1c1c"
        sub = "<br>".join(adv["sub"]) + "".join(f"<br><span style='color:{err}'><b>⚠ {w}</b></span>" for w in adv["warn"])
        self.lbl_sub.setText(sub)
        go = adv.get("go")
        self.btn_go.setVisible(bool(go))
        if go:
            self.btn_go.setText(f"Открыть «{go}»")
        for t in self.tiles:
            t.style_for(th, pt)

    def _clear_tiles(self) -> None:
        for t in self.tiles:
            t.set("—")

    def recalc(self) -> None:
        p = self.win.params
        th = theme_for(self)
        self.summary.setStyleSheet(f"background:{th.summary_bg}; color:{th.summary_fg}; padding:10px;")
        self.lbl_hint.setStyleSheet(f"color:{th.muted};")
        tg_now = self.state.t_grain if self.state is not None else None
        target_read = mdl.read_from_true(p, self.w_target.value(), tg_now)
        self.lbl_target_read.setText(f"На выходном влагомере это ≈ {target_read:.1f} % "
                                     f"(поправка {target_read - self.w_target.value():+.1f} п.п.)")
        self.lbl_target_read.setStyleSheet(f"color:{th.muted};")
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
            soon = self._soon(p, st, fc, (1.0,))
            self.t_out.set(f"{st.w_out_read:.1f} %",
                           (f"проба {st.last_sample:%H:%M} · " if st.last_sample else "") + f"от цели {dev:+.1f} п.п."
                           + (f" · {soon}" if soon else ""),
                           "Последняя проба выходного влагомера (как показал прибор) и её отличие от цели прохода на "
                           "влагомере; «через 1 ч» — прогноз по зерну, которое уже в шахте (со всеми режимами, при "
                           "которых оно шло).")
        else:
            self.t_out.set("—", "проб выхода ещё нет")
        sub = f"после охлаждения {self.w_target.value():g} %"
        if abs(fc.w_goal - self.w_target.value()) >= 0.05:
            sub = f"для силоса сейчас {fc.w_goal:.1f} % (цель прохода {self.w_target.value():g} %)"
        self.t_goal.set(f"{goal_read:.1f} %", sub, "С чем сравнивать выходной влагомер: цель прохода в его показаниях "
                                                 "(поправка на горячее зерно — «Погода и влагомер»).")
        sl = fc.silo
        if st.silo_w is not None:
            self.t_silo.set(f"{st.silo_w:.1f} %",
                            f"{st.unloaded:.0f} т" + (f" · к концу ≈ {sl['avg']:.1f} %" if sl else ""),
                            "Средняя по массе влажность всего, что ушло в приёмный силос с начала прохода (истинная, "
                            "с поправкой влагомера), и ожидаемая к концу прохода.")
        else:
            self.t_silo.set("—", f"выгружено {st.unloaded:.0f} т")
        if fc.pass_end:
            rem = "выгружен" if fc.remaining <= 0.05 else f"осталось {fc.remaining:.0f} т"
            self.t_pass.set(f"{fc.pass_end:%d.%m %H:%M}", rem, "Когда закончится выгрузка массы этого прохода при "
                                                               "советуемом D.")
        else:
            self.t_pass.set("—")
        if fc.batch_end:
            n = len(fc.rest_plan.passes) if fc.rest_plan is not None else 0
            rng = (f"90 %: {fc.batch_end_range[0]:%d.%m %H:%M} … {fc.batch_end_range[1]:%d.%m %H:%M}"
                   if fc.batch_end_range else "")
            self.t_batch.set(f"{fc.batch_end:%d.%m %H:%M}",
                             (f"ещё {n} прох. до {self.wk_final.value():g} %" if n else "это последний проход"),
                             ("С охлаждением. " + (f"Допуск {rng}." if rng else "")).strip())
        else:
            self.t_batch.set("—")

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
                if abs(d - d_use) < 0.5:
                    it.setBackground(QBrush(th.level(0)[0]))
                    it.setForeground(QBrush(th.level(0)[1]))
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
        if tr is not None and len(tr["t"]):
            # ожидаемый выход по часам: зерно, которое уже в шахте (с его историей), затем новое зерно при этом D
            xs = [(x - st.start).total_seconds() / 3600.0 for x in tr["t"]]
            ys = [mdl.read_from_true(p, float(w), st.t_grain) for w in tr["w"]]
            self.plot.add_line(xs + [xs[-1], xs[-1] + max(1.0, 0.3 * (xs[-1] - xs[0]))], ys + [steady, steady], "line", 3)
        elif fc.visible_at:
            h0 = (fc.visible_at - st.start).total_seconds() / 3600.0
            self.plot.add_line([h0, h0 + max(fc.tau_h, 1.0)], [steady] * 2, "line", 3)
        self.plot.set_title("серые — вход, синие — выход по влагомеру; красная — цель на влагомере сейчас; "
                            "линия — ожидаемый выход по часам при этом D")
