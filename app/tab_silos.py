"""Раздел «Размещение по силосам»: куда принять сырое зерно и как гонять его между оперативными силосами за проходы
сушки, чтобы после последнего прохода оно лежало в нужных силосах.

Слева — зерно (сколько сырого принять, влажность, цель, число проходов, начало), таблица оперативных силосов
(вместимость, что лежит сейчас и сколько проходов прошло, где зерно должно оказаться, силос приёмки с поля) и приход
с поля. Справа — вывод крупно, плитки и схема (silo_flow.SiloFlow): на каждый шаг силосы с уровнем зерна и стрелка
«откуда → сушилка → куда»; второй вкладкой — таблица тех же шагов. Схему можно сохранить картинкой. Расчёт —
silos.plan_silos; съём, D и тонны в час по проходам — из модели сушилки (model.plan_by_passes).
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtWidgets import (QComboBox, QDateTimeEdit, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QPushButton, QScrollArea, QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from . import model as mdl
from .silo_flow import SiloFlow
from .silos import EPS, Silo, SiloPlan, plan_silos, used_silos
from .ui_common import Collapsible, Tile, dspin, fmt_hours, item, mute, scroll_wrap, set_role, table, to_num

if TYPE_CHECKING:
    from .gui import MainWindow

NAMES = ("С\u00a01", "С\u00a02", "С\u00a03", "С\u00a04")
DOW = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
COL_NAME, COL_CAP, COL_MASS, COL_DONE, COL_TARGET, COL_INTAKE = range(6)

ABOUT = (
    "<b>Правила, по которым строится план.</b> Сушилка берёт зерно из одного оперативного силоса и сыплет в "
    "другой. Сыпать можно только в пустой силос или к зерну того же прохода — сырое с подсушенным не смешивается. "
    "Зерно идёт проход за проходом. Силос, который опустел в ходе прохода, в этом же проходе может принять зерно."
    "<br><br><b>Что выбирает программа.</b> В какие силосы принять сырое зерно и в каком порядке переключать "
    "маршруты, чтобы после последнего прохода зерно оказалось в отмеченных силосах."
    "<br><br><b>Очерёдность.</b> В новом проходе первым сушится зерно, которое лежит дольше: наполнили сначала "
    "С 3, потом С 4 — сушим сначала из С 3. Если с этим правилом в отмеченные силосы не прийти, программа его "
    "нарушает и пишет, в каких шагах."
    "<br><br><b>Варианты.</b> Подходящих планов обычно много. В списке «Вариант» — лучшие: сначала те, где "
    "очередь соблюдена, затем с меньшим числом переключений и с меньшим числом занятых силосов (свободный силос "
    "— запас под приход с поля)."
    "<br><br><b>Почему нужен пустой силос.</b> Без смешивания первый шаг можно сделать только в пустой силос. "
    "Если заняты все четыре, сушить некуда — программа так и скажет."
    "<br><br><b>Масса.</b> После каждого прохода зерна становится меньше: уходит вода. Тонны «в приёмник» — уже "
    "после сушки; съём влаги, D и тонны в час по проходам — из модели сушилки, как в «Плане сушки партии»."
    "<br><br><b>Приход с поля.</b> Силос приёмки в плане не участвует — он занят приходом. Программа проверит, "
    "хватит ли в нём места до конца плана. Сушка того, что в него пришло, — следующим планом."
    "<br><br><b>Чего план не знает.</b> Зерна в самой сушилке (оно уходит в приёмник в конце шага) и силосов "
    "хранения С 5 – С 8. Вместимости в тоннах зависят от культуры и натуры — впишите свои.")


def _when(t: Optional[datetime]) -> str:
    return f"{t:%d.%m %H:%M}" if t else "—"


def _t(m: float) -> str:
    return f"{m:,.0f}".replace(",", " ")


class SilosTab(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.result: Optional[SiloPlan] = None
        self._loading = True
        lay = QHBoxLayout(self)
        left = QWidget()
        lv = QVBoxLayout(left)

        gb = QGroupBox("Зерно")
        f = QFormLayout(gb)
        self.new_mass = dspin(0, 20000, 0, 0, 50, " т")
        self.new_mass.setToolTip("Сколько сырого зерна ещё принять в оперативные силосы. Программа сама выберет, в "
                                 "какие. Зерно, которое уже лежит, вписывается в таблицу силосов.")
        self.w0 = dspin(6, 50, win.in_w0.value(), 1, 0.5, " %")
        self.wk = dspin(5, 30, win.in_wk.value(), 1, 0.5, " %")
        self.npass = QSpinBox()
        self.npass.setRange(0, 8)
        self.npass.setSpecialValueText("авто — по лимиту съёма")
        self.npass.setSuffix(" прох.")
        self.npass.setToolTip("От числа проходов зависит, в каком силосе зерно окажется в конце. «Авто» — наименьшее "
                              "число проходов, при котором съём за проход в лимите культуры.")
        self.start = QDateTimeEdit(QDateTime.currentDateTime())
        self.start.setCalendarPopup(True)
        self.start.setDisplayFormat("dd.MM.yyyy HH:mm")
        f.addRow("Сырого зерна принять", self.new_mass)
        f.addRow("Влажность с поля W₀", self.w0)
        f.addRow("Цель Wк", self.wk)
        f.addRow("Проходов", self.npass)
        f.addRow("Начало сушки", self.start)
        lv.addWidget(gb)

        gb = QGroupBox("Оперативные силосы")
        gv = QVBoxLayout(gb)
        t = QTableWidget(len(NAMES), 6)
        t.setHorizontalHeaderLabels(["Силос", "Ёмкость,\nт", "Лежит,\nт", "Прошло\nпрох.", "Конеч-\nный", "Приём-\nка"])
        t.verticalHeader().setVisible(False)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for i, name in enumerate(NAMES):
            it = QTableWidgetItem(name)
            it.setFlags(Qt.ItemIsEnabled)
            t.setItem(i, COL_NAME, it)
            for c, v in ((COL_CAP, "1000"), (COL_MASS, ""), (COL_DONE, "")):
                t.setItem(i, c, QTableWidgetItem(v))
            for c in (COL_TARGET, COL_INTAKE):
                it = QTableWidgetItem()
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Unchecked)
                it.setTextAlignment(Qt.AlignCenter)
                t.setItem(i, c, it)
        t.setFixedHeight(t.horizontalHeader().sizeHint().height() + 4 * 34 + 8)
        t.itemChanged.connect(self._table_changed)
        t.horizontalHeaderItem(COL_DONE).setToolTip("Сколько проходов сушилки уже прошло зерно, которое лежит в "
                                                    "силосе. Пусто или 0 — сырое.")
        t.horizontalHeaderItem(COL_TARGET).setToolTip("Отметьте силосы, где зерно должно лежать после последнего "
                                                      "прохода.")
        t.horizontalHeaderItem(COL_INTAKE).setToolTip("Силос, в который продолжает идти зерно с поля. В плане он не "
                                                      "участвует.")
        self.tbl = t
        gv.addWidget(t)
        self.cap_note = mute(QLabel("Вместимости стоят условные (1000 т) — впишите свои: от них зависит весь план."))
        self.cap_note.setWordWrap(True)
        gv.addWidget(self.cap_note)
        f2 = QFormLayout()
        self.inflow = dspin(0, 5000, 0, 0, 50, " т/сут")
        self.inflow.setSpecialValueText("нет — всё зерно уже принято")
        self.inflow.setToolTip("Сколько сырого зерна в сутки продолжает приходить с поля в силос приёмки. Программа "
                               "проверит, хватит ли в нём места до конца плана.")
        f2.addRow("Приход с поля", self.inflow)
        gv.addLayout(f2)
        lv.addWidget(gb)

        self.btn = set_role(QPushButton("Построить план"), "primary")
        self.btn.clicked.connect(self.recalc)
        self.btn_png = QPushButton("Сохранить схему…")
        self.btn_png.setToolTip("Вся схема одной картинкой (PNG) — распечатать или передать сушильщикам.")
        self.btn_png.clicked.connect(self._save_png)
        self.btn_png.setEnabled(False)
        hb = QHBoxLayout()
        hb.addWidget(self.btn)
        hb.addWidget(self.btn_png)
        hb.addStretch(1)
        lv.addLayout(hb)
        lv.addWidget(Collapsible("Как строится план", ABOUT))
        lv.addStretch(1)
        lay.addWidget(scroll_wrap(left, 470), 0)

        right = QWidget()
        rv = QVBoxLayout(right)
        self.head = QLabel("Впишите зерно и силосы и нажмите «Построить план».")
        self.head.setProperty("role", "adviceHead")
        self.head.setWordWrap(True)
        rv.addWidget(self.head)
        self.sub = mute(QLabel(""))
        self.sub.setWordWrap(True)
        rv.addWidget(self.sub)
        tiles = QHBoxLayout()
        self.t_steps = Tile("Переключений маршрута")
        self.t_time = Tile("Конец сушки")
        self.t_mass = Tile("Зерна после сушки")
        self.t_regime = Tile("Проходы")
        for x in (self.t_steps, self.t_time, self.t_mass, self.t_regime):
            tiles.addWidget(x, 1)
        rv.addLayout(tiles)
        # Схема (главный вид): шаг за шагом силосы с уровнем зерна и стрелка «откуда → сушилка → куда». Таблица с теми
        # же шагами — второй вкладкой, для точных цифр и переписывания.
        vb = QHBoxLayout()
        self.var_label = QLabel("Вариант")
        self.variant = QComboBox()
        self.variant.setToolTip("Все найденные планы, лучший первым: сначала те, где зерно сушится по очереди (что "
                                "дольше лежит — то раньше), затем с меньшим числом переключений и занятых силосов.")
        self.variant.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.variant.setMinimumContentsLength(12)
        self.variant.currentIndexChanged.connect(self._variant_changed)
        self.var_note = mute(QLabel(""))
        vb.addWidget(self.var_label)
        vb.addWidget(self.variant, 1)
        vb.addWidget(self.var_note)
        self.var_row = QWidget()
        self.var_row.setLayout(vb)
        vb.setContentsMargins(0, 0, 0, 0)
        self.var_row.setVisible(False)
        rv.addWidget(self.var_row)
        self.views = QTabWidget()
        self.flow = SiloFlow()
        sc = QScrollArea()
        sc.setWidget(self.flow)
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.flow_scroll = sc
        self.views.addTab(sc, "Схема")
        tab_t = QWidget()
        tv = QVBoxLayout(tab_t)
        tv.setContentsMargins(0, 6, 0, 0)
        self.steps = table(["Проход", "Маршрут", "Тонн", "Начало", "Конец"] + list(NAMES), stretch_last=False)
        self.steps.setEditTriggers(QTableWidget.NoEditTriggers)
        tv.addWidget(self.steps, 1)
        self.legend = mute(QLabel("«Тонн» — сколько взято из источника → сколько пришло в приёмник после сушки. В "
                                  "столбцах силосов — что лежит после шага: тонны и, в скобках, сколько проходов "
                                  "прошло зерно (0 — сырое). Жирным — силос, который на этом шаге наполняется."))
        self.legend.setWordWrap(True)
        tv.addWidget(self.legend)
        self.views.addTab(tab_t, "Таблица")
        rv.addWidget(self.views, 1)
        lay.addWidget(right, 1)
        for w in (self.new_mass, self.w0, self.wk, self.inflow):
            w.valueChanged.connect(self._changed)
        self.npass.valueChanged.connect(self._changed)
        self._load()
        self._loading = False

    # ---------------------------------------------------------------- ввод
    def _cell(self, r: int, c: int) -> Optional[float]:
        it = self.tbl.item(r, c)
        return to_num(it.text()) if it is not None and it.text().strip() else None

    def silos(self) -> List[Silo]:
        out = []
        for i, name in enumerate(NAMES):
            cap = self._cell(i, COL_CAP) or 0.0
            mass = self._cell(i, COL_MASS) or 0.0
            done = int(self._cell(i, COL_DONE) or 0)
            out.append(Silo(name, max(cap, 0.0), max(mass, 0.0), max(done, 0),
                            self.tbl.item(i, COL_TARGET).checkState() == Qt.Checked,
                            self.tbl.item(i, COL_INTAKE).checkState() == Qt.Checked))
        return out

    def _table_changed(self, it: QTableWidgetItem) -> None:
        if self._loading:
            return
        if it.column() == COL_INTAKE and it.checkState() == Qt.Checked:       # силос приёмки один
            self._loading = True
            for r in range(self.tbl.rowCount()):
                if r != it.row():
                    self.tbl.item(r, COL_INTAKE).setCheckState(Qt.Unchecked)
            self.tbl.item(it.row(), COL_TARGET).setCheckState(Qt.Unchecked)
            self._loading = False
        if it.column() == COL_CAP:
            self._caps_set = True
            self.cap_note.setVisible(False)
        self._changed()

    def _changed(self, *_a) -> None:
        if self._loading:
            return
        self._save()
        if self.result is not None:
            self.recalc()

    # ---------------------------------------------------------------- расчёт
    def _drying(self, mass: float):
        """План сушки по модели: (число проходов, план). Авто — наименьшее число проходов в лимите съёма культуры."""
        p, c = self.win.params, self.win.conditions()
        w0, wk = self.w0.value(), self.wk.value()
        if self.npass.value() > 0:
            return mdl.plan_by_passes(p, c, mass, w0, wk, self.npass.value())
        crop = self.win.crop()
        plans = [mdl.plan_by_passes(p, c, mass, w0, wk, n) for n in range(1, 9)]
        i = mdl.recommend(plans, crop.soft, crop.hard)
        return plans[i if i is not None else len(plans) - 1]

    def recalc(self) -> None:
        silos = self.silos()
        new = self.new_mass.value()
        total = new + sum(s.mass for s in silos if not s.intake)
        if self.w0.value() <= self.wk.value():
            self._show_error("Влажность с поля должна быть выше цели.")
            return
        pl = self._drying(max(total, 1.0))
        n = len(pl.passes)
        factors = [(100.0 - ps.w_in) / (100.0 - ps.w_out) for ps in pl.passes]
        rates = [ps.q for ps in pl.passes]
        start = self.start.dateTime().toPython()
        res = plan_silos(silos, new, n, factors, rates, self.win.params.overhead_h, start, self.inflow.value())
        self.result, self._drying_plan, self._silos = res, pl, silos
        self._fill_variants(res, silos)
        self._show(res, pl, silos)
        self._save()

    def _fill_variants(self, res: SiloPlan, silos: List[Silo]) -> None:
        names = [s.name for s in silos]
        self.variant.blockSignals(True)
        self.variant.clear()
        for k, v in enumerate(res.variants, 1):
            idle = [names[i] for i, s in enumerate(silos) if not s.intake and i not in used_silos(v)]
            end = v.steps[-1].end if v.steps else None
            self.variant.addItem(
                f"{k}.  {len(v.steps)} перекл."
                + (" · по очереди" if v.fifo_breaks == 0 else f" · не по очереди: {v.fifo_breaks}")
                + (f" · свободен {', '.join(idle)}" if idle else " · заняты все")
                + (" · приём: " + ", ".join(f"{names[i]} {_t(m)}" for i, m in v.placement) if v.placement else "")
                + (f" · конец {_when(end)}" if end else ""))
        self.variant.blockSignals(False)
        self.var_row.setVisible(len(res.variants) > 1)
        shown = len(res.variants)
        if res.n_variants > shown:
            self.var_note.setText(f"{shown} лучших из {res.n_variants}" + ("+" if res.variants_capped else ""))
        else:
            self.var_note.setText(f"всего {shown}")

    def _variant_changed(self, idx: int) -> None:
        if self.result is None or not 0 <= idx < len(self.result.variants):
            return
        self._show(self.result.variants[idx], self._drying_plan, self._silos)

    def current_plan(self) -> Optional[SiloPlan]:
        """План, который сейчас показан (выбранный вариант)."""
        if self.result is None or not self.result.variants:
            return self.result
        return self.result.variants[max(self.variant.currentIndex(), 0)]

    def _show_error(self, text: str) -> None:
        self.result = None
        self.var_row.setVisible(False)
        self.head.setText(text)
        self.sub.setText("")
        self.steps.setRowCount(0)
        self.flow.set_plan(None, [])
        self.btn_png.setEnabled(False)
        for x in (self.t_steps, self.t_time, self.t_mass, self.t_regime):
            x.set("—")

    def _show(self, res: SiloPlan, pl, silos: List[Silo]) -> None:
        names = [s.name for s in silos]
        n = len(pl.passes)
        if not res.steps and not res.ok:
            self._show_error(res.reason)
            self.result = res
            return
        best = self.result
        tg = ", ".join(s.name for s in silos if s.target and not s.intake)
        if res.ok:
            self.head.setText(f"План есть: после {n}-го прохода всё зерно — в {tg}.")
        else:
            self.head.setText("Полностью в отмеченные силосы зерно не приходит — ближайший план ниже.")
        notes = []
        if res.reason:
            notes.append(res.reason)
        if res.placement:
            notes.append("Сырое зерно принять: " + ", ".join(f"{names[i]} — {_t(m)} т" for i, m in res.placement) + ".")
        if not pl.reaches_target:
            notes.append(f"За {n} прох. модель даёт {pl.w_final:.1f} %, а цель {self.wk.value():.1f} % — добавьте проход.")
        if res.ok and res.fifo_breaks:
            only = best is not None and best.fifo_breaks > 0
            notes.append(f"В {res.fifo_breaks} шаг. зерно сушится не по очереди (в другом силосе зерно того же прохода "
                         "лежит дольше)" + (" — иначе в отмеченные силосы не прийти." if only else
                                            " — есть варианты, где очередь соблюдена."))
        notes += res.warnings
        self.sub.setText("  ".join(notes))
        end = res.steps[-1].end if res.steps else None
        self.t_steps.set(str(len(res.steps)), "смен маршрута на пульте")
        self.t_time.set(_when(end), (f"{DOW[end.weekday()]}, " if end else "") + f"всего {fmt_hours(res.hours)}",
                        f"С накладными {self.win.params.overhead_h:g} ч на проход («Настройки сушилки»).")
        dry = sum(m for (m, _d), s in zip(res.final, silos) if not s.intake)
        self.t_mass.set(f"{_t(dry)} т", "  ".join(f"{names[i]}: {_t(m)}" for i, (m, _d) in enumerate(res.final)
                                                   if m > EPS and not silos[i].intake))
        self.t_regime.set(f"{n}", f"{pl.passes[0].w_in:.1f} → {pl.w_final:.1f} %, съём до {pl.max_dw:.1f} п.п.",
                          "  ".join(f"{ps.k}-й: {ps.w_in:.1f}→{ps.w_out:.1f} %, D {ps.d:.0f}" for ps in pl.passes))
        rows = []
        if res.placement:
            rows.append(("приём", "с поля → " + ", ".join(names[i] for i, _m in res.placement),
                         _t(sum(m for _i, m in res.placement)), "", "", res.start_state, -1))
        for s in res.steps:
            rows.append((f"{s.k}-й", f"{names[s.src]} → {names[s.dst]}" + (" (часть)" if s.split else "")
                         + (" · не по очереди" if s.out_of_turn else ""),
                         f"{_t(s.m_in)} → {_t(s.m_out)}", _when(s.start), _when(s.end), s.after, s.dst))
        self.steps.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, v in enumerate(row[:5]):
                self.steps.setItem(r, c, item(v))
            state, dst = row[5], row[6]
            for i, (m, d) in enumerate(state):
                if silos[i].intake:
                    txt = "приёмка"
                elif m <= EPS:
                    txt = "—"
                else:
                    txt = f"{_t(m)} ({d})"
                it = item(txt)
                if i == dst or (dst == -1 and any(i == pi for pi, _m in res.placement)):
                    fnt = it.font()
                    fnt.setBold(True)
                    it.setFont(fnt)
                if m <= EPS and not silos[i].intake:
                    it.setForeground(self.palette().placeholderText())
                self.steps.setItem(r, 5 + i, it)
        self.steps.resizeColumnsToContents()
        self.flow.set_plan(res, silos, pl.passes)
        self.flow_scroll.verticalScrollBar().setValue(0)
        self.btn_png.setEnabled(bool(res.steps))

    def _save_png(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        path, _f = QFileDialog.getSaveFileName(self, "Сохранить схему", "Размещение по силосам.png", "Картинка (*.png)")
        if path:
            ok = self.flow.save_png(path)
            self.win.statusBar().showMessage(("Схема сохранена: " + path) if ok else "Не удалось сохранить схему.", 12000)

    # ---------------------------------------------------------------- состояние
    def _load(self) -> None:
        d = self.win.storage.get_json("ui_silos", {}) or {}
        self._loading = True
        self.new_mass.setValue(float(d.get("new", 0.0)))
        self.w0.setValue(float(d.get("w0", self.win.in_w0.value())))
        self.wk.setValue(float(d.get("wk", self.win.in_wk.value())))
        self.npass.setValue(int(d.get("npass", 0)))
        self.inflow.setValue(float(d.get("inflow", 0.0)))
        rows = d.get("silos") or []
        self._caps_set = bool(d.get("caps_set"))
        for i in range(len(NAMES)):
            r = rows[i] if i < len(rows) else {}
            self.tbl.item(i, COL_CAP).setText(f"{float(r.get('cap', 1000.0)):g}")
            self.tbl.item(i, COL_MASS).setText(f"{float(r['mass']):g}" if r.get("mass") else "")
            self.tbl.item(i, COL_DONE).setText(str(int(r["done"])) if r.get("done") else "")
            self.tbl.item(i, COL_TARGET).setCheckState(Qt.Checked if r.get("target") else Qt.Unchecked)
            self.tbl.item(i, COL_INTAKE).setCheckState(Qt.Checked if r.get("intake") else Qt.Unchecked)
        self.cap_note.setVisible(not self._caps_set)
        self.result = None
        self.steps.setRowCount(0)
        self.flow.set_plan(None, [])
        self.btn_png.setEnabled(False)
        self.var_row.setVisible(False)
        self.head.setText("Впишите зерно и силосы и нажмите «Построить план».")
        self.sub.setText("")
        for x in (self.t_steps, self.t_time, self.t_mass, self.t_regime):
            x.set("—")
        self._loading = False

    def _save(self) -> None:
        self.win.storage.set_json("ui_silos", {
            "new": self.new_mass.value(), "w0": self.w0.value(), "wk": self.wk.value(), "npass": self.npass.value(),
            "inflow": self.inflow.value(), "caps_set": bool(getattr(self, "_caps_set", False)),
            "silos": [{"cap": s.cap, "mass": s.mass, "done": s.done, "target": s.target, "intake": s.intake}
                      for s in self.silos()]})

    def on_show(self) -> None:
        if self.result is not None:
            self.recalc()
