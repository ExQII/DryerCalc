"""Проверки v0.4: физическая модель, погода, веса калибровки, партии, очередь, влагомер, «по факту», бэкап, отчёты.

Запуск: python tests/test_features.py   или   python -m pytest tests
"""
import gzip
import math
import os
import sqlite3
import sys
import tempfile
from dataclasses import replace
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import physics as ph  # noqa: E402
from app.analysis import backtest, meter_bias, t975  # noqa: E402
from app.backup import auto_backup, check_db, copy_db, restore_into  # noqa: E402
from app.batches import balance, batches_from_journal, fuel_metrics, fuel_rate_from_batches, mix, schedule  # noqa: E402
from app.calibration import calibrate, read_journal, recency_weights  # noqa: E402
from app.live import live_forecast, live_state  # noqa: E402
from app.model import Conditions, DryerParams, d_for_target, plan_by_passes, q_at, solve_pass, water_rate  # noqa: E402,F401
from app.storage import Storage  # noqa: E402
from app.weather import WeatherSeries, classify_header, merge_points, read_weather  # noqa: E402

SAMPLE = os.path.join(ROOT, "examples", "journal_sample_oct2026.csv")
P = DryerParams()
C = Conditions(60.0, 10.0, 70.0)


def _tmp(name: str) -> str:
    return os.path.join(tempfile.mkdtemp(prefix="dryer_t_"), name)


# ---------------------------------------------------------------------------
# Физическая модель
# ---------------------------------------------------------------------------

def test_emc_and_psychrometrics_match_known_values():
    assert abs(ph.emc_db(20, 70) / (100 + ph.emc_db(20, 70)) * 100 - 14.0) < 0.3   # ≈14 % на сырую массу
    assert abs(ph.wet_bulb(20.0, round(ph.humidity_ratio(20, 50), 7)) - 13.7) < 0.3  # психрометрическая таблица
    rh60 = ph.agent_rh(60, 10, 70)
    assert 3.5 < rh60 < 5.0                     # нагрев 10 °C/70 % до 60 °C даёт RH ≈ 4 %
    t, rh = ph.bed_air(60.0, 10.0, 70.0, 1.0)
    assert abs(t - 60) < 1e-9 and abs(rh - rh60) < 1e-6


def test_thompson_curve_monotonic():
    assert ph.thompson_ln_mr(0, 60) == 0.0
    a, b = ph.thompson_ln_mr(1, 60), ph.thompson_ln_mr(3, 60)
    assert 0 > a > b                             # дольше — суше
    assert ph.thompson_ln_mr(2, 80) < ph.thompson_ln_mr(2, 50)  # горячее — быстрее


def test_physics_pass_behaviour():
    p = replace(P, model_kind="physics")
    w15, w35, w100 = (solve_pass(p, C, 30, 1, d).w_out for d in (15, 35, 100))
    assert w15 < w35 < w100 < 30
    assert solve_pass(p, Conditions(75, 10, 70), 30, 1, 35).w_out < w35
    d = d_for_target(p, C, 30, 25.0)
    assert abs(solve_pass(p, C, 30, 1, d).w_out - 25.0) < 0.05
    # холодная погода сушит медленнее в обеих моделях (прогрев холодного зерна)
    for kind in ("physics", "empirical"):
        q = replace(P, model_kind=kind)
        cold = solve_pass(q, Conditions(60, -5, 70), 25, 1, 35).w_out
        warm = solve_pass(q, Conditions(60, 20, 70), 25, 1, 35).w_out
        assert cold > warm, kind


def test_empirical_ambient_fix_keeps_reference_results():
    # при опорной T_нар новая запись (T − T_база) совпадает со старой (T − T_нар)
    old = P.r_ref * (((22 - P.w_eq) / (P.w_ref - P.w_eq)) ** P.b_w) * (((60 - 10) / (P.t_ref - P.t_amb_ref)) ** P.b_dt)
    assert abs(water_rate(P, 22, Conditions(60, 10, 70)) - old) < 1e-12
    assert water_rate(P, 22, Conditions(60, 0, 70)) < water_rate(P, 22, Conditions(60, 10, 70))


def test_calibration_fits_both_models_on_sample():
    rows, _ = read_journal(SAMPLE)
    new, rep = calibrate(rows, DryerParams())
    assert rep["seg_mae"] < 0.3
    assert 0.2 < new.eta < 1.5 and rep["seg_mae_phys"] < 0.8
    assert "ФИЗИЧЕСКАЯ" in rep["text"]
    pl = plan_by_passes(replace(new, model_kind="physics"), C, 500, 34, 14, 4)
    assert pl.reaches_target


def test_recency_weights_and_weighted_calibration():
    rows, _ = read_journal(SAMPLE)
    obs = [{"dt": datetime(2026, 10, 25)}, {"dt": datetime(2026, 10, 18)}, {"dt": None}]
    w = recency_weights(obs, 7.0)
    assert abs(w[0] - 1.0) < 1e-12 and abs(w[1] - 0.5) < 1e-12 and w[2] == 1.0
    assert recency_weights(obs, 0) == [1.0, 1.0, 1.0]
    a, _ = calibrate(rows, P, half_life_days=0)
    b, _ = calibrate(rows, P)
    assert abs(a.r_ref - b.r_ref) < 1e-12
    c, rep = calibrate(rows, P, half_life_days=2.0)
    assert c.r_ref > 0 and "полураспад" in rep["text"]


# ---------------------------------------------------------------------------
# Погода
# ---------------------------------------------------------------------------

RP5 = ('# Метеостанция Тест\n# Скачано с rp5\n"Местное время в Тест";"T";"Po";"U";"DD";"Td";\n'
       '"25.10.2026 09:00";"3.4";"750.1";"88";"Ветер";"1.5";\n'
       '"25.10.2026 06:00";"1.2";"750.4";"95";"Ветер";"0.5";\n'
       '"25.10.2026 03:00";"";"750.6";"97";"Ветер";"";\n')

OPEN_METEO = ("latitude,longitude,elevation,utc_offset_seconds,timezone,timezone_abbreviation\n"
              "47.2,39.7,50.0,10800,Europe/Moscow,MSK\n\n"
              "time,temperature_2m (°C),relative_humidity_2m (%)\n"
              "2026-10-24T08:00,6.1,81\n2026-10-24T09:00,7.4,76\n")

LOGGER = "No.;Дата;Время;Температура, °C;Влажность, %RH\n1;24.10.2026;10:00;8,5;70\n2;24.10.2026;10:10;8,7;69\n"


def test_headers_classification():
    assert classify_header("T") == "t" and classify_header("U") == "rh"
    assert classify_header("Td") is None and classify_header("Температура точки росы") is None
    assert classify_header("Местное время в Ростове-на-Дону") == "dt"
    assert classify_header("temperature_2m (°C)") == "t" and classify_header("relative_humidity_2m (%)") == "rh"
    assert classify_header("Дата") == "date" and classify_header("Время") == "time"


def test_read_weather_formats():
    p1 = _tmp("rp5.csv")
    with open(p1, "w", encoding="cp1251") as f:
        f.write(RP5)
    pts, info = read_weather(p1)
    assert [x["dt"].hour for x in pts] == [3, 6, 9]          # отсортировано по времени
    assert pts[0]["t"] is None and pts[0]["rh"] == 97.0 and pts[2]["t"] == 3.4
    p2 = _tmp("om.csv.gz")
    with open(p2, "wb") as f:
        f.write(gzip.compress(OPEN_METEO.encode("utf-8")))
    pts, _ = read_weather(p2)
    assert len(pts) == 2 and pts[1]["t"] == 7.4 and pts[1]["dt"] == datetime(2026, 10, 24, 9, 0)
    p3 = _tmp("logger.csv")
    with open(p3, "w", encoding="utf-8-sig") as f:
        f.write(LOGGER)
    pts, _ = read_weather(p3)
    assert len(pts) == 2 and pts[0]["dt"] == datetime(2026, 10, 24, 10, 0) and pts[0]["t"] == 8.5
    p4 = _tmp("u16.csv")
    with open(p4, "w", encoding="utf-16") as f:
        f.write(RP5)
    assert len(read_weather(p4)[0]) == 3


def test_weather_interpolation_and_fill():
    ws = WeatherSeries([{"dt": datetime(2026, 10, 18, 6), "t": 2.0, "rh": 90},
                        {"dt": datetime(2026, 10, 18, 12), "t": 8.0, "rh": 60},
                        {"dt": datetime(2026, 10, 19, 12), "t": 5.0, "rh": 80}])
    t, rh = ws.at(datetime(2026, 10, 18, 9))
    assert abs(t - 5.0) < 1e-9 and abs(rh - 75) < 1e-9          # линейно между 6:00 и 12:00
    t, _ = ws.at(datetime(2026, 10, 18, 14))
    assert t == 8.0                                                # дыра 24 ч: ближайший в пределах 3 ч
    assert ws.at(datetime(2026, 10, 18, 20)) == (None, None)       # далеко от замеров
    rows, _ = read_journal(SAMPLE)
    filled = ws.fill_rows(rows)
    assert filled[0]["t_amb"] is not None and rows[0]["t_amb"] is None   # исходный журнал не меняется
    have, total = ws.coverage(rows)
    assert 0 < have < total
    m = merge_points([{"dt": datetime(2026, 1, 1, 1), "t": 1.0, "rh": None}],
                     [{"dt": datetime(2026, 1, 1, 1), "t": None, "rh": 50.0}, {"dt": datetime(2026, 1, 1, 2), "t": 3.0, "rh": 40.0}])
    assert len(m) == 2 and m[0]["t"] == 1.0 and m[0]["rh"] == 50.0


def test_calibration_uses_weather_table():
    rows, _ = read_journal(SAMPLE)
    ws = WeatherSeries([{"dt": datetime(2026, 10, 17) + timedelta(hours=h), "t": 5 + 5 * math.sin(h / 4.0), "rh": 80}
                        for h in range(0, 24 * 10, 3)])
    _, rep = calibrate(rows, P, weather=ws)
    assert "Наружная T/RH есть у" in rep["text"]


# ---------------------------------------------------------------------------
# Партии, топливо, смешивание, очередь
# ---------------------------------------------------------------------------

def test_batch_balance_and_fuel():
    b = {"m_in": 400, "w_in": 33.0, "m_out": 350, "w_out": 23.0, "waste": 0, "fuel": 5000, "fuel_price": 7}
    bal = balance(b)
    exp = 400 * 67 / 77
    assert abs(bal["m_out_expected"] - exp) < 1e-9
    assert abs(bal["w_out_by_mass"] - (100 - 400 * 67 / 350)) < 1e-9
    assert abs(bal["ton_percent"] - 4000) < 1e-9
    assert abs(bal["shrink_formula_pct"] - 100 * 10 / 77) < 1e-9
    fu = fuel_metrics(b)
    assert abs(fu["per_water"] - 5000 / 50) < 1e-9 and abs(fu["cost"] - 35000) < 1e-9
    rate, n = fuel_rate_from_batches([b, {"m_in": 100, "w_in": 20, "w_out": 14, "fuel": 700}])
    w2 = 100 - 100 * 80 / 86
    assert n == 2 and abs(rate - (5700 / (50 + w2))) < 1e-9


def test_mix_and_schedule():
    m, w = mix([(300, 34.0), (200, 29.0)])
    assert m == 500 and abs(w - 32.0) < 1e-12
    items = [{"name": "A", "mass": 500, "w0": 34, "wk": 14},
             {"name": "B", "mass": 300, "w0": 30, "wk": 14, "ready": "01.11.2026 08:00"}]
    st = datetime(2026, 10, 29, 8, 0)
    q = schedule(items, P, C, st, method="q", q=18.2, lim=6)
    assert q[0].start == st and q[1].start >= q[0].end
    assert q[0].passes == 4                         # 20 п.п. при лимите 6 → 4 прохода
    q2 = schedule(items, P, C, st, method="model")
    assert all(e.plan is not None and e.plan.reaches_target for e in q2)
    assert q2[1].start >= datetime(2026, 11, 1, 8, 0) or q2[1].start >= q2[0].end


def test_batches_from_sample_journal():
    rows, _ = read_journal(SAMPLE)
    bs = batches_from_journal(rows)
    assert [b["name"] for b in bs] == ["A", "B", "C"]
    assert bs[0]["start"].startswith("18.10.2026")


# ---------------------------------------------------------------------------
# Влагомер, проверка модели
# ---------------------------------------------------------------------------

def test_meter_bias_statistics():
    assert abs(t975(1) - 12.706) < 1e-9 and abs(t975(60) - 2.0) < 0.01
    pairs = [{"reading": 15.0 + d, "ref": 15.0} for d in (0.8, 1.0, 0.6, 0.9, 0.7, 5.0)]
    r = meter_bias(pairs)
    assert r["outliers"] == [5] and r["n"] == 5 and abs(r["mean"] - 0.8) < 1e-9
    assert r["ci"][0] < 0.8 < r["ci"][1]
    assert "Пар нет" in meter_bias([])["text"]


def test_backtest_on_sample():
    rows, _ = read_journal(SAMPLE)
    bt = backtest(rows, DryerParams())
    assert len(bt["rows"]) == 4
    assert bt["mae"]["cur_emp"] < 0.3 and bt["mae"]["fc_emp"] < 0.6
    assert bt["rows"][0].get("fc_emp") is None and bt["rows"][-1].get("fc_emp") is not None
    assert abs(bt["drift"]) < 0.5


# ---------------------------------------------------------------------------
# По факту
# ---------------------------------------------------------------------------

def test_live_state_and_forecast():
    rows, _ = read_journal(SAMPLE)
    st = live_state(rows, P)
    assert st.ok and st.batch == "C" and st.start == datetime(2026, 10, 24, 8, 0)
    assert 300 < st.unloaded < 450                 # ~22 ч по ~18 т/ч
    c = Conditions(st.t_proc, 10, 70)
    fc = live_forecast(P, c, st, m_pass=420.0, w_target=24.0, wk_final=14.0, soft=6, hard=10)
    assert 1 <= fc.d_rec <= 100
    assert abs(fc.w_out_rec - 24.0) < 0.6          # округление D до целых секунд
    assert abs(fc.tau_h - P.lag_k * P.m_dryer / q_at(P, fc.d_rec)) < 1e-9
    assert fc.batch_end and fc.pass_end and fc.batch_end > fc.pass_end
    st2 = live_state([], P)
    assert not st2.ok


# ---------------------------------------------------------------------------
# Резервные копии и хранилище
# ---------------------------------------------------------------------------

def test_backup_and_restore():
    db = _tmp("dryer.db")
    s = Storage(db)
    s.set_json("x", 1)
    made = auto_backup(s.conn, s.path, keep=2, today=date(2026, 10, 1))
    assert made and made.exists()
    assert auto_backup(s.conn, s.path, keep=2, today=date(2026, 10, 1)) is None    # раз в сутки
    auto_backup(s.conn, s.path, keep=2, today=date(2026, 10, 2))
    auto_backup(s.conn, s.path, keep=2, today=date(2026, 10, 3))
    assert len(list(made.parent.glob("dryer_????-??-??.db"))) == 2                    # хранятся последние 2
    out = _tmp("copy.db")
    copy_db(s.conn, out)
    assert check_db(out) == ""
    s.set_json("x", 2)
    restore_into(s.conn, out)
    assert s.get_json("x") == 1
    bad = _tmp("bad.db")
    with open(bad, "w") as f:
        f.write("not a db")
    assert check_db(bad) != ""
    s.close()


def test_storage_weather_and_lists():
    s = Storage(_tmp("w.db"))
    s.replace_weather([{"dt": datetime(2026, 10, 1, 9), "t": 4.5, "rh": 81.0, "src": "rp5"}])
    w = s.load_weather()
    assert w[0]["t"] == 4.5 and w[0]["dt"] == datetime(2026, 10, 1, 9)
    s.set_list("batches", [{"name": "A"}, "мусор"])
    assert s.get_list("batches") == [{"name": "A"}]
    s.close()


def test_old_database_is_migrated():
    path = _tmp("old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE journal(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, w_in REAL)")
    conn.commit()
    conn.close()
    s = Storage(path)
    s.replace_journal([{"ts": "08:00", "w_in": 20.0, "w_out": 15.0, "d": 35}])
    assert s.load_journal()[0]["w_out"] == 15.0
    assert s.load_weather() == []
    s.close()


def test_params_from_old_settings_get_new_defaults():
    old = {"r_ref": 1.9, "b_w": 0.7, "calibrated": True, "calib_info": "x"}
    p = DryerParams.from_dict(old)
    assert p.model_kind == "empirical" and p.eta == DryerParams().eta and p.b_amb == DryerParams().b_amb


# ---------------------------------------------------------------------------
# Отчёты (нужен Qt; без дисплея — offscreen)
# ---------------------------------------------------------------------------

def test_reports_are_written():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])  # noqa: F841
    from app.reports import batch_report_data, batch_report_html, batch_report_xlsx, html_to_pdf, journal_blank_pdf, season_xlsx
    rows, _ = read_journal(SAMPLE)
    pdf = _tmp("blank.pdf")
    assert journal_blank_pdf(pdf, datetime(2026, 10, 29, 8), 24) == 2
    with open(pdf, "rb") as f:
        assert f.read(5) == b"%PDF-"
    b = {"name": "C", "start": "24.10.2026 08:00", "end": "25.10.2026 07:00", "m_in": 400, "w_in": 33.3,
         "m_out": 352, "w_out": 23.6, "fuel": 5200, "fuel_price": 7.2}
    data = batch_report_data(b, rows, P)
    assert len(data["journal"]["passes"]) == 1
    html_to_pdf(batch_report_html(data, "м³"), _tmp("b.pdf"))
    x = _tmp("b.xlsx")
    batch_report_xlsx(x, data, "м³")
    season_xlsx(_tmp("s.xlsx"), [b], "м³")
    from openpyxl import load_workbook
    wb = load_workbook(x)
    assert wb.sheetnames == ["Партия", "Проходы"] and wb["Проходы"].max_row == 2


# ---------------------------------------------------------------------------
# v0.5: поправка влагомера, допуски, рециркуляция, журнал, план опытов, качество, КПД, напоминания
# ---------------------------------------------------------------------------

def test_meter_conversion_inverse_and_backward_compatible():
    from app.model import read_from_true, true_from_read
    assert abs(true_from_read(P, 15.0) - 14.2) < 1e-12          # старая запись: показание − 0,8
    q = replace(P, out_kw=0.08, out_kt=0.05)
    for w in (13.0, 20.0, 31.0):
        for t in (None, 25.0, 40.0):
            assert abs(read_from_true(q, true_from_read(q, w, t), t) - w) < 1e-9


def test_meter_fit_recovers_slope_and_temperature():
    import random
    rnd = random.Random(7)
    pairs = []
    for _ in range(24):
        ref, t = rnd.uniform(13, 30), rnd.uniform(25, 45)
        pairs.append({"reading": ref + 0.7 + 0.06 * (ref - 20) + 0.04 * (t - 30) + rnd.gauss(0, 0.1), "ref": ref, "t": t})
    r = meter_bias(pairs)
    assert r["choice"] == "M2" and abs(r["kt"] - 0.04) < 0.01 and abs(r["kw"] - 0.06 / 1.06) < 0.02
    few = meter_bias(pairs[:6])
    assert few["choice"] == "M0"                                 # мало пар — только постоянная поправка


def test_time_range_brackets_nominal():
    from app.model import time_range
    build = lambda pp: plan_by_passes(pp, C, 500, 34, 14, 4, cool=True)  # noqa: E731
    nominal = build(P).total_time_h
    lo, hi = time_range(build, P)
    assert lo < nominal < hi and hi - lo > 2.0
    lo0, hi0 = time_range(build, replace(P, rate_sigma=0.0))
    assert abs(lo0 - nominal) < 1e-9 and abs(hi0 - nominal) < 1e-9


def test_recirculation_balance_and_trends():
    from app.model import recirc
    for kind in ("empirical", "physics"):
        q = replace(P, model_kind=kind)
        r = recirc(q, C, 60.0, 18.0, 14.0)
        assert r.hours is not None and 0.3 < r.hours < 10, (kind, r.hours)
        assert abs(r.m_end * (100 - r.w_end) - 60.0 * (100 - 18.0)) < 1e-6     # сухое вещество
        assert abs(r.water - (60.0 - r.m_end)) < 1e-9
        hot = recirc(q, Conditions(75, 10, 70), 60.0, 18.0, 14.0)
        assert hot.hours < r.hours                                   # горячее — быстрее
    assert recirc(P, C, 30.0, 18.0, 14.0).note                       # неполная шахта — предупреждение
    assert recirc(P, C, 60.0, 18.0, 9.0, max_h=5).hours is None


def test_journal_flags_recirculation_and_burners_off():
    from app.calibration import observe_all, prepare
    rows, _ = read_journal(SAMPLE)
    prep, _ = prepare(rows)
    flagged = [(r["ts"], r["recirc"], r["burner_off"]) for r in prep if r["recirc"] or r["burner_off"]]
    assert ("22.10.26 02:30", True, False) in flagged and ("22.10.26 03:35", True, True) in flagged
    extra = [{"ts": "22.10.26 03:40", "w_in": 14.0, "w_out": 13.5, "d": 100, "note": "", "batch": "B", "pass_no": 3}]
    obs1, sk1, _ = observe_all(rows, P)
    k = next(i for i, r in enumerate(rows) if (r.get("ts") or "").startswith("22.10.26 03:35")) + 1
    obs2, sk2, _ = observe_all(rows[:k] + extra + rows[k:], P)
    assert len(obs1) == len(obs2) and sk2.get("горелки выключены (охлаждение)") == 1


def test_rate_sigma_from_sample_backtest():
    from app.analysis import rate_sigma_from_backtest
    rows, _ = read_journal(SAMPLE)
    s, n = rate_sigma_from_backtest(backtest(rows, DryerParams()), "empirical")
    assert n == 3 and 0.03 < s < 0.07


def test_experiment_plan_respects_limits():
    from app.doe import explored_passes, suggest, typical_regime
    rows, _ = read_journal(SAMPLE)
    passes = explored_passes(rows, P)
    d_typ, t_typ = typical_regime(passes)
    p = replace(P, t_grain_max=45.0)
    plan, ok = suggest(p, C, 22.0, passes, 6.0, d_typ, t_typ, 4, True, t_max=70.0)
    assert plan and len(plan) <= 4
    for x in plan:
        assert max(x.dw_emp, x.dw_phys) <= 6.0 + 1e-9 and x.t <= 70.0 and x.t_grain <= 45.0
        assert abs(x.d - d_typ) < 0.5 or abs(x.t - t_typ) < 0.5            # один фактор
    assert len({(x.d, x.t) for x in plan}) == len(plan)
    assert all(a.score >= b.score for a, b in zip(ok, ok[1:]))


def test_quality_and_thermal_efficiency():
    from app.batches import correlation, quality_findings, quality_table
    b = {"m_in": 400, "w_in": 30.0, "m_out": 360, "w_out": 20.0, "fuel": 4000}
    fu = fuel_metrics(b, lhv=33.5)
    assert abs(fu["mj_per_kg"] - 4000 * 33.5 / (40 * 1000)) < 1e-9
    assert abs(fu["thermal_eff"] - 2.5 / fu["mj_per_kg"]) < 1e-12
    assert "mj_per_kg" not in fuel_metrics(b)
    r, slope, n = correlation([1, 2, 3, 4], [2, 4, 6, 8.5])
    assert n == 4 and r > 0.99 and slope > 2
    rows, _ = read_journal(SAMPLE)
    tab = quality_table([{"name": "C", "start": "24.10.2026 08:00", "end": "25.10.2026 07:00", "cracks": 6}], rows, P)
    assert tab[0]["max_dw"] and tab[0]["tg_max"] and tab[0]["passes"] == 1
    assert "с 5 партий" in quality_findings(tab)[0]


def test_reminders_logic():
    from app.reminders import ReminderCfg, check
    rows, _ = read_journal(SAMPLE)
    rows = [r for r in rows if not (r.get("ts") or "").startswith("25.10.26 06:30")] + [
        {"ts": "25.10.26 06:30", "w_in": 28.7, "w_out": 23.2, "d": 35, "note": "", "batch": "C", "pass_no": 1}]
    cfg = ReminderCfg()
    last = datetime(2026, 10, 25, 6, 30)
    st: dict = {}
    assert check(rows, last + timedelta(minutes=20), st, cfg) == []
    m = check(rows, last + timedelta(minutes=45), st, cfg)
    assert [k for k, _, _ in m] == ["sample"]
    assert check(rows, last + timedelta(minutes=50), st, cfg) == []           # не чаще раза в интервал
    assert [k for k, _, _ in check(rows, last + timedelta(minutes=80), st, cfg)] == ["sample"]
    m = check(rows, last + timedelta(minutes=10), {}, cfg, pass_end=last + timedelta(minutes=50))
    assert ("pass_end" in [k for k, _, _ in m])
    st2: dict = {}
    assert [k for k, _, _ in check(rows, last + timedelta(minutes=5), st2, cfg, target_read=25.0)] == ["deviation"]
    assert check(rows, last + timedelta(minutes=6), st2, cfg, target_read=25.0) == []   # одно на запись
    stopped = rows + [{"ts": "25.10.26 06:40", "note": "Остановка", "batch": "C", "pass_no": 1}]
    assert check(stopped, last + timedelta(minutes=60), {}, cfg) == []
    assert check(rows, last + timedelta(hours=9), {}, cfg) == []              # журнал давно не вёлся
    assert check(rows, last + timedelta(minutes=45), {}, ReminderCfg(enabled=False)) == []


def test_live_ignores_recirculation_unloading():
    rows, _ = read_journal(SAMPLE)
    b3 = [r for r in rows if r.get("batch") == "B" and r.get("pass_no") == 3]
    st = live_state(b3[:-1], P)       # последний участок — проход B3 с рециркуляцией в конце
    st_cut = live_state([r for r in b3 if not (r.get("ts") or "") >= "22.10.26 02:00"], P)
    assert st.recirc and not st_cut.recirc
    assert st.unloaded - st_cut.unloaded < q_at(P, 40) * 0.6     # строки рециркуляции не добавляют выгрузки


# ---------------------------------------------------------------------------
# v0.6: режим сушильщика, PIN, пилот (события, покрытие проб, замечания)
# ---------------------------------------------------------------------------

def test_pin_hash_and_check():
    from app.pilot import pin_hash, pin_ok
    h = pin_hash("1234")
    assert pin_ok("1234", h) and not pin_ok("4321", h) and pin_ok("anything", None)
    assert pin_hash("1234") != h                       # соль разная


def test_sample_coverage_on_sample_journal():
    from app.pilot import sample_coverage
    rows, _ = read_journal(SAMPLE)
    cov = sample_coverage(rows)
    assert cov["passes"] >= 4 and cov["actual"] <= cov["expected"] + cov["passes"]
    assert 0.8 < cov["coverage"] <= 1.05 and 25 <= cov["median_interval"] <= 35


def test_pilot_events_stats_and_export():
    from app.pilot import export_xlsx, pilot_stats, stats_text
    st = Storage(_tmp("p.db"))
    t0 = datetime(2026, 11, 2, 8, 0)
    st.log_event("start", {"mode": "operator"}, ts=t0)
    st.log_event("reminder", {"key": "sample"}, ts=t0 + timedelta(minutes=40))
    st.log_event("sample", {"ts": "02.11.26 08:30"}, ts=t0 + timedelta(minutes=47))
    st.log_event("feedback", {"who": "Иван", "text": "мелкий шрифт", "tab": "Журнал", "mode": "operator"},
                 ts=t0 + timedelta(hours=1))
    ev = st.load_events()
    assert [e["kind"] for e in ev] == ["start", "reminder", "sample", "feedback"]
    stt = pilot_stats(ev, [])
    assert stt["reaction_median"] == 7.0 and stt["quick_samples"] == 1 and len(stt["feedback"]) == 1
    assert stt["modes"] == {"operator": 1}
    assert "сушильщик 1" in stats_text(stt)
    path = _tmp("pilot.xlsx")
    export_xlsx(path, stt, ev)
    from openpyxl import load_workbook
    wb = load_workbook(path)
    assert wb.sheetnames == ["Сводка", "Замечания", "События", "Интервалы проб"] and wb["Замечания"].max_row == 2
    st.clear_events(keep_feedback=True)
    assert [e["kind"] for e in st.load_events()] == ["feedback"]
    st.close()


def test_operator_mode_shows_only_shift_tabs():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])  # noqa: F841
    from app import pilot
    from app.gui import MainWindow
    win = MainWindow(Storage(_tmp("ui.db")))
    assert win.first_run and win.mode == pilot.ENGINEER
    base = QApplication.font().pointSizeF()
    win.apply_mode(pilot.OPERATOR)
    vis = [win.tabs.tabText(i) for i in range(win.tabs.count()) if win.tabs.isTabVisible(i)]
    assert vis == list(pilot.OPERATOR_TABS) and win.tabs.tabText(win.tabs.currentIndex()) in vis
    assert QApplication.font().pointSizeF() == base + 2 and not win.j_buttons["Калибровать модель"].isVisibleTo(win)
    win.apply_mode(pilot.ENGINEER)
    assert all(win.tabs.isTabVisible(i) for i in range(win.tabs.count())) and QApplication.font().pointSizeF() == base
    assert win.storage.get_json("ui_mode")["mode"] == pilot.ENGINEER
    win.close()



# ---------------------------------------------------------------------------
# v0.7: расчёт по массивам (numpy) совпадает со скалярным solve_pass / pass_out
# ---------------------------------------------------------------------------

def test_vectorized_model_matches_scalar():
    import random
    from app.model import pass_rate_arr, q_at_arr, solve_out_arr
    rnd = random.Random(7)
    n = 400
    w_in = [rnd.uniform(8.0, 40.0) for _ in range(n)] + [8.02, 8.05, 9.0, 45.0]
    d = [rnd.uniform(-5.0, 110.0) for _ in range(n)] + [0.5, 1.0, 100.0, 150.0]
    t_proc = [rnd.uniform(20.0, 90.0) for _ in range(len(w_in))]
    t_amb = [rnd.choice([10.0, rnd.uniform(-15.0, 35.0)]) for _ in range(len(w_in))]
    rh = [rnd.uniform(20.0, 100.0) for _ in range(len(w_in))]
    table = [[10, 6.0], [35, 18.0], [60, 31.5], [100, 50.0]]
    variants = [P, replace(P, model_kind="physics"), replace(P, q_table=table),
                replace(P, model_kind="physics", q_table=table, kappa=0.7, eta=0.9, rate_scale=1.2),
                replace(P, b_amb=0.08, b_rh=-0.3, rate_scale=0.8)]
    for par in variants:
        wo, q = solve_out_arr(par, w_in, d, t_proc, t_amb, rh)
        rate = pass_rate_arr(w_in, wo, q)
        for i in range(len(w_in)):
            ps = solve_pass(par, Conditions(t_proc[i], t_amb[i], rh[i]), w_in[i], 1.0, d[i])
            assert abs(ps.w_out - wo[i]) < 1e-9, (par.model_kind, i, ps.w_out, wo[i])
            assert abs(ps.q - q[i]) < 1e-12 and abs(ps.rate - rate[i]) < 1e-9
        dd = [-1.0, 0.0, 3.0, 10.0, 35.0, 59.9, 100.0, 120.0]
        assert all(abs(q_at(par, x) - y) < 1e-12 for x, y in zip(dd, q_at_arr(par, dd)))


def test_physics_batch_matches_scalar_pass_out():
    import random
    from app.physics import PhysBatch, pass_out
    rnd = random.Random(11)
    obs = [(rnd.uniform(12, 38), rnd.uniform(5, 100), rnd.uniform(35, 85), rnd.uniform(-10, 30), rnd.uniform(30, 95))
           for _ in range(300)]
    for eta, kappa in ((0.52, 1.0), (1.7, 0.4), (0.05, 0.9)):
        b = PhysBatch(P, [o[0] for o in obs], [o[1] for o in obs], [o[2] for o in obs], [o[3] for o in obs],
                      [o[4] for o in obs], kappa)
        out = b.out(eta)
        for o, v in zip(obs, out):
            ref = pass_out(P, Conditions(o[2], o[3], o[4]), o[0], o[1], eta, kappa)
            assert abs(ref - v) < 1e-9, (o, ref, v)


# ---------------------------------------------------------------------------
# v0.8: честные допуски, средняя влажность приёмного силоса, поправка влагомера по партиям
# ---------------------------------------------------------------------------

def _sample_rows():
    rows, _ = read_journal(SAMPLE)
    return rows


def test_honest_tolerance_grows_outside_explored():
    from app import model as mdl
    p, rep = calibrate(_sample_rows(), DryerParams())
    assert p.unc_feats[:1] == ["const"] and "b_amb" in p.unc_feats and p.unc_cov and p.explored
    assert 0.0 < p.struct_base < 0.5 and "ДОПУСКИ" in rep["text"]
    # внутри изученного добавки нет, при уходе T, W, погоды — растёт
    assert mdl.param_sigma(p, 22.0, 60.0, 10.0, 70.0) < 0.005
    assert mdl.param_sigma(p, 22.0, 90.0, 10.0, 70.0) > 0.01 > mdl.param_sigma(p, 22.0, 75.0, 10.0, 70.0)
    assert mdl.param_sigma(p, 30.0, 90.0, 10.0, 70.0) > mdl.param_sigma(p, 22.0, 90.0, 10.0, 70.0)
    assert mdl.param_sigma(p, 22.0, 60.0, 0.0, 90.0) > 0.03          # влияние погоды по журналу не подобрано
    # сценарий в 1 проход (D ≈ 12, далеко от журнала) — модели расходятся, допуск шире, чем у 3 проходов
    c = Conditions(60.0, 10.0, 70.0)
    plans = mdl.scenarios(p, c, 500.0, 34.0, 14.0, 4)
    sig = [mdl.plan_sigma(p, pl, c, mdl.other_model_final(p, pl, c)) for pl in plans]
    assert sig[0][0] > 1.5 * sig[2][0] and sig[0][1]["struct"] > 0.03
    assert any("D" in f for f in sig[0][1]["flags"]) and "Допуск 90 %" in mdl.sigma_text(sig[0][1])
    lo0, hi0 = mdl.time_range(lambda pp: mdl.plan_by_passes(pp, c, 500.0, 34.0, 14.0, 1), replace(p, rate_sigma=sig[0][0]))
    lo1, hi1 = mdl.time_range(lambda pp: mdl.plan_by_passes(pp, c, 500.0, 34.0, 14.0, 1), p)
    assert hi0 - lo0 > hi1 - lo1
    # старые сохранённые параметры (без полей v0.8) читаются, добавки нет
    old = DryerParams.from_dict({k: v for k, v in DryerParams().to_dict().items() if not k.startswith("unc_")})
    assert mdl.param_sigma(old, 22.0, 90.0, 0.0, 90.0) == 0.0 and mdl.explored_flags(old, [80], [90], [40]) == []


def test_silo_average_targeting():
    def r(ts, wi=None, wo=None, note=""):
        return {"ts": "25.10.26 " + ts, "w_in": wi, "w_out": wo, "t_a1": 29 if wo else None, "t_a2": 33 if wo else None,
                "t_up": 60, "t_low": 55, "d": 35, "note": note, "batch": "К-07", "pass_no": 1, "t_amb": None, "rh": None}
    rows = [r("08:00", note="Запуск")] + [r(f"{8 + (i + 1) // 2:02d}:{30 * ((i + 1) % 2):02d}", 30.0, 23.0)
                                          for i in range(12)]
    st = live_state(rows, P)
    from app.model import true_from_read
    assert abs(st.silo_w - true_from_read(P, 23.0, 31.0)) < 1e-9 and abs(st.silo_w_read - 23.0) < 1e-9
    c = Conditions(st.t_proc, 10.0, 70.0)
    plain = live_forecast(P, c, st, 400.0, 24.0, 14.0, 6.0, 10.0)
    fc = live_forecast(P, c, st, 400.0, 24.0, 14.0, 6.0, 10.0, silo=True, silo_lim=1.0)
    sl = fc.silo
    assert plain.silo is None and abs(plain.w_goal - 24.0) < 1e-9
    assert sl["need"] > 25.0 and abs(fc.w_goal - 25.0) < 1e-9 and sl["limited"] and fc.d_rec > plain.d_rec
    total = sl["m_done"] + sl["m_tr"] + sl["m_new"]
    assert abs(sl["avg"] - (sl["w_done"] * sl["m_done"] + sl["w_tr"] * sl["m_tr"] + fc.w_out_rec * sl["m_new"]) / total) < 1e-9
    free = live_forecast(P, c, st, 400.0, 24.0, 14.0, 6.0, 10.0, silo=True, silo_lim=5.0)
    assert abs(free.silo["avg"] - 24.0) < 0.1                      # без ограничения среднее выходит в цель
    late = live_forecast(P, c, st, 120.0, 24.0, 14.0, 6.0, 10.0, silo=True)
    assert late.silo["late"] and abs(late.w_goal - 24.0) < 1e-9


def test_meter_bias_from_batches():
    from app.analysis import batch_meter_points, combine_meter
    rows = _sample_rows()
    bs = batches_from_journal(rows)
    pts0 = batch_meter_points([dict(b, w_out=15.0) for b in bs], rows, P)
    assert len(pts0) == 3 and all(x["n"] >= 6 and x["mass"] > 100 for x in pts0)
    # лаборатория = показание − 0,8 → по партиям ровно 0,8; вместе с парами 0,8 — без предупреждения
    bs8 = [dict(b, w_out=x["read"] - 0.8) for b, x in zip(bs, pts0)]
    pts = batch_meter_points(bs8, rows, P)
    pairs = meter_bias([{"reading": 15.0 + d, "ref": 15.0} for d in (0.6, 0.9, 0.8, 1.1, 0.7)])
    comb = combine_meter(pairs, pts)
    assert abs(comb["a_batches"] - 0.8) < 1e-9 and comb["source"] == "пары и партии" and not comb["warn"]
    assert abs(comb["a"] - 0.8) < 0.05 and comb["se"] < pairs["se_a"]
    # партии говорят 2,0, пары 0,8 — это систематика, программа предупреждает
    bs2 = [dict(b, w_out=x["read"] - 2.0) for b, x in zip(bs, pts0)]
    assert combine_meter(pairs, batch_meter_points(bs2, rows, P))["warn"]
    # без пар — только партии; галочка «не учитывать» — только пары
    only_b = combine_meter(meter_bias([]), pts)
    assert only_b["source"] == "партии" and abs(only_b["a"] - 0.8) < 1e-9
    assert combine_meter(pairs, pts, use_batches=False)["source"] == "пары"


# ---------------------------------------------------------------------------
# «Правила работы» (docs/rules_ru.txt): то, что там написано о разборе журнала, должно оставаться правдой.
# Если тест упал после изменения программы — обновите разделы 4–8, 10 и 20 правил.
# ---------------------------------------------------------------------------

def _jr(ts, wi=None, wo=None, d=35, note=""):
    return {"ts": "25.10.26 " + ts, "w_in": wi, "w_out": wo, "t_a1": 29 if wi else None, "t_a2": 33 if wi else None,
            "t_up": 60, "t_low": 55, "d": d, "note": note, "batch": "К-07", "pass_no": 1, "t_amb": None, "rh": None}


def test_rules_doc_journal_claims_hold():
    from app.calibration import chronological_issues, chronological_order, insert_position, note_trap, prepare
    from app.reminders import drying_active
    base = [_jr("08:00", note="Запуск"), _jr("08:30", 32.4, 30.8), _jr("09:00", 32.1, 30.3)]
    segs = lambda rows: [r["seg"] for r in prepare(rows)[0]]  # noqa: E731
    flags = lambda rows, k: [bool(r[k]) for r in prepare(rows)[0]]  # noqa: E731
    # слова-ловушки (раздел 5.3)
    assert flags(base + [_jr("09:30", 32, 29, note="на выходе хорошо охлаждено")], "burner_off")[-1]
    assert flags(base + [_jr("09:30", note="рециркуляция"), _jr("10:00", note="Закончили рециркуляцию")], "recirc")[-1]
    assert not flags(base + [_jr("09:30", note="рециркуляция"), _jr("10:00", note="Запуск подачи с 1 с.")], "recirc")[-1]
    assert segs(base + [_jr("09:30", 32, 29, note="открыли люк, почистили")])[-1] == 1
    for ok_note in ("на выходе холодное", "чистка люка", "вентилятор 2 вместо 1", "отгрузка", "проба", "без пробы",
                    "перемер", "испр.: было 2,1", "D 35→38 по «Сейчас»", "возврат воздуха", "нория стоит"):
        rows = base + [_jr("09:30", 32, 29, note=ok_note)]
        assert segs(rows)[-1] == 0 and not flags(rows, "burner_off")[-1] and not flags(rows, "recirc")[-1], ok_note
    # перерыв > 1,5 ч режет проход, строка «без пробы» — нет (раздел 4.3)
    assert segs(base + [_jr("11:00", 32, 26)])[-1] == 1
    assert segs(base + [_jr("10:00", note="без пробы"), _jr("11:00", 32, 26)])[-1] == 0
    # строка «из прошлого» в конце журнала режет проход (поэтому программа вставляет её на место — раздел 8)
    late = base + [_jr("09:30", 32, 29), _jr("10:00", 32, 28), _jr("08:45", 32, 30), _jr("10:30", 32, 27)]
    assert segs(late)[-1] == 1
    assert chronological_issues(late) == [5]
    fixed = [late[i] for i in chronological_order(late)]
    assert [r["ts"][-5:] for r in fixed] == ["08:00", "08:30", "08:45", "09:00", "09:30", "10:00", "10:30"]
    assert chronological_issues(fixed) == [] and segs(fixed)[-1] == 0
    assert insert_position(late[:5], "25.10.26 08:45") == 2 and insert_position(late[:5], "25.10.26 11:00") == 5
    # «Остановка» без пробы глушит напоминания; после «Запуск» проход продолжается: «выгружено» не обнуляется,
    # время остановки не считается (раздел 6.3)
    stop = base + [_jr("09:10", note="Остановка: силос 1 полный")]
    assert not drying_active(stop, datetime(2026, 10, 25, 9, 40))[0]
    run = stop + [_jr("09:50", note="Запуск"), _jr("10:00", 32, 29), _jr("10:30", 32, 28)]
    st = live_state(run, P)
    assert st.start == datetime(2026, 10, 25, 8, 0)
    assert abs(st.unloaded - q_at(P, 35) * (70 + 40) / 60) < 1e-6, st.unloaded
    assert drying_active(run, datetime(2026, 10, 25, 10, 40))[2] == drying_active(base, datetime(2026, 10, 25, 9, 5))[2]
    # другой номер прохода после остановки — уже новый проход
    other = stop + [dict(_jr("09:50", note="Запуск"), pass_no=2), dict(_jr("10:00", 32, 29), pass_no=2),
                    dict(_jr("10:30", 32, 28), pass_no=2)]
    st2 = live_state(other, P)
    assert st2.start == datetime(2026, 10, 25, 9, 50) and abs(st2.unloaded - q_at(P, 35) * 40 / 60) < 1e-6
    # слова-ловушки: программа переспрашивает (раздел 5.3), явные события — нет
    for trap in ("зерно на выходе хорошо охлаждено", "закончили рециркуляцию", "рециркуляция воздуха",
                 "открыли люк, почистили", "переключили вентилятор", "загрузка машины", "снимаем пробу",
                 "отключили блок управления нории"):
        assert note_trap(trap), trap
    for ev in ("Запуск, сушим с 2 с. в 1 с.", "Закончился 2 с., сушим с 1 с.", "Остановка: силос 1 полный",
               "рециркуляция", "охлаждение", "Откл. т.б.", "Запуск подачи с 1 с.", "открыли с 1 с.",
               "Остановка, закончили партию К-07", "без пробы", "D 35→38 по «Сейчас»", "на выходе холодное"):
        assert note_trap(ev) is None, ev
    # сам файл правил на месте и содержит ключевые разделы
    text = open(os.path.join(ROOT, "docs", "rules_ru.txt"), encoding="utf-8-sig").read()
    for key in ("СЛОВА-ЛОВУШКИ", "ЗАДАНИЕ НА СУШКУ ПАРТИИ", "ЧЕК-ЛИСТЫ", "ОДИН КОМПЬЮТЕР, ОДНА БАЗА"):
        assert key in text, key
    assert max(len(line) for line in text.splitlines()) <= 101


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                import traceback
                print("FAIL", name, "->", repr(e))
                traceback.print_exc()
    sys.exit(1 if failed else 0)
