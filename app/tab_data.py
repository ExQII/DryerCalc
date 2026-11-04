"""Вкладка «Погода и влагомер»: наружная T/RH без интернета и поправка выходного влагомера по парам проб."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Dict, List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from .analysis import batch_meter_points, combine_meter, meter_bias
from .plots import SimplePlot
from .theme import theme_for
from .ui_common import mute, set_role, size_by_current, to_num, with_legend
from .weather import WeatherSeries, merge_points, parse_dt, read_weather

if TYPE_CHECKING:
    from .gui import MainWindow

W_HEADERS = ["Дата-время (дд.мм.гггг чч:мм)", "T наружная, °C", "RH наружная, %", "Источник"]
M_HEADERS = ["Дата-время", "Выходной влагомер сушилки, %", "Та же проба после охлаждения / лаборатория, %",
             "T зерна по датчикам сушилки, °C (необяз.)", "Разница, п.п.", "Примечание"]
MB_HEADERS = ["Партия", "Проб выхода", "Выгружено, т", "Влагомер (средн.), %", "Лаборатория, %", "Разница, п.п.",
              "T зерна, °C", "Замечание"]


class DataTab(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self._loading = False
        tabs = QTabWidget()
        QVBoxLayout(self).addWidget(tabs)
        tabs.addTab(self._build_weather(), "Погода (наружный воздух)")
        tabs.addTab(self._build_meter(), "Поправка выходного влагомера")
        size_by_current(tabs)
        self._load()

    # ==================================================================
    # Погода
    # ==================================================================
    def _build_weather(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.w_hint = QLabel(
            "Интернет не нужен. Источники: (1) замеры у сушилки термометром-гигрометром — кнопка «Добавить замер»; "
            "(2) файл логгера температуры/влажности (CSV/XLSX); (3) архив погоды, скачанный на любом компьютере с "
            "интернетом и принесённый на флешке: rp5.ru → ближайшая метеостанция → «Архив погоды» → «Скачать» "
            "в формате CSV (любая кодировка) или open-meteo.com → Historical Weather → температура 2 м и "
            "относительная влажность 2 м, часовой пояс «auto» → Download CSV. Значения подставляются к строкам "
            "журнала по времени (интерполяция между замерами не дальше 6 ч друг от друга, иначе ближайший в "
            "пределах 3 ч) и учитываются при калибровке и в «Текущем проходе».")
        self.w_hint.setWordWrap(True)
        lay.addWidget(self.w_hint)
        bar = QHBoxLayout()
        bar.setSpacing(6)
        for text, slot in (("Импорт файла…", self._w_import), ("Добавить замер", self._w_add),
                           ("Удалить выделенные", self._w_del), ("Очистить всё", self._w_clear),
                           ("Записать в пустые T/RH журнала", self._w_fill_journal)):
            b = QPushButton(text)
            if text in ("Удалить выделенные", "Очистить всё"):
                set_role(b, "danger")
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.w_cov = QLabel()
        lay.addWidget(self.w_cov)
        split = QSplitter(Qt.Horizontal)
        self.tbl_w = QTableWidget(0, len(W_HEADERS))
        self.tbl_w.setHorizontalHeaderLabels(W_HEADERS)
        self.tbl_w.horizontalHeader().setStretchLastSection(True)
        self.tbl_w.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_w.itemChanged.connect(self._w_edited)
        split.addWidget(self.tbl_w)
        plots = QSplitter(Qt.Vertical)
        self.plot_t = SimplePlot()
        self.plot_t.set_labels("сутки от первого замера", "T наружная, °C")
        self.plot_rh = SimplePlot()
        self.plot_rh.set_labels("сутки от первого замера", "RH наружная, %")
        for pl in (self.plot_t, self.plot_rh):
            pl.set_empty_text("Замеров нет: импортируйте файл или добавьте замер")
        plots.addWidget(self.plot_t)
        plots.addWidget(self.plot_rh)
        split.addWidget(plots)
        split.setSizes([560, 700])
        lay.addWidget(split, 1)
        return w

    def _points(self) -> List[Dict]:
        out = []
        for i in range(self.tbl_w.rowCount()):
            cell = [self.tbl_w.item(i, j).text().strip() if self.tbl_w.item(i, j) else "" for j in range(4)]
            dt = parse_dt(cell[0])
            if dt is None:
                continue
            t, rh = to_num(cell[1]), to_num(cell[2])
            if t is None and rh is None:
                continue
            out.append({"dt": dt, "t": t, "rh": rh, "src": cell[3]})
        return out

    def _fill_w(self, pts: List[Dict]) -> None:
        self._loading = True
        try:
            self.tbl_w.setRowCount(len(pts))
            for i, p in enumerate(pts):
                vals = [p["dt"].strftime("%d.%m.%Y %H:%M"), "" if p.get("t") is None else f"{round(p['t'], 1):g}",
                        "" if p.get("rh") is None else f"{round(p['rh'], 0):g}", p.get("src", "")]
                for j, v in enumerate(vals):
                    self.tbl_w.setItem(i, j, QTableWidgetItem(v))
            self.tbl_w.resizeColumnsToContents()
        finally:
            self._loading = False
        self._w_plot()

    def _w_save(self) -> None:
        self.win.storage.replace_weather(self._points())
        self.win.weather_changed()
        self._w_plot()

    def _w_edited(self, *_a) -> None:
        if not self._loading:
            self._w_save()

    def _w_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Файл погоды", "", "Погода (*.csv *.txt *.gz *.xlsx *.xlsm);;Все файлы (*)")
        if not path:
            return
        try:
            pts, info = read_weather(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Импорт погоды", str(e))
            return
        if not pts:
            QMessageBox.warning(self, "Импорт погоды", "В файле не нашлось строк с датой и температурой/влажностью.")
            return
        src = path.replace("\\", "/").split("/")[-1][:40]
        for p in pts:
            p["src"] = src
        merged = merge_points(self._points(), pts)
        self._fill_w(merged)
        self._w_save()
        cols = ", ".join(f"{k} ← «{v}»" for k, v in info["columns"].items())
        QMessageBox.information(self, "Импорт погоды",
                                f"Замеров: {len(pts)} ({info['first']:%d.%m.%Y %H:%M} — {info['last']:%d.%m.%Y %H:%M}); "
                                f"пропущено строк: {info['skipped']}.\nКолонки: {cols}.\n\n"
                                f"Проверьте часовой пояс: время в файле должно быть местным, как в журнале.")

    def _w_add(self) -> None:
        pts = self._points()
        pts.append({"dt": datetime.now().replace(second=0, microsecond=0), "t": None, "rh": None, "src": "замер"})
        self._loading = True
        try:
            i = self.tbl_w.rowCount()
            self.tbl_w.insertRow(i)
            for j, v in enumerate([pts[-1]["dt"].strftime("%d.%m.%Y %H:%M"), "", "", "замер"]):
                self.tbl_w.setItem(i, j, QTableWidgetItem(v))
        finally:
            self._loading = False
        self.tbl_w.scrollToBottom()
        self.tbl_w.setCurrentCell(self.tbl_w.rowCount() - 1, 1)
        self.tbl_w.editItem(self.tbl_w.item(self.tbl_w.rowCount() - 1, 1))

    def _w_del(self) -> None:
        rows = sorted({i.row() for i in self.tbl_w.selectedIndexes()}, reverse=True)
        for r in rows:
            self.tbl_w.removeRow(r)
        if rows:
            self._w_save()

    def _w_clear(self) -> None:
        if self.tbl_w.rowCount() and QMessageBox.question(self, "Погода", "Удалить все замеры погоды?") == QMessageBox.Yes:
            self.tbl_w.setRowCount(0)
            self._w_save()

    def _w_fill_journal(self) -> None:
        ws = self.win.weather_series()
        rows = self.win.journal_rows()
        filled = ws.fill_rows(rows)
        n = sum(1 for a, b in zip(rows, filled) if a.get("t_amb") is None and b.get("t_amb") is not None)
        if not n:
            QMessageBox.information(self, "Погода → журнал", "Нечего заполнять: нет строк без T/RH рядом с замерами.")
            return
        if QMessageBox.question(self, "Погода → журнал",
                                f"Записать наружные T/RH в {n} строк журнала (где они пустые)?\n"
                                f"Для расчётов это не обязательно — подстановка идёт и так; запись нужна, чтобы "
                                f"значения были видны в журнале и в его выгрузке.") == QMessageBox.Yes:
            self.win.replace_journal_rows(filled)

    def _w_plot(self) -> None:
        mute(self.w_hint)
        pts = self._points()
        self.plot_t.clear()
        self.plot_rh.clear()
        ws = WeatherSeries(pts)
        have, total = ws.coverage(self.win.journal_rows()) if len(ws) else (0, 0)
        self.w_cov.setText(f"Замеров: {len(pts)}. Строк журнала с наружной T после подстановки: {have} из {total}."
                           if pts else "Замеров нет: в расчётах используются опорные 10 °C / 70 % или значения из журнала.")
        if not pts:
            return
        t0 = pts[0]["dt"]
        xs = [(p["dt"] - t0).total_seconds() / 86400.0 for p in pts]
        xt = [x for x, p in zip(xs, pts) if p.get("t") is not None]
        yt = [p["t"] for p in pts if p.get("t") is not None]
        xr = [x for x, p in zip(xs, pts) if p.get("rh") is not None]
        yr = [p["rh"] for p in pts if p.get("rh") is not None]
        if len(xt) > 1:
            self.plot_t.add_line(xt, yt, "line", 1.5)
        self.plot_t.add_points(xt, yt, "dot", 4)
        if len(xr) > 1:
            self.plot_rh.add_line(xr, yr, "alt", 1.5)
        self.plot_rh.add_points(xr, yr, "alt", 4)
        self.plot_t.set_title(f"с {t0:%d.%m.%Y %H:%M}")

    # ==================================================================
    # Поправка влагомера
    # ==================================================================
    def _build_meter(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.m_hint = QLabel(
            "Выходной влагомер меряет горячее зерно и обычно показывает больше, чем та же проба после охлаждения "
            "(или в лаборатории). Эта разница — «поправка влагомера на выходе» в модели: она влияет на рассчитанный "
            "съём влаги примерно на 10 %. Как набирать пары: в момент записи пробы в журнал отберите ту же пробу, "
            "дайте остыть (или отнесите в лабораторию) и запишите оба числа. Точность поправки ≈ 2·разброс/√n: при "
            "разбросе ±1 п.п. 20 пар дают ±0,45 п.п., 50 пар — ±0,3.")
        self.m_hint.setWordWrap(True)
        lay.addWidget(self.m_hint)
        bar = QHBoxLayout()
        bar.setSpacing(6)
        for text, slot in (("Добавить пару", self._m_add), ("Удалить выделенные", self._m_del),
                           ("Применить поправку в модель", self._m_apply)):
            b = QPushButton(text)
            if text == "Удалить выделенные":
                set_role(b, "danger")
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.m_stat = QLabel()
        self.m_stat.setWordWrap(True)
        self.m_stat.setTextFormat(Qt.RichText)
        lay.addWidget(self.m_stat)
        split = QSplitter(Qt.Horizontal)
        left = QSplitter(Qt.Vertical)
        self.tbl_m = QTableWidget(0, len(M_HEADERS))
        self.tbl_m.setHorizontalHeaderLabels(M_HEADERS)
        self.tbl_m.horizontalHeader().setStretchLastSection(True)
        self.tbl_m.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_m.itemChanged.connect(self._m_edited)
        left.addWidget(self.tbl_m)
        bw = QWidget()
        bl = QVBoxLayout(bw)
        bl.setContentsMargins(0, 0, 0, 0)
        self.cb_mb = QCheckBox("Учитывать партии (лабораторная W готовой партии)")
        self.cb_mb.setToolTip("Второй источник поправки, кроме пар: средний по массе выходной влагомер последнего "
                              "прохода партии против лабораторной W после охлаждения (раздел «Партии и очередь»).")
        self.cb_mb.setChecked(True)
        self.cb_mb.stateChanged.connect(lambda _s: self._m_stats())
        bl.addWidget(self.cb_mb)
        self.tbl_mb = QTableWidget(0, len(MB_HEADERS))
        self.tbl_mb.setHorizontalHeaderLabels(MB_HEADERS)
        self.tbl_mb.horizontalHeader().setStretchLastSection(True)
        self.tbl_mb.setEditTriggers(QAbstractItemView.NoEditTriggers)
        bl.addWidget(self.tbl_mb)
        left.addWidget(bw)
        left.setSizes([330, 200])
        split.addWidget(left)
        self.plot_m = SimplePlot()
        self.plot_m.set_labels("показание выходного влагомера, %", "показание − после охлаждения, п.п.")
        split.addWidget(with_legend(self.plot_m, (("dot", "dot", "пары проб"), ("dot", "alt", "партии"),
                                                  ("line", "line", "рекомендуемая поправка"),
                                                  ("dash", "target", "сейчас в модели"))))
        split.setSizes([700, 560])
        lay.addWidget(split, 1)
        return w

    def _pairs(self) -> List[Dict]:
        out = []
        for i in range(self.tbl_m.rowCount()):
            cell = [self.tbl_m.item(i, j).text().strip() if self.tbl_m.item(i, j) else "" for j in range(6)]
            out.append({"dt": cell[0], "reading": to_num(cell[1]), "ref": to_num(cell[2]), "t": to_num(cell[3]),
                        "note": cell[5]})
        return out

    def _fill_m(self, pairs: List[Dict]) -> None:
        self._loading = True
        try:
            self.tbl_m.setRowCount(len(pairs))
            for i, p in enumerate(pairs):
                diff = (p["reading"] - p["ref"]) if p.get("reading") is not None and p.get("ref") is not None else None
                vals = [p.get("dt", ""), "" if p.get("reading") is None else f"{p['reading']:g}",
                        "" if p.get("ref") is None else f"{p['ref']:g}", "" if p.get("t") is None else f"{p['t']:g}",
                        "" if diff is None else f"{diff:+.1f}", p.get("note", "")]
                for j, v in enumerate(vals):
                    it = QTableWidgetItem(v)
                    if j == 4:
                        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                    self.tbl_m.setItem(i, j, it)
            self.tbl_m.resizeColumnsToContents()
        finally:
            self._loading = False
        self._m_stats()

    def _m_edited(self, *_a) -> None:
        if self._loading:
            return
        pairs = self._pairs()
        self.win.storage.set_list("meter_pairs", pairs)
        self._fill_m(pairs)

    def _m_add(self) -> None:
        pairs = self._pairs()
        pairs.append({"dt": datetime.now().strftime("%d.%m.%Y %H:%M"), "reading": None, "ref": None, "note": ""})
        self._fill_m(pairs)
        self.win.storage.set_list("meter_pairs", pairs)
        self.tbl_m.setCurrentCell(len(pairs) - 1, 1)
        self.tbl_m.editItem(self.tbl_m.item(len(pairs) - 1, 1))

    def _m_del(self) -> None:
        rows = sorted({i.row() for i in self.tbl_m.selectedIndexes()}, reverse=True)
        pairs = self._pairs()
        for r in rows:
            del pairs[r]
        self._fill_m(pairs)
        self.win.storage.set_list("meter_pairs", pairs)

    def _m_stats(self) -> None:
        th = theme_for(self)
        mute(self.m_hint)
        r = meter_bias(self._pairs())
        p = self.win.params
        points = batch_meter_points(self.win.storage.get_list("batches"), self.win.journal_rows(), p)
        comb = combine_meter(r, points, self.cb_mb.isChecked())
        self._fill_mb(points)
        cur = p.out_bias
        txt = r["text"]
        if points and self.cb_mb.isChecked() and comb["n_batches"]:
            sdb = f", разброс между партиями ±{comb['sd_batches']:.2f}" if comb.get("sd_batches") is not None else \
                "; пока партий меньше трёх, разброс принят ±0,4 п.п."
            txt += (f"<br>По партиям ({comb['n_batches']}): поправка {comb['a_batches']:+.2f} п.п. "
                    f"(±{1.96 * comb['se_batches']:.2f} при 95 %{sdb}).")
            if comb["source"] == "пары и партии":
                txt += (f"<br><b>Вместе: a = {comb['a']:+.2f} ± {1.96 * comb['se']:.2f} п.п.</b> (вес партий "
                        f"{100 * comb['share_batches']:.0f} %).")
            if comb["warn"]:
                txt += f"<br><span style='color:{th.error}'>⚠ {comb['warn']}</span>"
        elif not points:
            txt += ("<br>Партий с лабораторной W после охлаждения пока нет — впишите её в разделе «Партии и очередь», "
                    "это ещё один независимый источник поправки.")
        txt += (f"<br>Сейчас в модели: <b>a = {cur:+.2f}</b>" + (f", k_w = {p.out_kw:+.3f}" if p.out_kw else "")
                + (f", k_t = {p.out_kt:+.3f}" if p.out_kt else "") + ".")
        if comb.get("a") is not None and comb.get("se") and abs(cur - comb["a"]) > 1.96 * comb["se"]:
            txt += f" <span style='color:{th.error}'>— вне 95 % интервала, стоит применить новую.</span>"
        self.m_stat.setText(txt)
        self.plot_m.clear()
        pts = [(q["reading"], q["reading"] - q["ref"]) for q in self._pairs()
               if q.get("reading") is not None and q.get("ref") is not None]
        bpts = [(x["read"], x["diff"]) for x in points if x.get("diff") is not None] if self.cb_mb.isChecked() else []
        if pts or bpts:
            if pts:
                self.plot_m.add_points([a for a, _ in pts], [b for _, b in pts], "dot", 7)
            if bpts:
                self.plot_m.add_points([a for a, _ in bpts], [b for _, b in bpts], "alt", 12)
            allx = [a for a, _ in pts + bpts]
            xs = [min(allx), max(allx)]
            if xs[1] - xs[0] < 1e-6:
                xs = [xs[0] - 1, xs[0] + 1]
            if comb.get("a") is not None:
                self.plot_m.add_line(xs, [comb["a"] + comb["kw"] * (x - 20.0) for x in xs], "line", 2)
            self.plot_m.add_line(xs, [p.out_bias + p.out_kw * (x - 20.0) for x in xs], "target", 1.5, dashed=True)
            self.plot_m.set_title("")
        self._m_result = r
        self._m_comb = comb

    def _fill_mb(self, points) -> None:
        self.tbl_mb.setRowCount(len(points))
        for i, x in enumerate(points):
            cells = [x["name"], str(x["n"]), f"{x['mass']:.0f}" if x["mass"] else "—",
                     "—" if x["read"] is None else f"{x['read']:.2f}", f"{x['lab']:.1f}",
                     "—" if x["diff"] is None else f"{x['diff']:+.2f}", "—" if x["t"] is None else f"{x['t']:.0f}",
                     x["note"]]
            for j, v in enumerate(cells):
                self.tbl_mb.setItem(i, j, QTableWidgetItem(v))
        self.tbl_mb.resizeColumnsToContents()

    def _m_apply(self) -> None:
        self._m_stats()
        r, comb = self._m_result, self._m_comb
        if comb.get("a") is None:
            QMessageBox.information(self, "Поправка влагомера", "Нет ни пар с обоими значениями, ни партий с "
                                                                 "лабораторной влажностью.")
            return
        a, kw, kt = round(comb["a"], 2), round(comb.get("kw", 0.0), 3), round(comb.get("kt", 0.0), 3)
        n_info = r.get("n", 0) + 4 * comb.get("n_batches", 0)        # партия по весу ≈ нескольким парам
        if n_info < 5 and QMessageBox.question(
                self, "Поправка влагомера", f"Данных мало (пар {r.get('n', 0)}, партий {comb.get('n_batches', 0)}) — "
                                            f"оценка ненадёжная. Всё равно применить?") != QMessageBox.Yes:
            return
        form = f"{a:+.2f}" + (f" {kw:+.3f}·(W − 20)" if kw else "") + (f" {kt:+.3f}·(T − 30)" if kt else "")
        p = self.win.params
        if QMessageBox.question(self, "Поправка влагомера",
                                f"Поставить поправку {form} п.п. (была {p.out_bias:+.2f}"
                                + (f" {p.out_kw:+.3f}·(W − 20)" if p.out_kw else "")
                                + (f" {p.out_kt:+.3f}·(T − 30)" if p.out_kt else "") + ")?\n"
                                "После этого модель нужно перекалибровать по журналу: поправка входит в расчёт "
                                "съёма влаги.") == QMessageBox.Yes:
            self.win.set_out_bias(a, kw, kt)
            self._m_stats()

    # ==================================================================
    def _load(self) -> None:
        self._fill_w(self.win.storage.load_weather())
        self._fill_m(self.win.storage.get_list("meter_pairs"))

    def on_show(self) -> None:
        self._w_plot()
        self._m_stats()
