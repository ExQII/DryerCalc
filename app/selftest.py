"""Самопроверка собранной программы: python run.py --selftest report.txt (или DryerCalc.exe --selftest report.txt).

Создаёт окно во временной базе, читает встроенный пример журнала, калибрует модель и считает партию.
Результат пишется в файл (в оконном .exe нет консоли). Код возврата 0 — всё в порядке.
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from typing import List

from . import __version__


def run(out_path: str) -> int:
    lines: List[str] = [f"DryerCalc {__version__} selftest, Python {sys.version.split()[0]}"]
    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        lines.append(("ok    " if cond else "FAIL  ") + msg)
        ok = ok and cond

    try:
        from PySide6.QtWidgets import QApplication
        import openpyxl
        from PySide6 import __version__ as pyside_v
        lines.append(f"PySide6 {pyside_v}, openpyxl {openpyxl.__version__}")

        app = QApplication.instance() or QApplication([])
        from . import model as mdl
        from .calibration import calibrate, read_journal
        from .gui import MainWindow
        from .resources import resource_path
        from .storage import Storage

        tmp = tempfile.mkdtemp(prefix="dryercalc_selftest_")
        win = MainWindow(Storage(os.path.join(tmp, "t.db")))
        check(win.tbl_sc.rowCount() >= 6, f"«План сушки партии»: сценариев {win.tbl_sc.rowCount()}")
        check(win.sm_tbl.rowCount() >= 3, f"«Быстрая оценка»: проходов {win.sm_tbl.rowCount()}")

        sample = resource_path("examples/journal_sample_oct2026.csv")
        check(sample.exists(), f"пример журнала найден: {sample.name}")
        rows, _ = read_journal(str(sample))
        check(len(rows) == 159, f"журнал прочитан: {len(rows)} строк")
        new_p, rep = calibrate(rows, mdl.DryerParams())
        check(1.5 <= new_p.r_ref <= 2.2, f"калибровка: R_ref = {new_p.r_ref:.2f} т воды/ч")
        check(rep["seg_mae"] < 0.35, f"ошибка съёма по проходам ±{rep['seg_mae']:.2f} п.п.")
        pl = mdl.plan_manual(new_p, mdl.Conditions(60, 10, 70), 500.0, 34.0, 14.0, [35], cool=True)
        check(pl.reaches_target and 50 < pl.total_time_h < 110, f"500 т, 34→14 %, D=35: {pl.total_time_h:.1f} ч")

        # --- XLSX-импорт (openpyxl внутри .exe) ---
        from datetime import datetime as _dt
        from openpyxl import Workbook
        xl = os.path.join(tmp, "j.xlsx")
        wb = Workbook()
        ws = wb.active
        ws.append(["Время", "Вход", "Выход", "Т зерна", "Т топочных блоков", "Разгрузка", "Примечание"])
        ws.append([_dt(2026, 10, 25, 8, 0), 28.4, 23.1, "26°-24°", "60-55", 35, "Запуск"])
        ws.append([_dt(2026, 10, 25, 8, 30), 28.2, 22.9, "27-25", "—", None, ""])
        wb.save(xl)
        xrows, xinfo = read_journal(xl)
        check(len(xrows) == 2 and xrows[0]["t_up"] == 60.0 and xrows[0]["t_a1"] == 26.0 and xrows[0]["d"] == 35.0,
              f"импорт XLSX: {len(xrows)} строк, колонки {sorted(xinfo['mapped'])[:4]}…")

        # --- окно целиком: импорт, калибровка с графиками, отрисовка всех вкладок ---
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
        QMessageBox.information = staticmethod(lambda *a, **k: None)
        QMessageBox.warning = staticmethod(lambda *a, **k: None)
        win._append_rows(rows)
        win._qe_prefill()
        win._calibrate()
        from . import worker as _worker
        _worker.wait_all()
        check("откалибрована" in win.banner.text(), "кнопка «Калибровать модель» отработала, баннер: " + win.banner.text()[:40])
        check(0.2 < win.params.eta < 1.5, f"физическая модель откалибрована: η = {win.params.eta:.2f}")
        win._apply_settings()

        # --- v0.4: быстрый ввод, погода, влагомер, партии, очередь, «по факту», проверка модели, отчёты, бэкап ---
        from datetime import datetime as _dt2
        n0 = win.tbl_j.rowCount()
        win.qe["w_in"].setText("29,5")
        win.qe["w_out"].setText("24,1")
        win.qe["t_amb"].setText("−4")                      # минус U+2212 — как из Word или с сайта погоды
        win._qe_add()
        check(win.tbl_j.rowCount() == n0 + 1 and win.tbl_j.item(n0, 0).text().startswith("25.10.26 07:00"),
              f"быстрый ввод: строка {win.tbl_j.item(n0, 0).text()} добавлена, время сдвинулось на 30 мин")
        from .calibration import FIELDS as _JF
        _ta = win.tbl_j.item(n0, _JF.index("t_amb")).text()
        check(_ta == "-4" and win.qe["t_amb"].text() == "-4",
              f"быстрый ввод: T наружная {_ta} °C записана и перенесена на следующую пробу")
        win.qe["t_amb"].clear()
        win.tbl_j.removeRow(n0)
        win._save_journal()
        wpath = os.path.join(tmp, "rp5.csv")
        with open(wpath, "w", encoding="cp1251") as f:
            f.write('# rp5\n"Местное время в Тест";"T";"U";\n' + "".join(
                f'"{d:02d}.10.2026 {h:02d}:00";"{5 + (h % 12) / 3:.1f}";"{70 + h % 20}";\n'
                for d in range(17, 26) for h in range(0, 24, 3)))
        from .weather import merge_points, read_weather
        pts, info = read_weather(wpath)
        win.storage.replace_weather(merge_points([], pts))
        win.weather_changed()
        win.tab_data._load()
        have, total = win.weather_series().coverage(win.journal_rows())
        check(len(pts) == 72 and have == total, f"погода rp5: {len(pts)} замеров, подставлено в {have} из {total} строк журнала")
        win.storage.set_list("meter_pairs", [{"reading": 15.0 + d, "ref": 15.0} for d in (0.6, 0.9, 0.8, 1.1, 0.7)])
        win.tab_data._load()
        check(abs(win.tab_data._m_result["mean"] - 0.82) < 1e-9, f"поправка влагомера по парам: {win.tab_data._m_result['text'][:60]}")
        _cv = win.tab_data._m_cov
        check(_cv["n_near"] == 4 and not _cv["warn"] and "у цели" in win.tab_data.m_stat.text(),
              f"покрытие пар влагомера: у цели {_cv['target']:g} % — пар {_cv['n_near']}")
        win.tab_batches._from_journal()
        check(win.tab_batches.tbl_b.rowCount() == 3, f"партии из журнала: {win.tab_batches.tbl_b.rowCount()}")
        win.tab_batches._fill_batches([dict(b, m_in=400, w_in=30, m_out=352, w_out=23.6, fuel=5000, fuel_price=7)
                                       for b in win.tab_batches._batches()])
        win.tab_batches._save_batches()
        from .reports import batch_report_data, batch_report_html, batch_report_xlsx, html_to_pdf, journal_blank_pdf
        data = batch_report_data(win.tab_batches._batches()[2], win.journal_rows(), win.params)
        html_to_pdf(batch_report_html(data), os.path.join(tmp, "b.pdf"))
        batch_report_xlsx(os.path.join(tmp, "b.xlsx"), data)
        pages = journal_blank_pdf(os.path.join(tmp, "blank.pdf"), _dt2(2026, 10, 29, 8, 0), 24)
        sizes = [os.path.getsize(os.path.join(tmp, x)) for x in ("b.pdf", "b.xlsx", "blank.pdf")]
        check(pages == 2 and min(sizes) > 3000 and len(data["journal"]["passes"]) == 1,
              f"отчёты PDF/XLSX и бланк журнала: {sizes} байт")
        win.tab_batches.tbl_q.setRowCount(0)
        win.tab_batches._fill_queue([{"name": "П1", "mass": 500, "w0": 34, "wk": 14, "ready": ""},
                                     {"name": "П2", "mass": 300, "w0": 30, "wk": 14, "ready": ""}])
        win.tab_batches.recalc_queue()
        g = win.tab_batches.gantt.entries
        check(len(g) == 2 and g[1].start >= g[0].end, f"очередь: {len(g)} партии, вторая с {g[1].start:%d.%m %H:%M}" if len(g) == 2 else "очередь не посчитана")
        win.tab_live.refresh()
        st = win.tab_live.state
        check(st is not None and st.ok and "D =" in win.tab_live.summary.text(),
              f"«Текущий проход»: проход {st.batch if st else '?'}, выгружено {st.unloaded:.0f} т" if st else "«Текущий проход» не посчитан")
        win.tab_check.refresh()
        _worker.wait_all()
        check(win.tab_check.tbl.rowCount() == 4, f"проверка модели: проходов {win.tab_check.tbl.rowCount()}")
        check(0.02 <= win.params.rate_sigma <= 0.3 and "проверка" in win.params.rate_sigma_info,
              f"допуск прогнозов по проверке: σ = {win.params.rate_sigma * 100:.1f} %")
        # --- v0.5 ---
        win.tab_check.sub.setCurrentIndex(1)
        win.tab_check.d_w.setValue(22.0)
        win.tab_check.refresh_doe()
        n_plan = win.tab_check.doe_tbl.rowCount()
        t_cell = win.tab_check.doe_tbl.item(0, 2).text() if n_plan else ""
        check(n_plan >= 1 and ("/" in t_cell or win.params.temp_source != "block"),
              f"план опытов: {n_plan} прох., первый D = "
              f"{win.tab_check.doe_tbl.item(0, 1).text() if n_plan else '—'}, T блоков {t_cell or '—'}")
        check("журнал покрывает D" in win.tab_check.doe_hint.text(), "план опытов: пределы изученного — из журнала")
        win.tab_check.sub.setCurrentIndex(0)
        win.tab_recirc._from_journal()
        win.tab_recirc.recalc()
        check("Досушка" in win.tab_recirc.summary.text(), "рециркуляция: " +
              win.tab_recirc.summary.text().split("<br>")[0].replace("<b>", "").replace("</b>", "")[:70])
        rng = getattr(win, "plan_ranges", [])
        check(bool(rng) and all(a <= b for a, b in rng), f"допуск времени в сценариях: {rng[0][0]:.0f}–{rng[0][1]:.0f} ч"
              if rng else "допуск времени не посчитан")
        from .reminders import ReminderCfg, check as rem_check
        rows_now = win.journal_rows()
        last_t = _dt2(2026, 10, 25, 6, 30)
        msgs = rem_check(rows_now, last_t + __import__("datetime").timedelta(minutes=45), {}, ReminderCfg())
        check([k for k, _, _ in msgs] == ["sample"], f"напоминание «пора внести пробу»: {len(msgs)}")
        win.tab_help.on_show()
        check("шпаргалка" in win.tab_help.manual.toPlainText().lower(), "инструкция для пользователя встроена в «Справку»")
        check("слова-ловушки" in win.tab_help.rules.toPlainText().lower(), "правила работы встроены в «Справку»")
        # --- v0.6: режим сушильщика, PIN, замечания, статистика пилота ---
        from . import pilot as _pilot
        from . import pages as _pages
        _w_eng0 = win.minimumSizeHint().width()
        win.apply_mode(_pilot.OPERATOR)
        app.processEvents()
        tb = win.tabs
        vis = [tb.tabText(i) for i in range(tb.count()) if tb.isTabVisible(i)]
        check(vis == list(_pilot.OPERATOR_TABS), "режим сушильщика: " + ", ".join(vis))
        tb.setCurrentIndex(tb.indexOfText(_pages.JOURNAL))
        app.processEvents()
        win.grab().save(os.path.join(os.path.dirname(os.path.abspath(out_path)), "selftest_operator.png"))
        _w_op = win.minimumSizeHint().width()
        win.apply_mode(_pilot.ENGINEER)
        for _ in range(5):
            app.processEvents()
        win._relayout()
        _w_eng = win.minimumSizeHint().width()
        check(_w_eng <= _w_eng0 * 1.02 + 2,
              f"после смены режима окно не раздувается: инженер {_w_eng0} → {_w_eng} px, сушильщик {_w_op} px")
        if app.platformName() != "offscreen":
            check(max(_w_op, _w_eng) <= 1366, f"окно помещается в экран 1366 px: сушильщик {_w_op}, инженер {_w_eng} px")
        else:      # без экрана Qt рисует буквы прямоугольниками-заглушками — ширина текста не настоящая
            lines.append(f"info  ширина окна без экрана (шрифт-заглушка): сушильщик {_w_op}, инженер {_w_eng} px")
        h = _pilot.pin_hash("2580")
        check(_pilot.pin_ok("2580", h) and not _pilot.pin_ok("0000", h), "PIN режима инженера (sha256)")
        win.add_feedback("самопроверка", "тестовое замечание", _pages.JOURNAL)
        st_p = _pilot.pilot_stats(win.storage.load_events(), win.journal_rows())
        check(len(st_p["feedback"]) == 1 and st_p["quick_samples"] >= 1 and st_p["modes"],
              f"пилот: замечаний {len(st_p['feedback'])}, быстрых проб {st_p['quick_samples']}")
        xl_p = os.path.join(tmp, "pilot.xlsx")
        _pilot.export_xlsx(xl_p, st_p, win.storage.load_events())
        check(os.path.getsize(xl_p) > 3000, f"отчёт пилота Excel: {os.path.getsize(xl_p)} байт")
        # --- v0.7: numpy и pyqtgraph внутри exe, расчёт по массивам, журнал по порядку, слова-ловушки, графики ---
        import numpy as _np
        import pyqtgraph as _pg
        from .model import Conditions as _C, solve_out_arr as _soa
        _wo, _q = _soa(new_p, [30.0, 24.0], [35.0, 50.0], 60.0, 10.0, 70.0)
        _ws = [mdl.solve_pass(new_p, _C(60.0, 10.0, 70.0), w, 1.0, d).w_out for w, d in ((30.0, 35.0), (24.0, 50.0))]
        check(max(abs(a - b) for a, b in zip(_wo, _ws)) < 1e-9,
              f"numpy {_np.__version__}, pyqtgraph {_pg.__version__}: расчёт по массивам совпадает со скалярным")
        from .calibration import note_trap as _trap
        check(bool(_trap("зерно на выходе хорошо охлаждено")) and _trap("Остановка: силос полный") is None
              and _trap("Сушим в 1 силос") is None and _trap("Закончился 2 силос, рециркуляция") is None,
              "слова-ловушки в примечании распознаются, обычные записи о силосах — нет")
        from PySide6.QtCore import QDateTime as _QDT
        _asked9: list = []                        # окно «Проверьте пробу» в самопроверке не показываем
        win._confirm_sample = lambda _d: _asked9.append(_d) or True
        n_before = win.tbl_j.rowCount()
        last_ts = win.tbl_j.item(n_before - 1, 0).text()
        mid_ts = win.tbl_j.item(n_before // 2, 0).text()
        mid = _QDT.fromString(mid_ts, "dd.MM.yy HH:mm").addSecs(5 * 60)
        win.qe_time.setDateTime(mid)
        win.qe["w_in"].setText("30,1")
        win.qe["w_out"].setText("25,2")
        win.qe["note"].setText("")
        win._qe_add()
        pos = next((i for i in range(win.tbl_j.rowCount()) if win.tbl_j.item(i, 0).text() == mid.toString("dd.MM.yy HH:mm")), -1)
        check(win.tbl_j.rowCount() == n_before + 1 and 0 < pos < n_before and win.tbl_j.item(n_before, 0).text() == last_ts
              and not win.order_bar.isVisibleTo(win),
              f"забытая проба {mid.toString('HH:mm')} встала на своё место (строка {pos + 1} из {n_before + 1})")
        win.tbl_j.removeRow(pos)
        win._save_journal()
        pl = win.plot
        pl.show_all()
        app.processEvents()
        (x0, x1), _ = pl.vb.viewRange()
        pl.vb.scaleBy((0.5, 0.5))
        app.processEvents()
        (z0, z1), _ = pl.vb.viewRange()
        pl.show_all()
        app.processEvents()
        (r0, r1), _ = pl.vb.viewRange()
        check(z1 - z0 < 0.6 * (x1 - x0) and abs((r1 - r0) - (x1 - x0)) < 1e-6 * max(1.0, x1 - x0),
              "интерактивный график: масштаб и «показать всё»")
        # --- v0.8: честные допуски, средняя влажность приёмного силоса, поправка влагомера по партиям ---
        win.recalc()
        tips = [win.tbl_sc.item(i, 3).toolTip() for i in range(win.tbl_sc.rowCount())]
        sig1 = win.plan_sigmas[0][0] if getattr(win, "plan_sigmas", None) else 0.0
        check(bool(tips) and all("Допуск 90 %" in t for t in tips) and bool(win.params.unc_cov)
              and sig1 > win.params.rate_sigma,
              f"честные допуски: 1 проход ±{100 * mdl.Z90 * sig1:.0f} % скорости (проверка ±"
              f"{100 * mdl.Z90 * win.params.rate_sigma:.0f} %)")
        win.tab_live.cb_silo.setChecked(True)
        win.tab_live.refresh()
        _fc = getattr(win.tab_live, "fc", None)
        check(_fc is not None and _fc.silo is not None and win.tab_live.state.silo_w is not None
              and "силос" in win.tab_live.summary.text(),
              f"средняя влажность приёмного силоса: {win.tab_live.state.silo_w:.1f} %" if _fc and _fc.silo else
              "средняя влажность силоса не посчитана")
        win.tab_data._m_stats()
        _cm = win.tab_data._m_comb
        check(_cm.get("n_batches", 0) >= 1 and _cm.get("a") is not None and win.tab_data.tbl_mb.rowCount() >= 1,
              f"поправка влагомера по партиям: партий {_cm.get('n_batches', 0)}, итог a = {_cm.get('a', 0):+.2f}")
        # --- v0.8.1: модель прошлой версии, быстрый ввод следует за журналом, смена режима отдельной строкой ---
        from dataclasses import replace as _rp
        from datetime import timedelta as _td
        from . import worker as _wk
        from .calibration import to_float as _tf
        from .regime import plan_regime_change as _plan, row_time as _rt
        _full = win.params
        win.set_params(_rp(_full, unc_feats=None, unc_cov=None, unc_lev=0.0, explored=None))
        win._upgrade_model()
        _wk.wait_all()
        for _ in range(20):
            app.processEvents()
        _np = win.params
        check(bool(_np.unc_cov) and (_np.r_ref, _np.b_w, _np.b_dt, _np.eta) == (_full.r_ref, _full.b_w, _full.b_dt, _full.eta),
              "модель прошлой версии: коэффициенты те же, допуски дополнены по журналу без перекалибровки"
              f" (±{100 * _np.unc_lev ** 0.5:.1f} %, при калибровке ±{100 * (_full.unc_lev or 0) ** 0.5:.1f} %)"
              if _np.unc_cov else f"допуски не дополнены: calibrated={_np.calibrated}, {_np.calib_info}")
        _last = win.tbl_j.rowCount() - 1
        _d_cell = win.tbl_j.item(_last, 7).text()
        _d0 = _tf(win.qe["d"].text())
        win._set_cell(_last, "d", _d0 + 5)
        for _ in range(5):
            app.processEvents()
        win._qe_sync_timer.stop()
        win._qe_prefill()
        check(_tf(win.qe["d"].text()) == _d0 + 5, f"быстрый ввод следует за журналом: D {_d0:g} → {win.qe['d'].text()}")
        _rows = win._table_to_rows()
        _t0 = _rt(_rows[-1])
        _pl = _plan(_rows, _t0 + _td(minutes=5), {"d": _d0 + 10})
        win._apply_regime(_pl, False)
        _rows2 = win._table_to_rows()
        check(len(_rows2) == len(_rows) + 1 and _rows2[-1]["note"].startswith("Режим: D") and _rows2[-2]["d"] == _d0 + 5
              and _tf(win.qe["d"].text()) == _d0 + 10 and win.qe_time.dateTime().toPython() == _t0 + _td(minutes=30),
              f"смена режима отдельной строкой: «{_rows2[-1]['note']}», следующая проба "
              f"{win.qe_time.dateTime().toString('HH:mm')}")
        win.tbl_j.removeRow(win.tbl_j.rowCount() - 1)
        win.tbl_j.item(_last, 7).setText(_d_cell)
        win._save_journal()
        win._qe_prefill(after_add=True)
        # --- v0.9: боковое меню, названия по смыслу работы, «Текущий проход» с одним действием ---
        _nav = win.tabs
        check([_nav.tabText(i) for i in range(_nav.count())] == [x[0] for x in _pages.PAGES] and _nav.list.count() > 10,
              "боковое меню: " + ", ".join(_nav.tabText(i) for i in range(_nav.count())))
        _nav.setCurrentIndex(_nav.indexOfText(_pages.LIVE))
        app.processEvents()
        win.tab_live.refresh()
        _adv = win.tab_live.advice or {}
        check(bool(_adv.get("head")) and win.tab_live.lbl_head.text().replace("\u00a0", " ") == _adv["head"]
              and win.tab_live.t_pass.lbl_value.text() not in ("", "—") and _nav.lbl_title.text() == _pages.LIVE
              and win.tab_live.dial.state()[0] != "nodata" and win.tab_live.tdial.state()[0] != "nodata"
              and len(win.tab_live.tiles) == 4,
              f"«{_pages.LIVE}»: «{_adv.get('head', '?')}», приборы D {win.tab_live.dial.state()[2]} с и T блоков "
              f"{win.tab_live.tdial.text()} °C, конец прохода {win.tab_live.t_pass.lbl_value.text()}")
        from .live import outlet_soon as _soon
        _fc = win.tab_live.fc
        _txt = _soon(win.params, win.tab_live.state, _fc) if _fc is not None else ""
        check(_fc is not None and _fc.transit is not None and bool(_txt),
              f"прогноз выхода по зерну в шахте (все режимы за его путь): {_txt or 'не посчитан'}")
        # --- v1.1: проход делит рециркуляция, а не переключение силоса; рециркуляция у цели — досушка ---
        from datetime import datetime
        from .calibration import journal_passes as _jp, prepare as _prep
        from .live import live_state
        _t0 = datetime(2026, 11, 2, 8, 0)

        def _r(h, wi=None, wo=None, note=""):
            return {"ts": (_t0 + _td(hours=h)).strftime("%d.%m.%y %H:%M"), "w_in": wi, "w_out": wo,
                    "t_a1": 30 if wo else None, "t_a2": 34 if wo else None, "t_up": 60, "t_low": 60, "d": 35,
                    "note": note, "batch": "П-1", "pass_no": 1}
        _rows = ([_r(0, note="Запуск, сушим с 2 с. в 5 с.")] + [_r(0.5 * k, 26.0, 21.8) for k in range(1, 21)]
                 + [_r(10.2, note="Закончился 2 с., сушим с 4 с.")] + [_r(0.5 * k, 26.5, 21.6) for k in range(21, 41)]
                 + [_r(20.2, note="Закончился 4 с., рециркуляция"), _r(20.5, 21.0, 20.2),
                    _r(21.4, note="сушим с 5 с. в 2 с.")] + [_r(0.5 * k, 21.0, 14.7) for k in range(44, 80)]
                 + [_r(40.2, note="Закончился 5 с., рециркуляция"), _r(40.5, 14.6, 14.2)])
        _ps = _jp(_prep(_rows)[0], win.params, 15.0)
        _st = live_state(_rows[:40], win.params, final_w=15.0)
        check([(x["pass_no"], x["batch_end"]) for x in _ps] == [(1, False), (2, True)]
              and abs(_st.unloaded - mdl.q_at(win.params, 35) * 19.0) < 1e-6,
              f"проходы по рециркуляции: {len(_ps)} (переключение силоса — один проход, выгружено "
              f"{_st.unloaded:.0f} т); рециркуляция при {_ps[-1]['w_end']:.1f} % — досушка и конец партии")
        # --- v1.1.1: запуск с сохранённой целью партии Wк; статистика пилота при одной пробе в проходе ---
        _errs: List[str] = []
        _hook = sys.excepthook
        sys.excepthook = lambda t, e, tb: _errs.append(f"{t.__name__}: {e}")
        try:
            _st2 = Storage(os.path.join(tmp, "saved.db"))
            _st2.set_json("ui_live", {"m_pass": 900.0, "w_target": 15.5, "wk": 13.5, "t_amb": 4.0, "rh": 75.0})
            _win2 = MainWindow(_st2)
            _win2.tab_live.refresh()
            _win2.tab_live.wk_final.setValue(14.0)
            app.processEvents()
            _win2.close()       # окно и его база остаются до конца проверки: смена темы ниже дойдёт и до него
        finally:
            sys.excepthook = _hook
        _cov = {"expected": 1, "actual": 1, "intervals": [], "coverage": 1.0, "late_share": None,
                "median_interval": None, "passes": 1}
        _ptxt = _pilot.stats_text({"start": _t0, "end": _t0, "days": [_t0.date()], "modes": {}, "coverage": _cov,
                                   "quick_samples": 1, "reminders": {}, "reaction_median": None, "reactions": 0,
                                   "feedback": []})
        check(not _errs and "пока нет" in _ptxt,
              "запуск с сохранённой целью партии Wк и статистика пилота при одной пробе — без ошибок"
              if not _errs else f"ошибка при запуске с сохранёнными настройками: {_errs[0]}")
        # --- v1.2: масса на выходные — партия ровно заполняет окно до выхода хозяйства ---
        from .weekend import WeekendInput as _WI, allowable_storage_days as _asd, weekend_mass as _wm
        _wr = _wm(win.params, win.conditions(), _WI(_t0, _t0 + _td(hours=62), 28.0, 14.0))
        win.tabs.setCurrentIndex(win.tabs.indexOfText(_pages.WEEKEND))
        app.processEvents()
        _wres = win.tab_weekend.result
        check(_wr.ok and abs(_wr.plan.total_time_h - 62.0) < 0.02 and _wr.mass_lo is not None
              and _wr.mass_lo < _wr.mass < _wr.mass_hi and _wres is not None and _wres.ok
              and abs(_asd(25.0, (60 - 32) * 5 / 9)[0] - 10.0) < 1e-6,
              f"масса на выходные: окно 62 ч, 28 → 14 % — {_wr.mass:.0f} т (90 %: {_wr.mass_lo or 0:.0f}–"
              f"{_wr.mass_hi or 0:.0f} т), {_wr.n_passes} прох.; раздел открыт: {_wres.mass if _wres else 0:.0f} т")
        # --- v1.3: число проходов задаётся вручную; поправка влагомера с фото журнала октября убирается ---
        _w0s, _wks = win.in_w0.value(), win.in_wk.value()
        win.in_w0.setValue(35.0)
        win.in_wk.setValue(14.0)
        win.in_npass.setValue(3)
        app.processEvents()
        _star = [i for i in range(win.tbl_sc.rowCount()) if "★" in win.tbl_sc.item(i, 0).text()]
        _np_ok = len(_star) == 1 and len(win.plans[_star[0]].passes) == 3 and "задано" in win.status.text()
        win.in_npass.setValue(0)
        win.in_w0.setValue(_w0s)
        win.in_wk.setValue(_wks)
        check(_np_ok, "число проходов вручную: с 35 % в 3 прохода — ★ этот сценарий (съём по 7 п.п.)")
        from dataclasses import replace as _rp2
        _st3 = Storage(os.path.join(tmp, "photo.db"))
        _st3.replace_journal(rows)
        _st3.save_params(_rp2(mdl.DryerParams(), out_bias=0.8, r_ref=1.83, b_w=0.69, b_dt=0.87, eta=0.52,
                              calib_info="04.11.2026, строк 120, ошибка w_вых ±0.44 п.п."))
        _win3 = MainWindow(_st3)
        _r_old = _win3.params.r_ref
        _win3._startup_model()
        _wk.wait_all()
        for _ in range(30):
            app.processEvents()
        _p3 = _win3.params
        check(_p3.out_bias == 0.0 and _p3.r_ref < 0.95 * _r_old and "фото" in _p3.calib_info
              and not _win3._has_photo_bias(),
              f"поправка влагомера +0,8 с фото убрана, модель перекалибрована: R₀ {_r_old:.2f} → {_p3.r_ref:.2f}")
        _win3.close()
        # --- v1.4: плитки «Текущего прохода» в реальном времени; масса на входе прохода — сама ---
        from .live import pass_mass as _pm
        _now = datetime.now().replace(second=0, microsecond=0)
        _rows4 = _rows[:-2]                                  # без рециркуляции в конце: проход 2 идёт
        _sh = _now - _td(minutes=10) - datetime.strptime(_rows4[-1]["ts"], "%d.%m.%y %H:%M")
        _rows4 = [dict(r, ts=(datetime.strptime(r["ts"], "%d.%m.%y %H:%M") + _sh).strftime("%d.%m.%y %H:%M"))
                  for r in _rows4]
        _st4 = Storage(os.path.join(tmp, "realtime.db"))
        _st4.replace_journal(_rows4)
        _st4.set_json("ui", {"mass": 800.0, "w0": 27.0, "wk": 14.0})
        _win4 = MainWindow(_st4)
        _win4.show()
        _win4.tabs.setCurrentIndex(_win4.tabs.indexOfText(_pages.LIVE))
        for _ in range(5):
            app.processEvents()
        _lt4 = _win4.tab_live
        _s4 = _lt4.state
        _m4 = _pm(800.0, 27.0, _s4.batch_passes)[0] if _s4 is not None else None
        check(_s4 is not None and _s4.end is not None and _s4.end > _s4.last and _m4 is not None
              and abs(_lt4.m_pass.value() - _m4) <= 0.5 and "осталось" in _lt4.t_pass.lbl_sub.text(),
              f"«Текущий проход» в реальном времени: выгружено {(_s4.unloaded if _s4 else 0):.0f} т на "
              f"{(_s4.end if _s4 and _s4.end else _now):%H:%M}; масса прохода {(_m4 or 0):.0f} т (партия 800 т)")
        _win4.close()
        # --- v1.4.1: проход закончен только по «рециркуляции» в журнале, а не по расчётной массе ---
        from .live import live_advice as _la, live_forecast as _lf
        _cut = [r for r in _rows if datetime.strptime(r["ts"], "%d.%m.%y %H:%M") < _t0 + _td(hours=19.6)]
        _s5 = live_state(_cut, win.params, final_w=15.0)
        _c5 = mdl.Conditions(_s5.t_proc, 10.0, 70.0)
        _f5 = _lf(win.params, _c5, _s5, 200.0, 21.5, 14.0, 6.0, 10.0, silo=True)
        _a5 = _la(win.params, _s5, _f5)
        _rec = [r for r in _rows if datetime.strptime(r["ts"], "%d.%m.%y %H:%M") <= _t0 + _td(hours=20.5)]
        _s6 = live_state(_rec, win.params, final_w=15.0)
        _a6 = _la(win.params, _s6, _lf(win.params, _c5, _s6, 200.0, 21.5, 14.0, 6.0, 10.0, silo=True))
        check(_f5.overrun and "уже не менять" not in _a5["head"] and _s6.recirc and "уже не менять" in _a6["head"],
              f"масса прохода занижена, подача идёт — совет «{_a5['head']}»; при рециркуляции — «{_a6['head']}»")
        # --- v1.5: совет D вне проверенного журналом D — рядом режим D + T блоков в проверенной зоне ---
        from dataclasses import replace as _rp5
        from .live import ALT_TOL as _tol
        _cut7 = [r for r in _rows if datetime.strptime(r["ts"], "%d.%m.%y %H:%M") < _t0 + _td(hours=19.0)]
        _p7 = _rp5(win.params, explored={"d": [33.0, 37.0], "t": [40.0, 70.0], "w": [20.0, 30.0]},
                   temp_source="block", t_grain_max=0.0, t_agent_max=0.0)
        _s7 = live_state(_cut7, _p7, final_w=15.0)
        _c7 = mdl.Conditions(_s7.t_proc, 10.0, 70.0)
        _f7 = _lf(_p7, _c7, _s7, 300.0, 23.5, 14.0, 6.0, 10.0)
        _a7 = _la(_p7, _s7, _f7)
        _alt = _f7.alt or {}
        _p8 = _rp5(_p7, explored={"d": [33.0, 37.0], "t": [55.0, 62.0], "w": [20.0, 30.0]})
        _f8 = _lf(_p8, _c7, _s7, 300.0, 23.5, 14.0, 6.0, 10.0)
        check(_f7.d_out_of_zone and bool(_alt) and 31.0 <= _alt["d"] <= 39.0 and _alt["dt"] < 0
              and abs(_alt["w_out"] - _alt["w_goal"]) <= _tol + 1e-9
              and _a7["option"] == "alt" and "T блоков" in _a7["head"] and _a7["t_rec"] == (_alt["t_up"], _alt["t_low"])
              and _f8.alt is None and "ниже" in _f8.alt_reason,
              f"совет D = {_f7.d_rec:.0f} с вне проверенного (D 33–37 с): проверенный режим D = {_alt.get('d', 0):.0f} с и "
              f"T блоков {_alt.get('t_up', 0):.0f}/{_alt.get('t_low', 0):.0f} °C; журнал знает только T 55–62 °C — "
              f"режима нет ({_f8.alt_reason})")
        # --- v1.5.3: «сушим в N силос» — подача; рециркуляция до первой сушки — прогрев; поправка выключена ---
        _t9 = datetime(2026, 11, 1, 22, 30)

        def _r9(h, wi=None, wo=None, note="", d=None, pn=1):
            return {"ts": (_t9 + _td(hours=h)).strftime("%d.%m.%y %H:%M"), "w_in": wi, "w_out": wo, "t_a1": None,
                    "t_a2": None, "t_up": 60, "t_low": 60, "d": d, "note": note, "batch": "1", "pass_no": pn}
        _rows9 = ([_r9(0, 31.4, note="Набираем сушилку с 3 силоса"), _r9(2, note="Запуск топочных блоков"),
                   _r9(2.5, None, 29.1, "Рециркуляция", 35), _r9(4, None, 25.0, d=35),
                   _r9(5.5, 33.0, 23.5, "Сушим в 1 силос", 35)] + [_r9(6 + 0.5 * k, 32.0, 23.8, d=35) for k in range(30)]
                  + [_r9(21.5, 24.3, 24.8, "Закончился 4 силос, рециркуляция", 35)])
        _jp9 = _jp(_prep(_rows9)[0], win.params, 15.0)
        _s9w = live_state(_rows9[:4], win.params, final_w=15.0)
        _a9w = _la(win.params, _s9w, _lf(win.params, mdl.Conditions(60.0, 10.0, 70.0), _s9w, 1000.0, 24.0, 14.0, 6.0,
                                         10.0))
        _s9 = live_state(_rows9[:20], win.params, final_w=15.0)
        check([(x["pass_no"], x["end_kind"]) for x in _jp9] == [(1, "recirc")] and _s9w.warmup
              and _a9w["head"].startswith("Прогрев") and not _s9.recirc and _s9.unloaded > 100
              and not win.tab_live.cb_corr.isChecked() and not win.tab_live.sw_d_only.isChecked(),
              f"прогрев и «сушим в 1 силос»: проходов {len(_jp9)}, при прогреве — «{_a9w['head']}», в сушке выгружено "
              f"{_s9.unloaded:.0f} т; поправка по последним замерам — выключена, «Только разгрузка» — выключено")
        # --- v1.7: набор сушилки — совет на запуск по пробам набора; рециркуляция сушит зерно в шахте ---
        _s9f = live_state(_rows9[:1], win.params, final_w=15.0)
        _f9f = _lf(win.params, mdl.Conditions(60.0, 10.0, 70.0), _s9f, 1000.0, 24.0, 14.0, 6.0, 10.0)
        _a9f = _la(win.params, _s9f, _f9f)
        _kinds = {x[5] for x in _s9w.regime}
        check(_s9f.fill and _a9f.get("phase") == "fill" and _a9f["head"].startswith("При запуске — D = ")
              and _a9f["t_rec"] == (60, 60) and {"fill", "recirc"} <= _kinds,
              f"набор сушилки: «{_a9f['head']}» по пробе набора {_s9f.w_in:g} %; прогрев сушит зерно в шахте")
        # --- v1.7.2: рециркуляция через силос — начало следующего прохода; пределы T 75/75 ---
        _rows10 = [dict(r) for r in _rows9]
        _rows10[-1] = dict(_rows10[-1], note="Закончился 4 силос, рециркуляция через 3 силос")
        _rows10 += [_r9(22.0, 24.0, 22.0, d=35), _r9(22.5, 23.8, 21.5, "Сушим в 4 силос", 35, pn=2)]
        _jp10 = _jp(_prep(_rows10)[0], win.params, 15.0)
        check([(x["pass_no"], x["end_kind"]) for x in _jp10] == [(1, "silo"), (2, None)]
              and (win.params.t_agent_max, win.params.t_grain_max) == (75.0, 75.0),
              f"рециркуляция через силос начинает проход {_jp10[-1]['pass_no']:g}; пределы T блоков/зерна "
              f"{win.params.t_agent_max:g}/{win.params.t_grain_max:g} °C")
        # --- v1.7.3: влияние наружной T по журналу не подбирается (физическая оценка) ---
        _rows11 = [dict(r, t_amb=(10.0 if r.get("batch") == "C" else float(i % 4))) for i, r in enumerate(rows)]
        _p11, _rep11 = calibrate(_rows11, mdl.DryerParams())
        check(_p11.b_amb == mdl.DryerParams().b_amb and "не подбирается" in _rep11["text"]
              and not win._has_fitted_amb(),
              f"T нар в каждой строке, меняется с проходом: b_нар = {_p11.b_amb:+.2f} (физическая оценка), "
              f"R₀ = {_p11.r_ref:.2f}")
        # --- v1.10: размещение по оперативным силосам ---
        from .silos import Silo as _Silo, plan_silos as _plan_silos
        _sl = [_Silo(f"С{i + 1}", 600.0, 0.0, 0, i < 2) for i in range(4)]
        _sp = _plan_silos(_sl, 1100.0, 3, [0.93, 0.93, 0.93], rates=[18.0, 20.0, 22.0], overhead_h=2.0)
        _sl3 = [_Silo(f"С{i + 1}", 500.0, 0.0, 0, i >= 2, i < 2, i == 0) for i in range(4)]      # сырое С1, С2; след. — С1
        _sp3 = _plan_silos(_sl3, 900.0, 3, [0.93, 0.93, 0.93])
        check(_sp.ok and len(_sp.steps) == 6 and _sp.final[2][0] < 0.05 and _sp.final[3][0] < 0.05
              and not _plan_silos(_sl, 2400.0, 3, [0.93] * 3).ok and hasattr(win, "tab_silos")
              and _sp3.ok and _sp3.hold == 0 and sorted(i for i, _m in _sp3.placement) == [0, 1]
              and all(0 not in (x.src, x.dst) for x in _sp3.steps if x.k == 3),
              f"силосы: 1100 т, 3 прохода, в конце в С1 и С2 — {len(_sp.steps)} переключений, {_sp.hours:.0f} ч; "
              "все четыре заняты — сушить некуда; силос под следующую партию на последнем проходе свободен")
        # --- v1.9.2: часы, натура зерна, номер прохода из журнала ---
        from datetime import datetime as _dt92
        _pn = mdl.set_natura(win.params, win.params.natura * 1.1, _dt92(2030, 1, 1))
        _c92 = mdl.Conditions(60.0, 10.0, 70.0)
        win.tabs.setCurrentIndex(win.tabs.indexOfText(_pages.JOURNAL))
        check(abs(mdl.q_at(_pn, 35.0) - 1.1 * mdl.q_at(win.params, 35.0)) < 0.01 and _pn.r_ref == win.params.r_ref
              and mdl.solve_pass(_pn, _c92, 24.0, 1.0, 35.0).w_out > mdl.solve_pass(win.params, _c92, 24.0, 1.0, 35.0).w_out
              and abs(mdl.natura_k(_pn)(_dt92(2029, 1, 1)) - 1 / 1.1) < 1e-9 and win.s_natura.value() == win.params.natura
              and bool(win.tabs.lbl_clock.text()) and bool(win.storage.get_json("pass_label_from")),
              f"натура {win.params.natura:g} → {_pn.natura:g} г/л: Q(D) × 1,1, расход влаги тот же; прежние строки — "
              f"с прежней натурой; часы в журнале: {win.tabs.lbl_clock.text()}")
        # --- v1.9.1: выключенные горелки проход не заканчивают, конец прохода — рециркуляция ---
        from .calibration import journal_passes as _jp91f, prepare as _prep91f

        def _r91(k, note="", w_in=30.0, w_out=24.0, t=None):
            return {"ts": f"02.11.26 {k // 2:02d}:{30 * (k % 2):02d}", "w_in": w_in, "w_out": w_out, "t_up": t,
                    "t_low": t, "d": 35.0, "batch": "1", "pass_no": None, "note": note}
        _j91 = ([_r91(0, "Запуск, сушим с 1 с.", w_out=None, t=60.0)] + [_r91(k) for k in range(1, 6)]
                + [_r91(6, "Охлаждение")] + [_r91(7), _r91(8, "Запуск")] + [_r91(k) for k in range(9, 14)]
                + [_r91(14, "Рециркуляция", None, 14.5), _r91(15, "Охлаждение", None, None)])
        _p91, _ok91 = _prep91f(_j91)
        _jp91 = _jp91f(_p91, win.params)
        check(len(_jp91) == 1 and _jp91[0]["end_kind"] == "cool" and _p91[7]["burner_off"] and not _p91[7]["cool"]
              and not _p91[9]["burner_off"] and _p91[15]["cool"],
              "горелки выключили посреди прохода и включили — проход один; конец прохода — рециркуляция, "
              "охлаждение после неё — конец партии")
        # --- v1.9: проверка пробы и пары при вводе, две пробы, Q(D) по весовой, накладные по журналу ---
        from . import entry as _entry
        from .batches import overhead_from_journal as _ovj, scale_q as _scale_q
        _hist = [{"ts": f"02.11.26 {8 + k // 2:02d}:{30 * (k % 2):02d}", "w_in": 21.0 + 0.2 * (k % 3), "w_out": 14.0,
                  "note": "", "batch": "1", "pass_no": 1} for k in range(6)]
        _new = {"ts": "02.11.26 11:00", "w_in": 31.2, "w_out": 14.1, "note": "", "batch": "1", "pass_no": 1}
        _p2 = _scale_q(win.params, 0.96)
        _c9 = mdl.Conditions(60.0, 10.0, 70.0)
        check(len(_entry.sample_doubt(_hist, _new)) == 1 and not _entry.sample_doubt(_hist, dict(_new, w_in=21.3))
              and _entry.parse_moisture("31,2 30,6")[0] == 30.9
              and _entry.pair_doubt([{"reading": 14.0, "ref": 14.8}] * 5, {"reading": 14.0, "ref": 18.8}) is not None
              and abs(mdl.solve_pass(_p2, _c9, 24.0, 1.0, 35.0).w_out - mdl.solve_pass(win.params, _c9, 24.0, 1.0, 35.0).w_out) < 1e-6
              and abs(mdl.q_at(_p2, 35.0) - 0.96 * mdl.q_at(win.params, 35.0)) < 0.01
              and isinstance(_ovj(rows, win.params)["values"], list),
              "ввод: опечатка 31,2 при пробах 21,0–21,4 переспрошена, две пробы → среднее 30,9; Q(D) × 0,96 не "
              "меняет прогноз влажности")
        # --- v1.8.1: вход модели — среднее проб входа за ±2 ч ---
        import app.calibration as _cal
        from .calibration import observe_all as _oa, predict_out as _po

        def _mae_h(_h: float) -> float:
            _old, _cal.INLET_SMOOTH_H = _cal.INLET_SMOOTH_H, _h
            try:
                _o, _sk, _lg = _oa(rows, win.params)
            finally:
                _cal.INLET_SMOOTH_H = _old
            _w = _po(win.params, _o)[0]
            return sum(abs(float(a) - o["w_out"]) for a, o in zip(_w, _o)) / len(_o)
        _m0, _m2 = _mae_h(0.0), _mae_h(2.0)
        check(_m2 < _m0, f"вход — среднее проб за ±2 ч: ошибка w_вых по строкам примера ±{_m0:.2f} → ±{_m2:.2f} п.п.")
        # --- v1.8: культуры — своя модель и свой журнал у каждой; длинные пояснения свёрнуты ---
        from . import crops as _crops
        from .ui_common import Collapsible as _Coll
        _n_corn, _p_corn = len(win.storage.load_journal()), win.params
        win.switch_crop("wheat")
        _pw = win.params
        _wheat_ok = (_pw.crop == "wheat" and len(win.storage.load_journal()) == 0 and _pw.rate_sigma == 0.25
                     and abs(_pw.r_ref - 1.6 * _crops.start_params(_crops.get("corn"), _p_corn).r_ref) < 1e-9
                     and abs(mdl.corn_equiv(_pw, 21.3) - 22.0) < 1e-6 and (_pw.t_grain_max, _pw.t_agent_max) == (50.0, 140.0)
                     and "пшеницы" in win.windowTitle())
        win.switch_crop("corn")
        _boxes = win.findChildren(_Coll)
        check(_wheat_ok and win.params.crop == "corn" and len(win.storage.load_journal()) == _n_corn
              and win.params.r_ref == _p_corn.r_ref and len(_boxes) >= 14,
              f"культуры: пшеница — стартовая оценка (R₀ ×1,6, T зерна ≤ {_pw.t_grain_max:g} °C, в шахте {_pw.m_dryer:g} т), "
              f"журнал кукурузы на месте ({_n_corn} строк); сворачиваемых пояснений {len(_boxes)}")
        from . import backup as bkp
        bk = os.path.join(tmp, "copy.db")
        bkp.copy_db(win.storage.conn, bk)
        check(bkp.check_db(bk) == "" and len(bkp.list_backups(win.storage.path)) >= 1,
              f"резервные копии: автокопия есть, ручная копия {os.path.getsize(bk)} байт")
        win.resize(1400, 880)
        win.show()
        tabs = win.centralWidget()
        shots_dir = os.path.dirname(os.path.abspath(out_path))
        for i in range(tabs.count()):
            tabs.setCurrentIndex(i)
            app.processEvents()
            pm = win.grab()
            check((not pm.isNull()) and pm.width() >= 800, f"раздел {i + 1} «{tabs.tabText(i)}» отрисован {pm.width()}×{pm.height()}")
            pm.save(os.path.join(shots_dir, f"selftest_tab{i + 1}.png"))

        # --- оформление: светлое и тёмное — контраст всех цветных надписей и переключение на ходу ---
        from .theme import theme_for
        from . import design as _design
        _mode0 = _design.mode()
        for label, mode in (("светлое", "light"), ("тёмное", "dark")):
            win.set_ui_theme(mode, save=False)
            app.processEvents()
            th = theme_for(win)
            style = _design.style_name()
            check(th.dark == (mode == "dark") and style.lower() == "fusion",
                  f"оформление {label}: стиль окна {style}, фон {_design.tokens().paper}")
            bad = [f"{n} {c:.1f}<{need:g}" for n, c, need in th.contrast_report() if c < need]
            check(not bad, f"оформление {label}: контраст надписей "
                           + ("в норме" if not bad else "мал: " + ", ".join(bad)))
            check(th.summary_bg in win.sm_summary.styleSheet() and win.tabs.side.width() > 150,
                  f"оформление {label}: сводка перекрашена ({th.summary_bg}), меню {win.tabs.side.width()} px")
            if th.dark:
                tabs.setCurrentIndex(tabs.indexOfText(_pages.LIVE))
                app.processEvents()
                win.grab().save(os.path.join(shots_dir, "selftest_dark_live.png"))
                tabs.setCurrentIndex(0)
                app.processEvents()
                win.grab().save(os.path.join(shots_dir, "selftest_dark_tab1.png"))
        win.set_ui_theme(_mode0, save=False)
        app.processEvents()
        win.close()
    except Exception:  # noqa: BLE001
        ok = False
        lines.append("EXCEPTION\n" + traceback.format_exc())
    lines.append("RESULT: " + ("OK" if ok else "FAILED"))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0 if ok else 1
