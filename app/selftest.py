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
        ws.append(["Время", "Вход", "Выход", "Т кукурузы", "Т топочных блоков", "Разгрузка", "Примечание"])
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
        win._qe_add()
        check(win.tbl_j.rowCount() == n0 + 1 and win.tbl_j.item(n0, 0).text().startswith("25.10.26 07:00"),
              f"быстрый ввод: строка {win.tbl_j.item(n0, 0).text()} добавлена, время сдвинулось на 30 мин")
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
        check(n_plan >= 1, f"план опытов: {n_plan} прох., первый D = "
              f"{win.tab_check.doe_tbl.item(0, 1).text() if n_plan else '—'}")
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
        check("ШПАРГАЛКА" in win.tab_help.manual.toPlainText(), "инструкция для пользователя встроена в «Справку»")
        check("СЛОВА-ЛОВУШКИ" in win.tab_help.rules.toPlainText(), "правила работы встроены в «Справку»")
        # --- v0.6: режим сушильщика, PIN, замечания, статистика пилота ---
        from . import pilot as _pilot
        from . import pages as _pages
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
        check(max(_w_op, _w_eng) <= 1366, f"окно помещается в экран 1366 px: сушильщик {_w_op}, инженер {_w_eng} px")
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
        check(bool(_trap("зерно на выходе хорошо охлаждено")) and _trap("Остановка: силос полный") is None,
              "слова-ловушки в примечании распознаются")
        from PySide6.QtCore import QDateTime as _QDT
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
        check(bool(_adv.get("head")) and win.tab_live.lbl_head.text() == _adv["head"]
              and win.tab_live.t_pass.lbl_value.text() not in ("", "—") and _nav.lbl_title.text() == _pages.LIVE,
              f"«{_pages.LIVE}»: «{_adv.get('head', '?')}», конец прохода {win.tab_live.t_pass.lbl_value.text()}")
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

        # --- светлая и тёмная темы: контраст всех цветных надписей и переключение на ходу ---
        from .theme import dark_palette, theme_for
        light_pal = app.palette()
        for label, pal in (("системная", light_pal), ("тёмная", dark_palette())):
            app.setPalette(pal)
            app.processEvents()
            win._theme_changed()
            app.processEvents()
            th = theme_for(win)
            style = app.style().name()
            check(not (th.dark and style.lower() in ("windowsvista", "windows")),
                  f"тема {label}: стиль окна {style}")
            bad =[f"{n} {c:.1f}<{need:g}" for n, c, need in th.contrast_report() if c < need]
            check(not bad, f"тема {label} ({'тёмная' if th.dark else 'светлая'}): контраст надписей "
                           + ("в норме" if not bad else "мал: " + ", ".join(bad)))
            check(th.summary_bg in win.sm_summary.styleSheet(), f"тема {label}: сводка перекрашена ({th.summary_bg})")
            if th.dark:
                tabs.setCurrentIndex(0)
                app.processEvents()
                win.grab().save(os.path.join(shots_dir, "selftest_dark_tab1.png"))
        app.setPalette(light_pal)
        app.processEvents()
        win._theme_changed()
        win.close()
    except Exception:  # noqa: BLE001
        ok = False
        lines.append("EXCEPTION\n" + traceback.format_exc())
    lines.append("RESULT: " + ("OK" if ok else "FAILED"))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0 if ok else 1
