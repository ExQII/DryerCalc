"""Вкладка «Партии и очередь»: учёт партий (весовая, топливо, отчёты), смешивание и очередь сушки."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Dict, List, Optional

from PySide6.QtCore import QDateTime, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDateTimeEdit, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea,
                               QSizePolicy, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
                               QWidget)

from .batches import (BATCH_FIELDS, BATCH_HEADERS, BATCH_TEXT, QUALITY_NAMES, REGIME_NAMES, QueueEntry,
                      batches_from_journal, correlation, fuel_rate_from_batches, mix, quality_findings, quality_table,
                      schedule, season_meter_shift)
from .plots import SimplePlot
from .theme import mix as mix_color, theme_for
from .ui_common import (Collapsible, dspin, fmt_hours, fnum, item, menu_button, mute, set_role, size_by_current, table,
                        to_num)

if TYPE_CHECKING:
    from .gui import MainWindow

QUEUE_FIELDS = ["name", "mass", "w0", "wk", "ready", "passes"]
QUEUE_HEADERS = ["Партия", "Масса, т", "W₀, %", "Wк, %", "Готова к сушке\n(дд.мм.гггг чч:мм)",
                 "Проходов\n(пусто — авто)"]


class GanttChart(QWidget):
    """Полосы партий во времени; внутри полосы — проходы (чередование оттенков), промежутки — накладные.
    Усик справа от полосы — допуск окончания 90 % (end_range); вертикальный пунктир — отметка времени (marks)."""

    def __init__(self, empty_text: str = "Добавьте партии в очередь"):
        super().__init__()
        self.entries: List[QueueEntry] = []
        self.marks: List[tuple] = []
        self.empty_text = empty_text
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(700, 220)

    def set_entries(self, entries: List[QueueEntry], marks=None) -> None:
        self.entries = [e for e in entries if e.plan is not None and e.start and e.end]
        self.marks = list(marks or [])          # [(время, подпись)]
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        th = theme_for(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, th.plot_bg)
        if not self.entries:
            p.setPen(th.plot_ink)
            p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, self.empty_text)
            p.end()
            return
        fm = QFontMetrics(self.font())
        left = max(fm.horizontalAdvance(e.name) for e in self.entries) + 16
        top, bottom, right = (8 + fm.height() + 4 if self.marks else 8), 26, 12
        ends = [e.end for e in self.entries] + [e.end_range[1] for e in self.entries if e.end_range]
        t0 = min([e.start for e in self.entries] + [t for t, _ in self.marks])
        t1 = max(ends + [t for t, _ in self.marks])
        span = max((t1 - t0).total_seconds() / 3600.0, 1.0)
        pw = w - left - right
        row_h = min(34.0, (h - top - bottom) / len(self.entries))

        def x_of(t: datetime) -> float:
            return left + (t - t0).total_seconds() / 3600.0 / span * pw

        # сетка по суткам
        p.setPen(QPen(th.plot_grid, 1))
        day = datetime(t0.year, t0.month, t0.day) + timedelta(days=1)
        step = 1 if span <= 24 * 12 else 2 if span <= 24 * 25 else 7
        while day < t1:
            x = x_of(day)
            p.setPen(QPen(th.plot_grid, 1))
            p.drawLine(QPointF(x, top), QPointF(x, h - bottom))
            p.setPen(th.plot_ink)
            p.drawText(QRectF(x - 40, h - bottom + 4, 80, 18), Qt.AlignHCenter | Qt.AlignTop, f"{day:%d.%m}")
            day += timedelta(days=step)
        base = th.role("line")
        alt = mix_color(base, th.plot_bg, 0.35)
        for i, e in enumerate(self.entries):
            y = top + i * row_h + 4
            bh = row_h - 8
            p.setPen(th.plot_ink)
            p.drawText(QRectF(4, y, left - 8, bh), Qt.AlignRight | Qt.AlignVCenter, e.name)
            t = e.start
            for k, ps in enumerate(e.plan.passes):
                a, b = t, t + timedelta(hours=ps.time_h)
                p.fillRect(QRectF(x_of(a), y, max(x_of(b) - x_of(a), 1.0), bh), base if k % 2 == 0 else alt)
                t = b + timedelta(hours=e.plan.overhead_h)
            if e.plan.cool_h:
                a = e.end - timedelta(hours=e.plan.cool_h)
                p.fillRect(QRectF(x_of(a), y, max(x_of(e.end) - x_of(a), 1.0), bh), th.role("ref"))
            p.setPen(QPen(th.plot_ink, 1))
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(x_of(e.start), y, x_of(e.end) - x_of(e.start), bh))
            if e.end_range:                      # допуск окончания: усик с засечками
                a, b = x_of(e.end_range[0]), x_of(e.end_range[1])
                ym = y + bh / 2
                p.setPen(QPen(th.plot_ink, 1.5))
                p.drawLine(QPointF(a, ym), QPointF(b, ym))
                for xx in (a, b):
                    p.drawLine(QPointF(xx, ym - bh * 0.3), QPointF(xx, ym + bh * 0.3))
        for t, label in self.marks:              # отметки времени: пунктир через все полосы, подпись сверху
            x = x_of(t)
            pen = QPen(th.role("target"), 1.5)
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(x, top - 2), QPointF(x, h - bottom))
            p.setPen(th.plot_ink)
            txt = f"{label} {t:%d.%m %H:%M}"
            tw = fm.horizontalAdvance(txt)
            tx = min(max(x - tw / 2, 2.0), w - tw - 2)
            p.drawText(QRectF(tx, 4, tw + 2, fm.height()), Qt.AlignLeft | Qt.AlignVCenter, txt)
        p.end()


class BatchesTab(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self._loading = False
        tabs = QTabWidget()
        QVBoxLayout(self).addWidget(tabs)
        tabs.addTab(self._build_batches(), "Партии (весовая, топливо, отчёты)")
        tabs.addTab(self._build_quality(), "Качество и режим")
        tabs.addTab(self._build_queue(), "Очередь и смешивание")
        self.sub = tabs
        size_by_current(tabs)
        tabs.currentChanged.connect(lambda _i: self.on_show())
        self._load()

    # ==================================================================
    # Партии
    # ==================================================================
    def _build_batches(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        hint = QLabel(
            "Одна строка — одна партия. Время начала и конца связывает партию с журналом (проходы, часы работы). "
            "Массы — по весовой, влажность — лабораторная или после охлаждения. Топливо — сколько израсходовано за "
            "партию (по счётчику или накладным), в той же единице, что в «Плане сушки партии».")
        hint.setWordWrap(True)
        lay.addWidget(Collapsible("Как заполнять партии", hint))
        self.b_hint = hint
        bar = QHBoxLayout()
        bar.setSpacing(6)
        for text, slot, role in (("Добавить партию", self._add_batch, None),
                                 ("Создать из журнала", self._from_journal, None),
                                 ("Удалить выделенные", self._del_batch, "danger")):
            b = QPushButton(text)
            if role:
                set_role(b, role)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch(1)
        b_rep, _acts = menu_button("Отчёты", (("Отчёт PDF…", self._report_pdf), ("Отчёт XLSX…", self._report_xlsx),
                                              ("Сводка сезона XLSX…", self._season_xlsx)),
                                   "Отчёт по выбранной партии (PDF или Excel) и сводка сезона по всем партиям.")
        bar.addWidget(b_rep)
        b = QPushButton("Перенести расход топлива в план")
        b.setToolTip("Удельный расход топлива на 1 т испарённой воды по всем партиям — в «План сушки партии».")
        b.clicked.connect(self._fuel_to_calc)
        bar.addWidget(b)
        lay.addLayout(bar)
        split = QSplitter(Qt.Vertical)
        self.tbl_b = QTableWidget(0, len(BATCH_FIELDS))
        self.tbl_b.setHorizontalHeaderLabels([BATCH_HEADERS[f] for f in BATCH_FIELDS])
        self.tbl_b.horizontalHeader().setStretchLastSection(True)
        self.tbl_b.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_b.itemChanged.connect(self._batch_edited)
        self.tbl_b.itemSelectionChanged.connect(self._show_batch)
        split.addWidget(self.tbl_b)
        bottom = QSplitter(Qt.Horizontal)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        self.b_detail = QLabel()
        self.b_detail.setTextFormat(Qt.RichText)
        self.b_detail.setWordWrap(True)
        self.b_detail.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.b_detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        sc.setWidget(self.b_detail)
        bottom.addWidget(sc)
        pw = QWidget()
        pv = QVBoxLayout(pw)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.addWidget(QLabel("Проходы по журналу за время партии"))
        self.tbl_bp = table(["Начало", "Конец", "Часов", "W вх, %", "W вых (влагомер), %", "Съём, п.п.", "D, с",
                             "T, °C", "Выгружено, т"])
        pv.addWidget(self.tbl_bp)
        bottom.addWidget(pw)
        bottom.setSizes([520, 760])
        split.addWidget(bottom)
        split.setSizes([300, 420])
        lay.addWidget(split, 1)
        return w

    def _batches(self) -> List[Dict]:
        out = []
        for i in range(self.tbl_b.rowCount()):
            b: Dict = {}
            for j, f in enumerate(BATCH_FIELDS):
                it = self.tbl_b.item(i, j)
                txt = it.text().strip() if it else ""
                b[f] = txt if f in BATCH_TEXT else to_num(txt)
            out.append(b)
        return out

    def _fill_batches(self, items: List[Dict]) -> None:
        self._loading = True
        try:
            self.tbl_b.setRowCount(len(items))
            for i, b in enumerate(items):
                for j, f in enumerate(BATCH_FIELDS):
                    v = b.get(f)
                    txt = "" if v is None else (str(v) if f in BATCH_TEXT else f"{v:g}")
                    self.tbl_b.setItem(i, j, QTableWidgetItem(txt))
            self.tbl_b.resizeColumnsToContents()
        finally:
            self._loading = False

    def _save_batches(self) -> None:
        self.win.storage.set_list("batches", self._batches())

    def _batch_edited(self, *_a) -> None:
        if self._loading:
            return
        self._save_batches()
        self._show_batch()

    def _add_batch(self) -> None:
        items = self._batches()
        items.append({"name": f"Партия {len(items) + 1}", "start": datetime.now().strftime("%d.%m.%Y %H:%M")})
        self._fill_batches(items)
        self._save_batches()
        self.tbl_b.selectRow(len(items) - 1)

    def _del_batch(self) -> None:
        rows = sorted({i.row() for i in self.tbl_b.selectedIndexes()}, reverse=True)
        if not rows:
            return
        if QMessageBox.question(self, "Удалить", f"Удалить партий: {len(rows)}?") != QMessageBox.Yes:
            return
        items = self._batches()
        for r in rows:
            del items[r]
        self._fill_batches(items)
        self._save_batches()
        self._show_batch()

    def _from_journal(self) -> None:
        drafts = batches_from_journal(self.win.journal_rows())
        if not drafts:
            QMessageBox.information(self, "Партии из журнала",
                                    "В журнале нет колонки «Партия» с названиями или не распознано время проб.")
            return
        items = self._batches()
        have = {str(b.get("name")) for b in items}
        new = [d for d in drafts if d["name"] not in have]
        items.extend(new)
        self._fill_batches(items)
        self._save_batches()
        QMessageBox.information(self, "Партии из журнала",
                                f"Добавлено партий: {len(new)}" + (f" ({', '.join(d['name'] for d in new)})" if new else "") +
                                ". Впишите массы с весовой, влажность и топливо.")

    def _current(self) -> Optional[Dict]:
        rows = self.tbl_b.selectionModel().selectedRows() if self.tbl_b.selectionModel() else []
        r = rows[0].row() if rows else self.tbl_b.currentRow()
        items = self._batches()
        return items[r] if 0 <= r < len(items) else None

    def _show_batch(self) -> None:
        from .reports import batch_report_data
        th = theme_for(self)
        mute(self.b_hint)
        b = self._current()
        self.tbl_bp.setRowCount(0)
        unit = self.win.fuel_unit()
        season = self._season_text()
        if b is None:
            self.b_detail.setText("Выберите партию в таблице.<br><br>" + season)
            return
        data = batch_report_data(b, self.win.journal_rows(), self.win.params, self.win.fuel_lhv())
        bal, fu, js = data["balance"], data["fuel"], data["journal"]
        lines = [f"<b>Партия «{b.get('name') or ''}»</b>"]
        if not bal:
            lines.append("Впишите массу и влажность до сушки — тогда появится сверка.")
        else:
            lines.append(f"Сухое вещество: <b>{fnum(bal.get('dm'))} т</b>")
            if "m_out_expected" in bal:
                lines.append(f"Масса после сушки по расчёту (по W вых): <b>{fnum(bal['m_out_expected'])} т</b>; "
                             f"убыль по формуле {fnum(bal.get('shrink_formula_pct'), 2)} %")
            if "m_diff" in bal:
                col = th.error if abs(bal["w_diff"]) > 0.5 else th.text.name()
                lines.append(f"Весовая: {fnum(_n(b.get('m_out')))} т — расхождение <b>{bal['m_diff']:+.1f} т "
                             f"({bal['m_diff_pct']:+.1f} %)</b>; фактическая убыль {fnum(bal.get('shrink_actual_pct'), 2)} %")
                lines.append(f"<span style='color:{col}'>W после сушки по массам <b>{bal['w_out_by_mass']:.2f} %</b> "
                             f"против лабораторной {fnum(_n(b.get('w_out')))} % — разница <b>{bal['w_diff']:+.2f} п.п.</b></span>")
                if bal["w_diff"] > 0.3:
                    lines.append("Масса после сушки больше расчётной: влагомеры, возможно, занижают W после сушки "
                                 "(или завышают до сушки).")
                elif bal["w_diff"] < -0.3:
                    lines.append("Масса после сушки меньше расчётной: неучтённые потери (отходы, пыль, просыпи) или "
                                 "влагомеры завышают W после сушки.")
            elif "w_out_by_mass" in bal:
                lines.append(f"W после сушки по массам: <b>{bal['w_out_by_mass']:.2f} %</b>")
            water = bal.get("water_by_mass", bal.get("water_by_moisture"))
            lines.append(f"Испарено воды: <b>{fnum(water)} т</b>; тонно-проценты: <b>{fnum(bal.get('ton_percent'), 0)} т·%</b>")
        if fu:
            lines.append(f"Топливо: <b>{fnum(fu.get('fuel'), 0)} {unit}</b> — {fnum(fu.get('per_water'), 1)} {unit}/т воды, "
                         f"{fnum(fu.get('per_ton_percent'), 2)} {unit}/т·%, {fnum(fu.get('per_ton_dried'), 2)} {unit}/т сухого")
            if "cost" in fu:
                lines.append(f"Стоимость: <b>{fnum(fu['cost'], 0)} ₽</b> — {fnum(fu.get('cost_per_ton_dried'), 0)} ₽/т сухого, "
                             f"{fnum(fu.get('cost_per_ton_percent'), 1)} ₽/т·%, {fnum(fu.get('cost_per_water'), 0)} ₽/т воды")
            if "mj_per_kg" in fu:
                lines.append(f"Тепло: {fnum(fu['energy_gj'], 1)} ГДж — <b>{fu['mj_per_kg']:.2f} МДж на 1 кг воды</b>, "
                             f"тепловой КПД ≈ <b>{100 * fu['thermal_eff']:.0f} %</b> (на испарение нужно ≈ 2,5 МДж/кг; "
                             f"остальное уходит с отработанным воздухом, на прогрев зерна и через стенки)")
            elif self.win.fuel_lhv() is None:
                lines.append("Тепловой КПД: задайте теплоту сгорания топлива в разделе «План сушки партии» (группа «Топливо»).")
        q = [(n, b.get(k)) for k, n in (("cracks", "трещиноватость"), ("broken", "битое"), ("test_weight", "натура"),
                                        ("germ", "всхожесть")) if b.get(k) is not None]
        if q or b.get("purpose"):
            lines.append("Качество: " + (f"{b.get('purpose')}; " if b.get("purpose") else "") +
                         ", ".join(f"{n} {v:g}" for n, v in q))
        if js.get("passes"):
            mx = max((x["dw"] for x in js["passes"] if x.get("dw") is not None), default=None)
            lines.append(f"По журналу: проходов {len(js['passes'])}, работы {fmt_hours(js['hours'])}"
                         + (f", максимальный съём за проход {mx:.1f} п.п." if mx is not None else ""))
        lines.append("<br>" + season)
        self.b_detail.setText("<br>".join(lines))
        passes = js.get("passes", [])
        self.tbl_bp.setRowCount(len(passes))
        for i, x in enumerate(passes):
            cells = [f"{x['start']:%d.%m %H:%M}", f"{x['end']:%d.%m %H:%M}", fnum(x["hours"]), fnum(x["w_in"]),
                     fnum(x["w_out_read"]), fnum(x["dw"]), fnum(x["d"], 0), fnum(x["t"], 0), fnum(x["unloaded"], 0)]
            for j, txt in enumerate(cells):
                self.tbl_bp.setItem(i, j, item(txt, align_right=j >= 2))

    def _season_text(self) -> str:
        items = self._batches()
        rate, n = fuel_rate_from_batches(items)
        shift, se, k = season_meter_shift(items)
        parts = [f"<b>Сезон:</b> партий {len(items)}"]
        if rate is not None:
            parts.append(f"удельный расход топлива по {n} партиям: <b>{rate:.1f} {self.win.fuel_unit()}/т воды</b>")
        if shift is not None:
            txt = f"сдвиг «W по массам − W лаб.» по {k} партиям: <b>{shift:+.2f} п.п.</b>"
            if se is not None:
                txt += f" (±{1.96 * se:.2f} на 95 %)"
            parts.append(txt)
        return "; ".join(parts) + "."

    def _report_data(self):
        b = self._current()
        if b is None:
            QMessageBox.information(self, "Отчёт", "Выберите партию в таблице.")
            return None, None
        from .reports import batch_report_data
        return b, batch_report_data(b, self.win.journal_rows(), self.win.params, self.win.fuel_lhv())

    def _report_pdf(self) -> None:
        b, data = self._report_data()
        if data is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Отчёт по партии (PDF)", f"партия_{_safe(b.get('name'))}.pdf", "PDF (*.pdf)")
        if path:
            from .reports import batch_report_html, html_to_pdf
            html_to_pdf(batch_report_html(data, self.win.fuel_unit()), path)
            _open_hint(self, path)

    def _report_xlsx(self) -> None:
        b, data = self._report_data()
        if data is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Отчёт по партии (Excel)", f"партия_{_safe(b.get('name'))}.xlsx", "Excel (*.xlsx)")
        if path:
            from .reports import batch_report_xlsx
            batch_report_xlsx(path, data, self.win.fuel_unit())
            _open_hint(self, path)

    def _season_xlsx(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Сводка сезона (Excel)", "сезон.xlsx", "Excel (*.xlsx)")
        if path:
            from .reports import season_xlsx
            season_xlsx(path, self._batches(), self.win.fuel_unit(), self.win.fuel_lhv())
            _open_hint(self, path)

    def _fuel_to_calc(self) -> None:
        rate, n = fuel_rate_from_batches(self._batches())
        if rate is None:
            QMessageBox.information(self, "Удельный расход", "Нет партий с топливом и массами/влажностью.")
            return
        if QMessageBox.question(self, "Удельный расход",
                                f"По {n} партиям: {rate:.1f} {self.win.fuel_unit()} на 1 т испарённой воды.\n"
                                f"Подставить в «План сушки партии» (расход топлива в сценариях)?") == QMessageBox.Yes:
            self.win.in_fuel.setValue(round(rate, 1))
            prices = [_n(b.get("fuel_price")) for b in self._batches() if _n(b.get("fuel_price"))]
            if prices and self.win.in_fuel_price.value() <= 0:
                self.win.in_fuel_price.setValue(prices[-1])

    # ==================================================================
    # Качество и режим
    # ==================================================================
    def _build_quality(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.q_hint = QLabel(
            "Связь качества зерна с режимом сушки по партиям сезона. Впишите в таблицу партий трещиноватость, "
            "битое/дроблёное, натуру и (для семенного) всхожесть — режим программа возьмёт из журнала за время "
            "партии: максимальный съём за проход, среднюю T блоков, максимальную T зерна. Когда партий наберётся "
            "5 и больше, станет видно, при каком съёме за проход растёт трещиноватость — и лимит «≤ 6 п.п.» можно "
            "будет обосновать или поправить по своим данным. Связь (r) — не доказательство причины: сравнивайте "
            "партии одного гибрида и назначения.")
        self.q_hint.setWordWrap(True)
        lay.addWidget(Collapsible("Что показывает эта вкладка", self.q_hint))
        hb = QHBoxLayout()
        hb.addWidget(QLabel("По оси Y:"))
        self.q_y = QComboBox()
        for k, n in QUALITY_NAMES.items():
            self.q_y.addItem(n, k)
        hb.addWidget(self.q_y)
        hb.addWidget(QLabel("По оси X:"))
        self.q_x = QComboBox()
        for k, n in REGIME_NAMES.items():
            self.q_x.addItem(n, k)
        hb.addWidget(self.q_x)
        hb.addStretch(1)
        lay.addLayout(hb)
        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.q_tbl = table(["Партия", "Назначение", "Трещин., %", "Битое, %", "Натура", "Всхож., %", "Проходов",
                            "Макс. съём", "T блоков", "Макс. T зерна"])
        lv.addWidget(self.q_tbl)
        self.q_text = QLabel()
        self.q_text.setWordWrap(True)
        lv.addWidget(self.q_text)
        split.addWidget(left)
        self.q_plot = SimplePlot()
        split.addWidget(self.q_plot)
        split.setSizes([700, 560])
        lay.addWidget(split, 1)
        self.q_x.currentIndexChanged.connect(self.recalc_quality)
        self.q_y.currentIndexChanged.connect(self.recalc_quality)
        return w

    def recalc_quality(self) -> None:
        mute(self.q_hint)
        tab = quality_table(self._batches(), self.win.journal_rows(), self.win.params)
        self.q_tbl.setRowCount(len(tab))
        for i, r in enumerate(tab):
            cells = [r["name"], r["purpose"], fnum(r["cracks"]), fnum(r["broken"]), fnum(r["test_weight"], 0),
                     fnum(r["germ"], 0), fnum(r["passes"], 0), fnum(r["max_dw"]), fnum(r["t_mean"], 0),
                     fnum(r["tg_max"], 0)]
            for j, txt in enumerate(cells):
                self.q_tbl.setItem(i, j, item(txt, align_right=j >= 2))
        xk, yk = self.q_x.currentData(), self.q_y.currentData()
        pts = [(r[xk], r[yk]) for r in tab if r[xk] is not None and r[yk] is not None]
        self.q_plot.clear()
        self.q_plot.set_labels(REGIME_NAMES[xk], QUALITY_NAMES[yk])
        if pts:
            self.q_plot.add_points([a for a, _ in pts], [b for _, b in pts], "dot", 9)
            res = correlation([a for a, _ in pts], [b for _, b in pts])
            if res:
                r, slope, n = res
                mx = sum(a for a, _ in pts) / n
                my = sum(b for _, b in pts) / n
                xs = [min(a for a, _ in pts), max(a for a, _ in pts)]
                self.q_plot.add_line(xs, [my + slope * (x - mx) for x in xs], "alt", 1.5, dashed=True)
                self.q_plot.set_title(f"r = {r:+.2f}, партий {n}")
        findings = quality_findings(tab)
        filled = sum(1 for r in tab if any(r[k] is not None for k in QUALITY_NAMES))
        self.q_text.setText(("Партий с данными о качестве: %d.<br>" % filled) + "<br>".join(findings))
        self.q_text.setTextFormat(Qt.RichText)

    # ==================================================================
    # Очередь и смешивание
    # ==================================================================
    def _build_queue(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        left = QWidget()
        lv = QVBoxLayout(left)
        gb = QGroupBox("Смешивание партий (в одном силосе)")
        gv = QVBoxLayout(gb)
        self.tbl_mix = table(["Масса, т", "W, %"], stretch_last=True)
        self.tbl_mix.setRowCount(5)
        for i in range(5):
            for j in range(2):
                self.tbl_mix.setItem(i, j, QTableWidgetItem(""))
        self.tbl_mix.setMaximumHeight(190)
        self.tbl_mix.itemChanged.connect(self._mix_changed)
        gv.addWidget(self.tbl_mix)
        self.lbl_mix = QLabel("Введите массы и влажности частей.")
        self.lbl_mix.setWordWrap(True)
        gv.addWidget(self.lbl_mix)
        hb = QHBoxLayout()
        self.mix_name = QLineEdit("Смесь")
        b = QPushButton("Добавить смесь в очередь")
        b.clicked.connect(self._mix_to_queue)
        hb.addWidget(self.mix_name)
        hb.addWidget(b)
        gv.addLayout(hb)
        lv.addWidget(gb)

        gb = QGroupBox("Как считать очередь")
        f = QFormLayout(gb)
        self.q_start = QDateTimeEdit(QDateTime.currentDateTime())
        self.q_start.setCalendarPopup(True)
        self.q_start.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.q_method = QComboBox()
        self.q_method.addItem("По модели (рекомендованный сценарий)", "model")
        self.q_method.addItem("По производительности (Q и лимит съёма)", "q")
        self.q_q = dspin(1, 200, 18.2, 1, 0.5, " т/ч")
        self.q_lim = dspin(1, 25, 6, 1, 0.5, " п.п.")
        self.q_change = dspin(0, 48, 0, 1, 0.5, " ч")
        self.q_cool = QCheckBox("Финальное охлаждение на D = 100")
        self.q_cool.setChecked(True)
        f.addRow("Сушилка свободна с", self.q_start)
        f.addRow("Метод", self.q_method)
        f.addRow("Q (для метода по производительности)", self.q_q)
        f.addRow("Лимит съёма за проход", self.q_lim)
        f.addRow("Переналадка между партиями", self.q_change)
        f.addRow(self.q_cool)
        lv.addWidget(gb)
        self.lbl_q_note = QLabel("Условия (T блоков, наружный воздух) и лимиты съёма для метода по модели берутся "
                                 "из раздела «План сушки партии».")
        self.lbl_q_note.setWordWrap(True)
        lv.addWidget(self.lbl_q_note)
        lv.addStretch(1)
        left.setMaximumWidth(430)

        right = QSplitter(Qt.Vertical)
        top = QWidget()
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        for text, slot in (("Добавить партию", self._q_add), ("Удалить", self._q_del), ("Выше", lambda: self._q_move(-1)),
                           ("Ниже", lambda: self._q_move(1))):
            b = QPushButton(text)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch(1)
        tv.addLayout(bar)
        self.tbl_q = QTableWidget(0, len(QUEUE_FIELDS))
        self.tbl_q.setHorizontalHeaderLabels(QUEUE_HEADERS)
        self.tbl_q.horizontalHeader().setStretchLastSection(True)
        self.tbl_q.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_q.itemChanged.connect(self._q_edited)
        tv.addWidget(self.tbl_q)
        right.addWidget(top)
        self.tbl_qr = table(["Партия", "Начало", "Окончание", "Длительность", "Проходов", "Съём макс., п.п.",
                             "Ожидание, ч", "Примечание"])
        right.addWidget(self.tbl_qr)
        self.gantt = GanttChart()
        right.addWidget(self.gantt)
        right.setSizes([220, 200, 240])
        lay.addWidget(left, 0)
        lay.addWidget(right, 1)
        for wd in (self.q_q, self.q_lim, self.q_change):
            wd.valueChanged.connect(self.recalc_queue)
        self.q_start.dateTimeChanged.connect(self.recalc_queue)
        self.q_method.currentIndexChanged.connect(self.recalc_queue)
        self.q_cool.stateChanged.connect(self.recalc_queue)
        return w

    def _mix_changed(self, *_a) -> None:
        lots = []
        for i in range(self.tbl_mix.rowCount()):
            m = to_num(self.tbl_mix.item(i, 0).text() if self.tbl_mix.item(i, 0) else "")
            wv = to_num(self.tbl_mix.item(i, 1).text() if self.tbl_mix.item(i, 1) else "")
            if m and wv is not None:
                lots.append((m, wv))
        if not lots:
            self.lbl_mix.setText("Введите массы и влажности частей.")
            self._mix = None
            return
        mm, ww = mix(lots)
        self._mix = (mm, ww)
        self.lbl_mix.setText(f"Смесь: <b>{mm:.1f} т</b>, влажность <b>{ww:.2f} %</b> "
                             f"(вода складывается: W = Σ m·W / Σ m). Разброс частей "
                             f"{min(w for _, w in lots):.1f}…{max(w for _, w in lots):.1f} %: "
                             + ("сушить вместе можно." if max(w for _, w in lots) - min(w for _, w in lots) <= 6 else
                                "больше 6 п.п. — сухие зёрна пересушатся, а сырые недосохнут; лучше сушить отдельно."))
        self.lbl_mix.setTextFormat(Qt.RichText)

    def _mix_to_queue(self) -> None:
        if not getattr(self, "_mix", None):
            return
        mm, ww = self._mix
        items = self._queue()
        items.append({"name": self.mix_name.text().strip() or "Смесь", "mass": round(mm, 1), "w0": round(ww, 2),
                      "wk": self.win.in_wk.value(), "ready": ""})
        self._fill_queue(items)
        self._save_queue()
        self.recalc_queue()

    def _queue(self) -> List[Dict]:
        out = []
        for i in range(self.tbl_q.rowCount()):
            d: Dict = {}
            for j, f in enumerate(QUEUE_FIELDS):
                it = self.tbl_q.item(i, j)
                txt = it.text().strip() if it else ""
                d[f] = txt if f in ("name", "ready") else to_num(txt)
            out.append(d)
        return out

    def _fill_queue(self, items: List[Dict]) -> None:
        self._loading = True
        try:
            self.tbl_q.setRowCount(len(items))
            for i, d in enumerate(items):
                for j, f in enumerate(QUEUE_FIELDS):
                    v = d.get(f)
                    txt = "" if v is None else (str(v) if f in ("name", "ready") else f"{v:g}")
                    self.tbl_q.setItem(i, j, QTableWidgetItem(txt))
            self.tbl_q.resizeColumnsToContents()
        finally:
            self._loading = False

    def _save_queue(self) -> None:
        self.win.storage.set_list("queue", self._queue())
        self.win.storage.set_json("ui_queue", {"method": self.q_method.currentData(), "q": self.q_q.value(),
                                               "lim": self.q_lim.value(), "change": self.q_change.value(),
                                               "cool": self.q_cool.isChecked()})

    def _q_edited(self, *_a) -> None:
        if self._loading:
            return
        self._save_queue()
        self.recalc_queue()

    def _q_add(self) -> None:
        items = self._queue()
        items.append({"name": f"Партия {len(items) + 1}", "mass": self.win.in_mass.value(), "w0": self.win.in_w0.value(),
                      "wk": self.win.in_wk.value(), "ready": "", "passes": self.win.plan_passes()})
        self._fill_queue(items)
        self._save_queue()
        self.recalc_queue()

    def _q_del(self) -> None:
        rows = sorted({i.row() for i in self.tbl_q.selectedIndexes()}, reverse=True)
        items = self._queue()
        for r in rows:
            del items[r]
        self._fill_queue(items)
        self._save_queue()
        self.recalc_queue()

    def _q_move(self, delta: int) -> None:
        r = self.tbl_q.currentRow()
        items = self._queue()
        j = r + delta
        if r < 0 or not (0 <= j < len(items)):
            return
        items[r], items[j] = items[j], items[r]
        self._fill_queue(items)
        self._save_queue()
        self.tbl_q.selectRow(j)
        self.recalc_queue()

    def queue_schedule(self) -> List[QueueEntry]:
        """Очередь партий по текущим настройкам раздела (как в таблице «Очередь»)."""
        soft, hard = self.win.limits()
        return schedule(self._queue(), self.win.params, self.win.conditions(), self.q_start.dateTime().toPython(),
                        method=self.q_method.currentData(), soft=soft, hard=hard, q=self.q_q.value(),
                        lim=self.q_lim.value(), cool=self.q_cool.isChecked(), changeover_h=self.q_change.value())

    def queue_end(self) -> Optional[datetime]:
        """Когда сушилка освободится после всей очереди (None — очередь пуста или не считается)."""
        ends = [e.end for e in self.queue_schedule() if e.end]
        return max(ends) if ends else None

    def add_to_queue(self, name: str, mass: float, w0: float, wk: float, ready: Optional[datetime],
                     passes: Optional[int] = None) -> None:
        items = self._queue()
        items.append({"name": name, "mass": round(mass, 1), "w0": w0, "wk": wk,
                      "ready": f"{ready:%d.%m.%Y %H:%M}" if ready else "", "passes": passes})
        self._fill_queue(items)
        self._save_queue()
        self.recalc_queue()

    def recalc_queue(self) -> None:
        mute(self.lbl_q_note)
        self.q_q.setEnabled(self.q_method.currentData() == "q")
        self.q_lim.setEnabled(self.q_method.currentData() == "q")
        if self._loading:
            return
        entries = self.queue_schedule()
        self.tbl_qr.setRowCount(len(entries))
        for i, e in enumerate(entries):
            if e.plan is None:
                cells = [e.name, "—", "—", "—", "—", "—", "—", e.note]
            else:
                cells = [e.name, f"{e.start:%d.%m %H:%M}", f"{e.end:%d.%m %H:%M}", fmt_hours(e.plan.total_time_h),
                         str(e.passes), f"{e.max_dw:.1f}", f"{e.wait_h:.1f}" if e.wait_h > 0.05 else "—", e.note]
            for j, txt in enumerate(cells):
                self.tbl_qr.setItem(i, j, item(txt, align_right=j in (4, 5, 6)))
        self.gantt.set_entries(entries)
        self._save_queue()

    # ==================================================================
    def _load(self) -> None:
        self._fill_batches(self.win.storage.get_list("batches"))
        self._fill_queue(self.win.storage.get_list("queue"))
        d = self.win.storage.get_json("ui_queue", {}) or {}
        if d.get("method") == "q":
            self.q_method.setCurrentIndex(1)
        for k, wd in (("q", self.q_q), ("lim", self.q_lim), ("change", self.q_change)):
            if k in d:
                wd.setValue(float(d[k]))
        if "cool" in d:
            self.q_cool.setChecked(bool(d["cool"]))

    def on_show(self) -> None:
        i = self.sub.currentIndex() if hasattr(self, "sub") else 0
        if i == 0:
            if self.tbl_b.rowCount() and not self.tbl_b.selectionModel().selectedRows():
                self.tbl_b.selectRow(self.tbl_b.rowCount() - 1)
            self._show_batch()
        elif i == 1:
            self.recalc_quality()
        else:
            self.recalc_queue()


def _n(v) -> Optional[float]:
    return to_num(v)


def _safe(name) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(name or "партия"))[:40]


def _open_hint(parent: QWidget, path: str) -> None:
    QMessageBox.information(parent, "Готово", f"Файл сохранён:\n{path}")
