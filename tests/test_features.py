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
    assert P.out_bias == 0.0 and true_from_read(P, 15.0) == 15.0       # с 1.3 по умолчанию поправки нет
    assert abs(true_from_read(replace(P, out_bias=0.8), 15.0) - 14.2) < 1e-12   # постоянная: показание − a
    q = replace(P, out_bias=0.8, out_kw=0.08, out_kt=0.05)
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
    pb = replace(P, out_bias=0.8)          # выходной влагомер завышает на 0,8: ушедшее зерно суше цели прохода
    st = live_state(rows, pb)
    from app.model import true_from_read
    assert abs(st.silo_w - true_from_read(pb, 23.0, 31.0)) < 1e-9 and abs(st.silo_w_read - 23.0) < 1e-9
    c = Conditions(st.t_proc, 10.0, 70.0)
    plain = live_forecast(pb, c, st, 400.0, 24.0, 14.0, 6.0, 10.0)
    fc = live_forecast(pb, c, st, 400.0, 24.0, 14.0, 6.0, 10.0, silo=True, silo_lim=1.0)
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
# v0.8.1: базы прошлых версий, быстрый ввод следует за журналом, смена режима отдельной строкой
# ---------------------------------------------------------------------------

OLD_DBS = [os.path.join(ROOT, "tests", "data", f"dryer_v{v}.db") for v in ("0.5.0", "0.6.0", "0.7.0")]
COEFS = ("r_ref", "b_w", "b_dt", "b_amb", "b_rh", "eta", "kappa", "calib_info", "phys_info", "out_bias", "m_dryer",
         "lag_k", "q_table", "calibrated")


def test_old_version_databases_keep_journal_and_model():
    """Базы, оставленные версиями 0.5–0.7 (tools/make_old_db.py): новая версия читает журнал, партии, пары
    влагомера и подобранную модель как есть; допуски v0.8 досчитываются без перекалибровки."""
    import json
    import shutil
    from app.calibration import needs_tolerance, tolerance_for
    from app.model import param_sigma
    for src in OLD_DBS:
        db = _tmp("dryer.db")
        shutil.copy(src, db)
        raw = json.loads(sqlite3.connect(src).execute("SELECT value FROM settings WHERE key='params'").fetchone()[0])
        st = Storage(db)
        p = st.load_params()
        assert all(getattr(p, k) == raw[k] for k in COEFS if k in raw), src
        rows = st.load_journal()
        assert len(rows) == 159 and len(st.get_list("batches")) == 3 and len(st.get_list("meter_pairs")) == 5
        assert needs_tolerance(p) and param_sigma(p, 22.0, 90.0, 0.0, 90.0) == 0.0
        q, text = tolerance_for(rows, p)
        assert all(getattr(q, k) == getattr(p, k) for k in COEFS), "коэффициенты модели не должны меняться"
        assert q.unc_cov and q.explored and not needs_tolerance(q) and "сохранена без изменений" in text
        assert param_sigma(q, 22.0, 90.0, 0.0, 90.0) > 0.03 and param_sigma(q, 22.0, 60.0, 10.0, 70.0) < 0.005
        st.save_params(q)
        st.close()
        q2 = Storage(db).load_params()
        assert q2.unc_cov == q.unc_cov and q2.r_ref == p.r_ref


def test_tolerance_for_saved_model_matches_calibration():
    """На том же журнале допуски «без перекалибровки» совпадают с теми, что даёт калибровка; если журнал с тех пор
    пополнился и модель отстала — коэффициенты те же, а разброс честно больше."""
    import numpy as np
    from app.calibration import tolerance_for
    rows = _sample_rows()
    p, _ = calibrate(rows, P)
    old = replace(p, unc_feats=None, unc_cov=None, unc_lev=0.0, explored=None)
    q, _ = tolerance_for(rows, old)
    assert q.unc_feats == p.unc_feats and q.explored == p.explored and abs(q.struct_base - p.struct_base) < 1e-12
    assert np.allclose(q.unc_cov, p.unc_cov, rtol=1e-9, atol=1e-15) and abs(q.unc_lev - p.unc_lev) < 1e-12
    # сушилка «стала сушить» на 15 % быстрее, чем помнит модель: выход ниже на соответствующую величину
    grown = rows + [dict(r, ts=r["ts"].replace("10.26", "11.26").replace(".10.", ".11."),
                         w_out=None if r.get("w_out") is None else r["w_out"] - 0.9) for r in rows]
    q3, _ = tolerance_for(grown, old)
    assert q3.r_ref == old.r_ref and q3.b_w == old.b_w and q3.unc_lev > 1.5 * q.unc_lev


def test_quick_entry_carry_and_next_sample_time():
    from app.regime import journal_carry

    def r(ts, wi=None, wo=None, d=None, tu=None, tl=None, note="", batch="К-07", pass_no=1):
        return {"ts": "25.10.26 " + ts, "w_in": wi, "w_out": wo, "t_a1": None, "t_a2": None, "t_up": tu, "t_low": tl,
                "d": d, "note": note, "batch": batch, "pass_no": pass_no, "t_amb": None, "rh": None}
    rows = [r("09:30", 30, 24, 35, 60, 55), r("10:00", 30, 24)]          # пустые клетки = прежний режим
    vals, nxt = journal_carry(rows)
    assert (vals["d"], vals["t_up"], vals["t_low"], vals["batch"], vals["pass_no"]) == (35, 60, 55, "К-07", 1)
    assert nxt == datetime(2026, 10, 25, 10, 30)
    rows[-1]["d"] = 40                                                    # исправили D в последней строке
    assert journal_carry(rows)[0]["d"] == 40
    # смена режима в 10:05 между пробами — следующая проба по-прежнему 10:30
    vals, nxt = journal_carry(rows + [r("10:05", d=42, note="Режим: D 40→42 с")])
    assert vals["d"] == 42 and nxt == datetime(2026, 10, 25, 10, 30)
    # «Запуск» после долгой остановки — от события
    assert journal_carry(rows + [r("10:10", note="Остановка"), r("12:10", note="Запуск")])[1] == datetime(2026, 10, 25, 12, 40)
    assert journal_carry(rows, 20.0)[1] == datetime(2026, 10, 25, 10, 20)
    assert journal_carry([dict(x, ts="") for x in rows])[1] is None and journal_carry([]) == ({}, None)


def test_regime_change_row_and_fix_of_later_rows():
    from app.calibration import note_trap, observe_all, prepare
    from app.regime import plan_regime_change, regime_note

    def r(ts, wi=None, wo=None, d=None, note=""):
        return {"ts": "25.10.26 " + ts, "w_in": wi, "w_out": wo, "t_a1": None, "t_a2": None, "t_up": 60, "t_low": 55,
                "d": d, "note": note, "batch": "К-07", "pass_no": 2, "t_amb": None, "rh": None}
    rows = [r("09:00", 30, 24, 35), r("09:30", 30, 24, 35), r("10:00", 30, 24, 35), r("10:30", 30, 24, 40),
            r("11:00", 30, 24, None), r("11:30", 30, 24, 40)]
    # вовремя: после последней пробы — в конец, исправлять нечего
    pl = plan_regime_change(rows, datetime(2026, 10, 25, 11, 35), {"d": 44})
    assert pl["pos"] == 6 and pl["changed"] == ["d"] and pl["fixes"] == []
    assert pl["row"]["note"] == "Режим: D 40→44 с" and pl["row"]["batch"] == "К-07" and pl["row"]["pass_no"] == 2
    assert (pl["row"]["d"], pl["row"]["t_up"], pl["row"]["t_low"]) == (44, 60, 55)
    # задним числом: D поменяли в 09:10, а 09:30 и 10:00 записаны со старым 35; в 10:30 уже 40 — дальше не трогаем
    pl = plan_regime_change(rows, datetime(2026, 10, 25, 9, 10), {"d": 38}, "по «Сейчас»")
    assert pl["pos"] == 1 and pl["fixes"] == [(1, "d", 38), (2, "d", 38)]
    assert pl["row"]["note"] == "Режим: D 35→38 с — по «Сейчас»"
    assert plan_regime_change(rows, datetime(2026, 10, 25, 9, 10), {"d": 35})["changed"] == []
    # примечание — не слово-команда: участок не рвётся, строка не идёт в калибровку как проба
    note = regime_note({"d": 35, "t_up": 60, "t_low": 55}, {"d": 38, "t_up": 65, "t_low": None})
    assert note == "Режим: D 35→38 с; T блоков 60/55→65/55 °C" and note_trap(note) is None
    full = rows[:1] + [pl["row"]] + rows[1:]
    prep, _ = prepare(full)
    assert len({x["seg"] for x in prep}) == 1 and len(observe_all(full, P)[0]) == len(observe_all(rows, P)[0])


def test_quick_entry_follows_journal_edits_in_window():
    """Окно: исправили D/T блоков в таблице — быстрый ввод берёт исправленное; своё значение человека не трогает;
    правка D в последней строке с пробой — вопрос «опечатка или режим поменяли»; старая модель — допуски сами."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import shutil
    from PySide6.QtCore import QDateTime
    from PySide6.QtWidgets import QApplication, QDialog, QLineEdit
    app = QApplication.instance() or QApplication([])
    from app import regime, worker
    from app.calibration import FIELDS
    from app.gui import MainWindow
    db = _tmp("dryer.db")
    shutil.copy(OLD_DBS[-1], db)
    st0 = Storage(db)                  # поправку с фото здесь не убираем: проверяется только дополнение допусков
    st0.set_json("meter_photo", {"done": "тест"})
    st0.close()
    win = MainWindow(Storage(db))
    r_ref = win.params.r_ref
    win._upgrade_model()
    worker.wait_all()
    for _ in range(20):
        app.processEvents()
    assert win.params.unc_cov and win.params.r_ref == r_ref and "без изменений" in win.statusBar().currentMessage()
    assert win.qe["d"].text() == "35" and win.qe_time.dateTime().toString("HH:mm") == "07:00"
    last, col_d = win.tbl_j.rowCount() - 1, FIELDS.index("d")

    def edit(i, col, txt):          # правка клетки человеком — через делегат таблицы, как при вводе с клавиатуры
        ed = QLineEdit(txt)
        win.tbl_j.itemDelegate().setModelData(ed, win.tbl_j.model(), win.tbl_j.model().index(i, col))
        for _ in range(5):
            app.processEvents()
        win._qe_sync_timer.stop()
        win._qe_prefill()

    answers = []
    dlg_cls = regime.regime_dialog_class()
    orig = dlg_cls.exec

    def fake_exec(self):
        a = answers.pop(0)
        if a == "typo":
            return dlg_cls.TYPO
        self.ts.setDateTime(QDateTime(a["ts"]))
        for k, v in a.get("vals", {}).items():
            self.ed[k].setText(v)
        return QDialog.Accepted if self.plan and self.plan["changed"] else QDialog.Rejected
    dlg_cls.exec = fake_exec
    try:
        answers.append("typo")
        edit(last, col_d, "40")
        assert win.tbl_j.item(last, col_d).text() == "40" and win.qe["d"].text() == "40" and not answers
        t0 = regime.row_time(win._table_to_rows()[-1])
        answers.append({"ts": t0 + timedelta(minutes=6), "vals": {"t_low": "58"}})
        edit(last, FIELDS.index("t_up"), "70")
        rows = win._table_to_rows()
        assert rows[-2]["t_up"] is None and rows[-2]["d"] == 40           # проба — с прежним режимом
        assert rows[-1]["t_up"] == 70 and rows[-1]["t_low"] == 58 and rows[-1]["note"].startswith("Режим: T блоков")
        assert (win.qe["t_up"].text(), win.qe["t_low"].text()) == ("70", "58")
        assert win.qe_time.dateTime().toPython() == t0 + timedelta(minutes=30)
        win.qe["d"].setText("45")                                             # своё значение для следующей строки
        edit(win.tbl_j.rowCount() - 1, col_d, "42")
        assert win.qe["d"].text() == "45"
    finally:
        dlg_cls.exec = orig
    win.close()


# ---------------------------------------------------------------------------
# v0.9: боковое меню и «Текущий проход» с одним действием
# ---------------------------------------------------------------------------

def _live_rows(d_late=None, change_at=None, note_last=""):
    def r(ts, wi=None, wo=None, d=35, note=""):
        return {"ts": "25.10.26 " + ts, "w_in": wi, "w_out": wo, "t_a1": 29 if wo else None, "t_a2": 33 if wo else None,
                "t_up": 60, "t_low": 55, "d": d, "note": note, "batch": "К-07", "pass_no": 1, "t_amb": None, "rh": None}
    rows = [r("08:00", note="Запуск")]
    for i in range(12):
        h, m = 8 + (i + 1) // 2, 30 * ((i + 1) % 2)
        ts = f"{h:02d}:{m:02d}"
        d = d_late if (change_at is not None and ts >= change_at) else 35
        rows.append(r(ts, 30.0, 23.0, d))
    if note_last:
        rows.append(r("14:05", note=note_last, d=rows[-1]["d"]))
    return rows


def test_live_advice_follows_operating_rules():
    """Совет на «Текущем проходе» — правила работы 7.3: < 3 с — не менять; после смены D — подождите τ/2;
    конец прохода — не менять; рециркуляция и нет данных — куда идти."""
    from app import pages
    from app.live import D_STEP_MIN, live_advice
    st = live_state(_live_rows(), P)
    c = Conditions(st.t_proc, 10.0, 70.0)
    base = live_forecast(P, c, st, 400.0, 24.0, 14.0, 6.0, 10.0)
    keep = live_forecast(P, c, st, 400.0, base.w_out_now, 14.0, 6.0, 10.0)     # цель = то, что выходит сейчас
    a = live_advice(P, st, keep)
    assert abs(keep.d_rec - st.d) < D_STEP_MIN and a["kind"] == "keep" and a["head"] == "D не менять — 35 с"
    far = live_forecast(P, c, st, 400.0, base.w_out_now + 3.0, 14.0, 6.0, 10.0)
    a = live_advice(P, st, far)
    assert a["kind"] == "change" and a["head"] == f"Поставьте D = {far.d_rec:.0f} с" and "Изменить режим" in " ".join(a["sub"])
    # D поменяли в 13:30 (35 → 45): до 13:30 + τ(45)/2 — «подождите», потом — обычный совет
    st2 = live_state(_live_rows(d_late=45, change_at="13:30"), P)
    t_ch = datetime(2026, 10, 25, 13, 30)
    assert st2.d_changed_at == t_ch and st2.d_prev == 35 and st2.d == 45
    fc2 = live_forecast(P, c, st2, 400.0, base.w_out_now + 3.0, 14.0, 6.0, 10.0)
    tau = P.lag_k * P.m_dryer / q_at(P, 45)
    a = live_advice(P, st2, fc2, now=t_ch + timedelta(hours=0.4 * tau))
    assert a["kind"] == "wait" and a["head"].startswith("Подождите до") and "13:30" in a["sub"][0]
    assert live_advice(P, st2, fc2, now=t_ch + timedelta(hours=0.6 * tau))["kind"] == "change"
    # конец прохода: остаток уже в шахте
    late = live_forecast(P, c, st, 120.0, 24.0, 14.0, 6.0, 10.0, silo=True)
    assert late.silo["late"] and live_advice(P, st, late)["head"].startswith("D для этого прохода уже не менять")
    # своё D, рециркуляция, нет данных
    tr = live_forecast(P, c, st, 400.0, 24.0, 14.0, 6.0, 10.0, d_override=40)
    assert live_advice(P, st, tr, d_try=40)["head"].startswith("При D = 40 с")
    st3 = live_state(_live_rows(note_last="рециркуляция"), P)
    fc3 = live_forecast(P, Conditions(st3.t_proc, 10.0, 70.0), st3, 400.0, 24.0, 14.0, 6.0, 10.0)
    a = live_advice(P, st3, fc3)
    assert st3.recirc and a["kind"] == "info" and a["go"] == pages.RECIRC
    a = live_advice(P, live_state([], P), None)
    assert a["kind"] == "nodata" and a["go"] == pages.JOURNAL


def test_plot_clear_really_clears():
    """SimplePlot.clear() очищает данные: раньше PlotWidget перекрывал его своим clear, и при каждом пересчёте серии и
    линии цели копились (старая цель оставалась на графике, график со временем тормозил)."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    import pyqtgraph as pg
    from app.plots import SimplePlot
    p = SimplePlot()
    for k in range(4):
        p.clear()
        p.add_points([0, 1, 2], [20, 22, 24], "dot", 7)
        p.add_hline(24.8 - k, "target")
        for _ in range(3):
            app.processEvents()
    lines = [i.value() for i in p.getPlotItem().vb.addedItems if isinstance(i, pg.InfiniteLine)]
    assert len(p._series) == 1 and lines == [21.8]


def test_nav_panel_sections_and_operator_visibility():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QWidget
    _app = QApplication.instance() or QApplication([])  # noqa: F841
    from app.nav import NavPanel
    nav = NavPanel("DryerCalc", "проба")
    seen = []
    nav.currentChanged.connect(seen.append)
    for name, sec in (("Журнал проб", "Смена"), ("Текущий проход", "Смена"), ("План сушки партии", "Планирование"),
                      ("Справка", None)):
        nav.addTab(QWidget(), name, sec, "назначение " + name)
    assert nav.count() == 4 and nav.currentIndex() == 0 and nav.lbl_title.text() == "Журнал проб"
    assert nav.list.count() == 7                     # 2 заголовка разделов + отступ перед «Справкой» + 4 страницы
    nav.setCurrentIndex(2)
    assert seen == [0, 2] and nav.lbl_sub.text() == "назначение План сушки партии"   # первая страница — сразу текущая
    nav.list.setCurrentRow(0)                        # заголовок раздела выбрать нельзя
    assert nav.currentIndex() == 2
    nav.setTabVisible(2, False)                      # режим сушильщика: «Планирование» пропадает целиком
    assert nav.currentIndex() == 0 and nav._headers["Планирование"].isHidden() and not nav._headers["Смена"].isHidden()
    nav._step(-1)
    assert nav.tabText(nav.currentIndex()) == "Справка"
    nav._step(1)
    assert nav.currentIndex() == 0 and nav.indexOfText("Текущий проход") == 1


# ---------------------------------------------------------------------------
# v0.9.1: все режимы за время прохода зерна через шахту, а не только последний
# ---------------------------------------------------------------------------

def _plug_flow_journal(truth, until_h=14.0, change_d=(6.0, 50), change_t=(8.0, 70), step_t=0.5, minutes=2):
    """«Истинная» сушилка = модель truth, но зерно идёт плотным потоком порциями, у каждой — своя история режимов.
    D 35 → 50 в 06:00, T блоков 60 → 70 в 08:00. Возвращает (журнал проб каждые 30 мин, функция «истина на выходе»)."""
    import numpy as np
    from app.model import d_at, q_at, read_from_true, solve_out_arr
    t0 = datetime(2026, 10, 25, 0, 0)

    def regime(t):
        h = (t - t0).total_seconds() / 3600
        return (35 if h < change_d[0] else change_d[1]), (60 if h < change_t[0] else change_t[1])
    m_shaft = truth.lag_k * truth.m_dryer
    tau0 = m_shaft / q_at(truth, 35)
    parcels = [{"ahead": (k + 0.5) * step_t, "w_in": 30.0, "h": tau0 * (1 - (k + 0.5) * step_t / m_shaft)}
               for k in range(int(m_shaft / step_t))]
    for pc in parcels:
        pc["Ti"] = 60 * pc["h"]
    inflow = lambda t: 30.0 + 2.0 * math.sin((t - t0).total_seconds() / 3600 / 3)  # noqa: E731
    exited, t, dt_h = [], t0, minutes / 60
    while t < t0 + timedelta(hours=until_h + 3):
        d, temp = regime(t)
        dm = q_at(truth, d) * dt_h
        for pc in parcels:
            pc["ahead"] -= dm
            pc["h"] += dt_h
            pc["Ti"] += temp * dt_h
        for pc in [x for x in parcels if x["ahead"] <= 0]:
            w, _ = solve_out_arr(truth, np.array([pc["w_in"]]), np.array([d_at(truth, m_shaft / pc["h"])]),
                                 np.array([pc["Ti"] / pc["h"]]), np.array([10.0]), np.array([70.0]))
            exited.append((t, float(w[0])))
        parcels = [x for x in parcels if x["ahead"] > 0]
        parcels.append({"ahead": (parcels[-1]["ahead"] if parcels else 0) + dm, "w_in": inflow(t), "h": 0.0, "Ti": 0.0})
        t += timedelta(minutes=minutes)

    def truth_at(tk):
        return min(exited, key=lambda e: abs((e[0] - tk).total_seconds()))[1]
    jr = [{"ts": t0.strftime("%d.%m.%y %H:%M"), "w_in": None, "w_out": None, "t_a1": None, "t_a2": None, "t_up": 60,
           "t_low": 60, "d": 35, "note": "Запуск", "batch": "S", "pass_no": 1, "t_amb": None, "rh": None}]
    for k in range(1, int(until_h * 2) + 1):
        tk = t0 + timedelta(minutes=30 * k)
        d, temp = regime(tk)
        jr.append({"ts": tk.strftime("%d.%m.%y %H:%M"), "w_in": round(inflow(tk), 1),
                   "w_out": round(read_from_true(truth, truth_at(tk)), 1), "t_a1": None, "t_a2": None, "t_up": temp,
                   "t_low": temp, "d": d, "note": "", "batch": "S", "pass_no": 1, "t_amb": None, "rh": None})
    return jr, truth_at, regime


def test_residence_follows_all_regimes_in_the_shaft():
    """Проба выхода сопоставляется с режимом, при котором это зерно сушилось (все D и T блоков за его путь через
    шахту), а не с D в строке пробы. На «идеальной» сушилке (истина = модель) ошибка после смены D и T — на уровне
    округления проб, а прежнее сопоставление давало до 2,5 п.п."""
    from app.calibration import observe_all, predict_out, residence
    truth, _ = calibrate(_sample_rows(), P)
    jr, _truth_at, _ = _plug_flow_journal(truth)
    obs, _sk, _ = observe_all(jr, truth)
    w_pred, _ = predict_out(truth, obs)
    errs = [abs(float(a) - o["w_out"]) for a, o in zip(w_pred, obs)]
    assert len(obs) >= 18 and max(errs) < 0.15
    assert any(o["mixed"] for o in obs) and all(o["d_now"] == 50 for o in obs if o["mixed"])
    # чистая функция: постоянный D — как раньше (t − M/Q); смена D — время в шахте другое; охлаждение — нельзя
    tl = [(0.0, 20.0, 60.0, False, 35), (2.0, 30.0, 70.0, False, 50)]
    t_in, tau, t_mean, d1 = residence(tl, 1.5, 20.0)
    assert abs(t_in - 0.5) < 1e-9 and d1 == 35 and abs(t_mean - 60.0) < 1e-9
    t_in, tau, t_mean, d2 = residence(tl, 3.0, 40.0)      # 30 т при D 50 за 1 ч + 10 т при D 35 за 0,5 ч
    assert abs(t_in - 1.5) < 1e-9 and abs(tau - 1.5) < 1e-9 and d2 is None and abs(t_mean - (70 + 60 * 0.5) / 1.5) < 1e-9
    assert residence(tl, 1.0, 60.0) == "ещё идёт прежнее зерно"
    assert residence([(0.0, 20.0, 60.0, True, 35), (2.0, 20.0, 60.0, False, 35)], 3.0, 30.0).startswith("горелки")


def test_transit_profile_predicts_outlet_of_grain_in_shaft():
    """«Текущий проход»: что выйдет в ближайшие часы — по истории каждой порции в шахте. В 09:00 (через 3 ч после
    смены D и 1 ч после смены T) прогноз совпадает с «истиной» до 0,2 п.п.; прежнее допущение «всё в шахте выйдет как
    при текущем режиме» ошибалось на 1,2 п.п."""
    import numpy as np
    from app.live import transit_profile
    from app.model import solve_pass
    truth, _ = calibrate(_sample_rows(), P)
    jr, truth_at, regime = _plug_flow_journal(truth, until_h=9.0)
    st = live_state(jr, truth)
    d, temp = regime(st.last)
    c = Conditions(temp, 10.0, 70.0)
    prof = transit_profile(truth, st, c, d, st.corr, st.last)
    hs = [(x - st.last).total_seconds() / 3600 for x in prof["t"]]
    old = solve_pass(truth, c, st.w_in, 1.0, d).w_out + st.corr
    new_err, old_err = [], []
    for h in (0.5, 1.0, 1.5, 2.0):
        real = truth_at(st.last + timedelta(hours=h))
        new_err.append(abs(float(np.interp(h, hs, prof["w"])) - real))
        old_err.append(abs(old - real))
    assert prof["known"].all() and max(new_err) < 0.2 and max(old_err) > 1.0


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
    passes = lambda rows: [r["pass"] for r in prepare(rows)[0]]  # noqa: E731
    # слова-ловушки (раздел 5.3)
    assert flags(base + [_jr("09:30", 32, 29, note="на выходе хорошо охлаждено")], "burner_off")[-1]
    # конец рециркуляции (раздел 6.4): словами или подачей следующего прохода
    assert not flags(base + [_jr("09:30", note="рециркуляция"), _jr("10:00", note="Закончили рециркуляцию")], "recirc")[-1]
    assert not flags(base + [_jr("09:30", note="рециркуляция"), _jr("10:00", note="Запуск подачи с 1 с.")], "recirc")[-1]
    # переключение силоса внутри прохода (раздел 6.2) не режет ни проход, ни участок; «открыли люк» — безвредно
    for note in ("Закончился 2 с., сушим с 1 с.", "открыли с 3 с.", "переключили на 4 с.", "открыли люк, почистили",
                 "переключили вентилятор", "снимаем пробу"):
        rows = base + [_jr("09:30", 32, 29, note=note), _jr("10:00", 32, 28)]
        assert segs(rows)[-1] == 0 and passes(rows)[-1] == 0, note
    # новый проход — после рециркуляции, когда пошла подача (раздел 6.2); «Проход» +1 — тоже новый проход
    after = base + [_jr("09:30", note="Закончился 2 с., рециркуляция"), _jr("10:00", 30, 29),
                    _jr("10:20", note="сушим с 1 с."), _jr("10:30", 29, 27)]
    assert passes(after) == [0, 0, 0, 0, 0, 1, 1] and flags(after, "recirc") == [False] * 3 + [True, True, False, False]
    assert passes(base + [dict(_jr("09:30", 32, 29), pass_no=2)])[-1] == 1
    for ok_note in ("на выходе холодное", "чистка люка", "вентилятор 2 вместо 1", "отгрузка", "проба", "без пробы",
                    "перемер", "испр.: было 2,1", "D 35→38 по «Сейчас»", "возврат воздуха", "нория стоит"):
        rows = base + [_jr("09:30", 32, 29, note=ok_note)]
        assert segs(rows)[-1] == 0 and not flags(rows, "burner_off")[-1] and not flags(rows, "recirc")[-1], ok_note
    # перерыв > 1,5 ч режет участок (для калибровки), но не проход; строка «без пробы» — ничего (раздел 4.3)
    assert segs(base + [_jr("11:00", 32, 26)])[-1] == 1 and passes(base + [_jr("11:00", 32, 26)])[-1] == 0
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
    for trap in ("зерно на выходе хорошо охлаждено", "рециркуляция воздуха", "загрузка машины",
                 "отключили блок управления нории"):
        assert note_trap(trap), trap
    for ev in ("Запуск, сушим с 2 с. в 1 с.", "Закончился 2 с., сушим с 1 с.", "Остановка: силос 1 полный",
               "рециркуляция", "охлаждение", "Откл. т.б.", "Запуск подачи с 1 с.", "открыли с 1 с.", "сушим со 2 с.",
               "Остановка, закончили партию К-07", "без пробы", "D 35→38 по «Сейчас»", "на выходе холодное",
               "закончили рециркуляцию", "открыли люк, почистили", "переключили вентилятор", "снимаем пробу"):
        assert note_trap(ev) is None, ev
    # сам файл правил на месте и содержит ключевые разделы
    text = open(os.path.join(ROOT, "docs", "rules_ru.txt"), encoding="utf-8-sig").read()
    for key in ("СЛОВА-ЛОВУШКИ", "ЗАДАНИЕ НА СУШКУ ПАРТИИ", "ЧЕК-ЛИСТЫ", "ОДИН КОМПЬЮТЕР, ОДНА БАЗА"):
        assert key in text, key
    assert max(len(line) for line in text.splitlines()) <= 101



# ---------------------------------------------------------------------------
# v1.0: оформление — тёплый минимализм и стекло, тема «как в Windows / светлое / тёмное»
# ---------------------------------------------------------------------------

def _qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_design_tokens_readable_in_both_themes():
    from app import design
    from app.theme import contrast
    for t in (design.LIGHT, design.DARK):
        pairs = [(t.ink, t.paper, 7.0), (t.ink, t.surface, 7.0), (t.ink2, t.surface, 4.5), (t.muted, t.paper, 4.5),
                 (t.muted, t.surface, 4.5), (t.accent_ink, t.accent, 4.5), (t.ink, t.accent_tint, 4.5),
                 (t.rust, t.paper, 4.5), (t.steel, t.surface, 3.0), (t.good_ink, t.good_tint, 4.5),
                 (t.rust_ink, t.rust_tint, 4.5), (t.amber_ink, t.accent_tint, 4.5)]
        bad = [(fg, bg, round(contrast(fg, bg), 2)) for fg, bg, need in pairs if contrast(fg, bg) < need]
        assert not bad, (t.dark, bad)
        assert set(design.token_names()) >= {"paper", "accent", "glass", "series1", "series2"}


def test_design_apply_switches_palette_and_qss():
    from PySide6.QtGui import QPalette
    app = _qapp()
    from app import design
    base = app.font().pointSizeF()
    t = design.apply(app, "dark", base)
    assert t.dark and design.mode() == "dark" and design.style_name() == "fusion"
    assert app.palette().color(QPalette.Window).name().lower() == design.DARK.paper.lower()
    assert design.DARK.accent.lower() in app.styleSheet().lower()
    t = design.apply(app, "light", base)
    assert not t.dark and app.palette().color(QPalette.Window).name().lower() == design.LIGHT.paper.lower()
    assert design.tokens_for(None) is design.LIGHT and design.base_pt() == base
    design.apply(app, "nonsense", base)                       # неизвестный режим — как в Windows
    assert design.mode() == "system"
    design.apply(app, "light", base)


def test_ui_theme_saved_and_restored():
    _qapp()
    from app import design
    from app.gui import MainWindow
    db = _tmp("theme.db")
    win = MainWindow(Storage(db))
    win.set_ui_theme("dark")
    assert win.storage.get_json("ui_theme") == {"mode": "dark"} and design.tokens().dark
    assert win.theme_actions["dark"].isChecked() and not win.theme_actions["light"].isChecked()
    win.close()
    win2 = MainWindow(Storage(db))                            # следующий запуск — то же оформление
    assert design.mode() == "dark" and win2.ui_theme == "dark"
    win2.set_ui_theme("light")
    assert win2.storage.get_json("ui_theme") == {"mode": "light"} and not design.tokens().dark
    win2.close()


def test_live_card_dial_follows_advice():
    _qapp()
    from app.gui import MainWindow
    win = MainWindow(Storage(_tmp("dial.db")))
    lt = win.tab_live
    base = {"sub": ["строка"], "warn": [], "go": None}
    lt._show_advice(dict(base, kind="change", head="Поставьте D = 47 с", d_now=35.0, d_rec=47.0))
    assert lt.dial.state() == ("change", 35.0, 47.0, "было 35") and lt.card.tint() == "gold"
    assert lt.lbl_head.text() == "Поставьте D = 47 с" and lt.lbl_warn.isHidden()
    lt._show_advice(dict(base, kind="keep", head="D не менять — 35 с", d_now=35.0, d_rec=36.0))
    assert lt.dial.state()[:3] == ("keep", 35.0, 35.0) and lt.card.tint() == "green"
    lt._show_advice(dict(base, kind="wait", head="Подождите", d_now=40.0, d_rec=44.0,
                         until=datetime(2026, 10, 25, 9, 30), warn=["Проверьте пробу"]))
    assert lt.dial.state() == ("wait", 40.0, 40.0, "до 09:30") and lt.card.tint() == "steel"
    assert not lt.lbl_warn.isHidden() and "⚠ Проверьте пробу" in lt.lbl_warn.text()
    lt._show_advice(dict(base, kind="nodata", head="Совета пока нет", go="Журнал проб", d_now=None, d_rec=None))
    assert lt.dial.state()[0] == "nodata" and lt.card.tint() is None and lt.btn_go.text() == "Открыть «Журнал проб»"
    # данные прохода и задание — на своей вкладке, рядом с графиком
    assert [lt.views.tabText(i) for i in range(lt.views.count())] == [
        "График проб", "Данные прохода и задание", "Если поставить другое D", "Расчёт подробно"]
    win.close()


def test_plural_passes_and_tiles():
    from app.tab_live import _passes
    assert [_passes(n) for n in (1, 2, 4, 5, 11, 12, 21, 22, 25)] == [
        "1 проход", "2 прохода", "4 прохода", "5 проходов", "11 проходов", "12 проходов", "21 проход", "22 прохода",
        "25 проходов"]
    _qapp()
    from app.ui_common import Tile
    from app.widgets import ProgressLine, Sparkline
    t = Tile("Конец прохода")
    bar = ProgressLine()
    t.set_below(bar)
    t.set("26.10 03:55", "")
    assert t.lbl_sub.isHidden() and t.lbl_value.property("role") == "tileValue"
    bar.set_value(1.7)
    assert bar.value() == 1.0
    sp = Sparkline()
    sp.set_data([0, 1], [23.0, 22.5], 22.0, [1.5], [22.2])
    assert not sp.isHidden()
    sp.set_data([0], [23.0])
    assert sp.isHidden()


def test_glass_shadow_is_soft_and_symmetric():
    _qapp()
    from app.widgets import _shadow_image
    img, corner = _shadow_image(16, 12, (0, 0, 0, 120))
    n = img.width()
    assert n == 2 * corner + 1
    mid = img.pixelColor(n // 2, n // 2).alpha()
    edge = img.pixelColor(0, n // 2).alpha()
    assert mid > 100 and edge < 5                             # под телом — тень, у края картинки — почти ноль
    assert abs(img.pixelColor(3, n // 2).alpha() - img.pixelColor(n - 4, n // 2).alpha()) <= 1


def test_icons_draw_for_every_page():
    _qapp()
    from app import icons
    from app import pages as pg
    for name in set(pg.ICONS.values()) | {"kernel", "sun", "moon", "auto", "chev_right", "chev_down"}:
        pm = icons.pixmap(name, "#4D4840", 20, 2.0)
        img = pm.toImage()
        assert not pm.isNull() and any(img.pixelColor(x, y).alpha() > 0 for x in range(0, 40, 2) for y in range(0, 40, 2)), name
    assert set(pg.ICONS) == {x[0] for x in pg.PAGES}


def test_docs_rendered_as_pages():
    from app.docview import _sentence, to_html
    text = open(os.path.join(ROOT, "docs", "manual_ru.txt"), encoding="utf-8-sig").read()
    h = to_html(text)
    assert "=====" not in h and "<h1" in h and "Инструкция для пользователя" in h
    assert "<a name='s8'></a>" in h and "href='#s8'" in h and "8. Раздел «Текущий проход»" in h
    assert _sentence("СУШИЛЬЩИК: КАК ПОЛЬЗОВАТЬСЯ РАЗДЕЛОМ «ТЕКУЩИЙ ПРОХОД» И PDF") == \
        "Сушильщик: как пользоваться разделом «Текущий проход» и PDF"
    rules = to_html(open(os.path.join(ROOT, "docs", "rules_ru.txt"), encoding="utf-8-sig").read())
    assert "href='#s22'" in rules and "<pre>" in rules


def test_rare_actions_in_menus_and_operator_hides_them():
    _qapp()
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QPushButton
    from app import pilot
    from app.gui import MainWindow
    win = MainWindow(Storage(_tmp("menus.db")))
    assert isinstance(win.j_buttons["Импорт CSV/XLSX…"], QAction) and isinstance(win.j_buttons["Калибровать модель"],
                                                                                 QPushButton)
    assert win.qe_add_btn.property("role") == "primary" and win.j_buttons["Удалить выделенные"].property("role") == "danger"
    win.apply_mode(pilot.OPERATOR)
    assert not win.j_buttons["Импорт CSV/XLSX…"].isVisible() and win.j_buttons["Бланк журнала PDF…"].isVisible()
    win.apply_mode(pilot.ENGINEER)
    assert win.j_buttons["Импорт CSV/XLSX…"].isVisible()
    win.close()



# ---------------------------------------------------------------------------
# v1.1: проходы делит рециркуляция, а не переключение силоса; рециркуляция у цели — досушка и конец партии
# ---------------------------------------------------------------------------

def _silo_journal(change_pass_no=False):
    """Партия в трёх оперативных ёмкостях: проход 1 — из силосов 2 и 4, рециркуляция при 21 %; проход 2 — из
    силоса 5 (номер прохода в журнале не сменили), рециркуляция у цели — досушка; затем охлаждение."""
    t0 = datetime(2026, 11, 2, 8, 0)
    rows = []

    def row(h, wi=None, wo=None, note="", pn=1):
        t = t0 + timedelta(hours=h)
        rows.append({"ts": t.strftime("%d.%m.%y %H:%M"), "w_in": wi, "w_out": wo, "t_a1": 30 if wo else None,
                     "t_a2": 34 if wo else None, "t_up": 60, "t_low": 60, "d": 35, "note": note, "batch": "П-1",
                     "pass_no": pn, "t_amb": None, "rh": None})
    row(0, note="Запуск, сушим с 2 с. в 5 с.")
    for k in range(1, 21):
        row(0.5 * k, 26.0, 21.8)
    row(10.2, note="Закончился 2 с., сушим с 4 с.")
    for k in range(21, 41):
        row(0.5 * k, 26.5, 21.6)
    row(20.2, note="Закончился 4 с., рециркуляция")
    row(20.5, 21.0, 20.2)
    row(21.0, 20.5, 19.8)
    pn2 = 2 if change_pass_no else 1
    row(21.4, note="сушим с 5 с. в 2 с.", pn=pn2)
    for k in range(44, 80):
        row(0.5 * k, 21.0, 14.7, pn=pn2)
    return rows, row, t0


def test_passes_split_by_recirculation_not_by_silo_switch():
    from app import pages
    from app.calibration import journal_passes, observe_all, prepare
    from app.live import live_advice
    from app.reminders import drying_active
    rows, row, t0 = _silo_journal()
    # проход 1 до рециркуляции: переключение силоса 2 → 4 не обнуляет «выгружено» и среднюю по силосу
    cut = [r for r in rows if r["ts"] < (t0 + timedelta(hours=19)).strftime("%d.%m.%y %H:%M")]
    st1 = live_state(cut, P, final_w=15.0)
    assert st1.start == t0 and abs(st1.unloaded - q_at(P, 35) * 18.5) < 1e-6 and st1.sources == ["2", "4"]
    assert 21.5 < st1.silo_w < 21.9 and st1.silo_n == 37          # без поправки влагомера: средняя показаний
    # подача после рециркуляции — новый проход, номер посчитан сам (в журнале «Проход» не сменили)
    st2 = live_state(rows, P, final_w=15.0)
    assert st2.pass_no == 2 and st2.pass_auto and st2.start == t0 + timedelta(hours=21.4) and not st2.recirc
    assert abs(st2.unloaded - q_at(P, 35) * (39.5 - 21.4)) < 1e-6 and st2.sources == ["5"]
    # рециркуляция у цели партии — досушка и конец партии
    row(40.2, note="Закончился 5 с., рециркуляция")
    row(40.5, 14.6, 14.2)
    prep, _ = prepare(rows)
    ps = journal_passes(prep, P, 15.0)
    assert [(x["pass_no"], x["end_kind"], x["batch_end"]) for x in ps] == [(1, "recirc", False), (2, "recirc", True)]
    assert 21.3 < ps[0]["w_end"] < 21.9 and ps[1]["w_end"] < 15.0 and ps[0]["sources"] == ["2", "4"]
    st3 = live_state(rows, P, final_w=15.0)
    assert st3.recirc and st3.batch_end and st3.recirc_since == t0 + timedelta(hours=40.2)
    fc3 = live_forecast(P, Conditions(st3.t_proc, 10.0, 70.0), st3, 1000.0, 14.5, 14.0, 6.0, 10.0)
    a = live_advice(P, st3, fc3)
    assert a["head"] == "Партия высушена — идёт досушка" and a["go"] == pages.RECIRC
    # при другой цели партии (Wк 12 % → порог 13 %) та же рециркуляция — конец прохода, не партии
    st4 = live_state(rows, P, final_w=13.0)
    assert st4.recirc and not st4.batch_end
    assert live_advice(P, st4, fc3)["head"] == "Проход закончен — идёт рециркуляция"
    # охлаждение после досушки, следующая партия без названия — новая партия, проход 1
    row(41.0, note="Откл. т.б., охлаждение")
    row(42.0, note="Запуск, сушим с 3 с. в 1 с.", pn=None)
    for k in range(85, 90):
        row(0.5 * k, 30.0, 26.0, pn=None)
    for r in rows[-6:]:
        r["batch"] = ""
    prep, _ = prepare(rows)
    ps = journal_passes(prep, P, 15.0)
    assert [(x["batch_no"], x["pass_no"], x["end_kind"]) for x in ps] == [(1, 1, "recirc"), (1, 2, "cool"), (2, 1, None)]
    # переключение силоса без пробы — сушка идёт, напоминания не затихают; рециркуляция в калибровку не идёт
    sw = cut + [{"ts": (t0 + timedelta(hours=18.6)).strftime("%d.%m.%y %H:%M"), "note": "Закончился 4 с., сушим с 1 с.",
                 "batch": "П-1", "pass_no": 1}]
    assert drying_active(sw, t0 + timedelta(hours=18.8))[0]
    obs = observe_all(rows, P)[0]
    assert not any(t0 + timedelta(hours=20.2) <= o["dt"] < t0 + timedelta(hours=21.4) for o in obs)
    # с номером прохода, вписанным как надо, — то же самое, только номер из журнала
    rows2, _, _ = _silo_journal(change_pass_no=True)
    st5 = live_state(rows2, P, final_w=15.0)
    assert st5.pass_no == 2 and not st5.pass_auto and st5.start == t0 + timedelta(hours=21.4)


def test_sample_journal_passes_unchanged():
    """Журнал октября: те же четыре прохода и та же калибровка, что до перехода на «проход делит рециркуляция»
    (в нём новый проход всегда отмечен сменой «Партия» / «Проход»)."""
    from app.calibration import journal_passes, prepare
    rows, _ = read_journal(SAMPLE)
    prep, _ = prepare(rows)
    ps = journal_passes(prep, P)
    assert [(x["batch"], x["pass_no"], x["end_kind"], x["batch_end"]) for x in ps] == [
        ("A", 3, "cool", True), ("B", 2, None, False), ("B", 3, "cool", True), ("C", 1, None, False)]
    p, rep = calibrate(rows, P)
    assert rep["n_used"] == 120


def test_startup_with_saved_settings_raises_nothing():
    """1.1.0 падала при запуске, если в настройках сохранена цель партии Wк не по умолчанию: смена Wк при загрузке
    настроек пересчитывала проход раньше, чем построен журнал. Запуск с сохранёнными настройками всех разделов —
    без единой ошибки (ошибки в обработчиках Qt идут в sys.excepthook, а не наружу)."""
    import shutil
    app = _qapp()
    from app.gui import MainWindow
    errors = []
    old_hook = sys.excepthook
    sys.excepthook = lambda *a: errors.append(a)
    try:
        db = _tmp("saved.db")
        shutil.copy(OLD_DBS[-1], db)
        st = Storage(db)
        st.set_json("ui_live", {"m_pass": 900.0, "w_target": 15.5, "wk": 13.5, "t_amb": 4.0, "rh": 75.0,
                                "corr": False, "silo": True, "silo_lim": 15.0})
        st.set_json("ui_simple", {"mass": 650.0, "w0": 27.0, "wk": 13.5, "lim": 6.0, "n": 3, "over": 0.5,
                                  "cool": True, "mode": "manual", "q": "5"})
        st.set_json("ui_queue", {"method": "q", "q": 40.0, "lim": 6.0, "change": 20.0, "cool": True})
        st.set_json("ui_theme", {"mode": "dark"})
        st.set_json("meter_photo", {"done": "тест"})
        win = MainWindow(st)
        lt = win.tab_live
        assert lt.wk_final.value() == 13.5 and lt.m_pass.value() == 900.0
        win.tabs.setCurrentIndex([win.tabs.tabText(i) for i in range(win.tabs.count())].index("Текущий проход"))
        lt.refresh()
        lt.wk_final.setValue(14.0)                    # после запуска смена Wк пересчитывает проход
        for _ in range(10):
            app.processEvents()
        assert lt.state is not None and win.storage.get_json("ui_live")["wk"] == 14.0
        for i in range(win.tabs.count()):            # и все разделы открываются без ошибок
            win.tabs.setCurrentIndex(i)
            for _ in range(3):
                app.processEvents()
        win.close()
    finally:
        sys.excepthook = old_hook
        from app import design
        design.apply(app, "light", design.base_pt())
    assert not errors, "".join(str(e[1]) for e in errors)


def test_pilot_stats_with_single_sample_per_pass():
    """1.0.0: «Пилотный запуск» падал, если в проходах за период по одной пробе — интервалов между пробами нет."""
    from app.pilot import export_xlsx, pilot_stats, stats_text
    st = Storage(_tmp("p1.db"))
    t0 = datetime(2026, 11, 4, 16, 0)
    rows = [{"ts": t.strftime("%d.%m.%y %H:%M"), "w_in": wi, "w_out": wo, "t_a1": 30 if wo else None,
             "t_a2": 34 if wo else None, "t_up": 60, "t_low": 60, "d": 35, "note": note, "batch": "П-1",
             "pass_no": 1, "t_amb": None, "rh": None}
            for t, wi, wo, note in ((t0, None, None, "Запуск"), (t0 + timedelta(minutes=30), 26.0, 21.8, ""))]
    st.log_event("start", {"mode": "operator"}, ts=t0)
    st.log_event("sample", {}, ts=t0 + timedelta(minutes=31))
    stt = pilot_stats(st.load_events(), rows)
    assert stt["coverage"]["expected"] == 1 and stt["coverage"]["median_interval"] is None
    text = stats_text(stt)
    assert "1 из ожидаемых 1" in text and "интервалов между пробами пока нет" in text
    export_xlsx(_tmp("p1.xlsx"), stt, st.load_events())
    st.close()


# ---------------------------------------------------------------------------
# v1.2: масса на выходные
# ---------------------------------------------------------------------------

def test_weekend_mass_fills_window_exactly():
    """Масса на выходные: партия со всеми проходами, охлаждением и остановками ровно заполняет окно; число
    проходов — как у рекомендованного сценария «План сушки партии»; допуск — та же схема при скорости ×(1 ± zσ)."""
    from app import model as mdl
    from app.weekend import WeekendInput, weekend_mass
    c = Conditions(60, 8, 75)
    st, en = datetime(2026, 11, 6, 18, 0), datetime(2026, 11, 9, 8, 0)
    for kind in ("empirical", "physics"):
        p = replace(P, model_kind=kind)
        r = weekend_mass(p, c, WeekendInput(st, en, 28.0, 14.0, stops_h=2.0))
        assert r.ok and r.hours == 60.0 and abs(r.plan.total_time_h - 60.0) < 0.02
        opt = {o.key: o for o in r.options}
        assert set(opt) == {"safe", "calc", "full"} and abs((opt["calc"].end - en).total_seconds()) < 90
        assert r.mass_lo < r.mass < r.mass_hi
        assert abs((opt["safe"].end_slow - en).total_seconds()) < 90 and opt["safe"].end < en   # успеет и при медленной
        assert abs((opt["full"].end_fast - en).total_seconds()) < 90 and opt["full"].end > en   # не встанет и при быстрой
        plans = mdl.scenarios(p, c, r.mass, 28.0, 14.0, 6, st, None, True)
        assert len(plans[mdl.recommend(plans, 6.0, 10.0)].passes) == r.n_passes == len(r.plan.passes)
        # время по прямой: T(M) = a·M + b
        assert abs(r.h_per_t * r.mass + r.fixed_h - 60.0) < 0.02 and abs(r.fixed_h - r.n_passes * p.overhead_h) < 1e-6
        assert r.pass1_end == st + timedelta(hours=r.plan.passes[0].time_h + 2.0)


def test_weekend_mass_by_throughput_closed_form():
    """Метод «по производительности»: масса из формулы вручную — Σ масса на выходе прохода / Q + охлаждение."""
    from app.model import q_full
    from app.weekend import WeekendInput, weekend_mass
    st, en = datetime(2026, 11, 6, 18, 0), datetime(2026, 11, 9, 8, 0)
    r = weekend_mass(P, Conditions(), WeekendInput(st, en, 28.0, 14.0, method="q", q=18.2, lim=6.0))
    n = 3                                               # 14 п.п. при лимите 6 — 3 прохода по 4,67
    ws = [28.0 - 14.0 * k / n for k in range(n + 1)]
    f = [(100 - ws[0]) / (100 - w) for w in ws[1:]]     # масса на выходе прохода на 1 т сырого
    a = sum(x / 18.2 for x in f) + f[-1] / q_full(P)
    m = (62.0 - n * P.overhead_h) / a
    assert r.ok and r.n_passes == n and abs(r.mass - m) < 0.01 and r.mass_lo is None
    assert [o.key for o in r.options] == ["calc"] and r.options[0].end_fast is None


def test_weekend_capacity_short_window_and_weekday():
    from app.weekend import WeekendInput, next_weekday_time, weekend_mass
    c = Conditions(60, 8, 75)
    st, en = datetime(2026, 11, 6, 18, 0), datetime(2026, 11, 9, 8, 0)
    r = weekend_mass(P, c, WeekendInput(st, en, 28.0, 14.0, capacity=300.0))
    bins = next(o for o in r.options if o.key == "bins")
    assert r.capacity_short > 0 and bins.mass == 300.0 and bins.end < en
    r2 = weekend_mass(P, c, WeekendInput(st, st + timedelta(hours=2), 28.0, 14.0))
    assert not r2.ok and "накладных" in r2.message
    assert not weekend_mass(P, c, WeekendInput(en, st, 28.0, 14.0)).ok
    assert not weekend_mass(P, c, WeekendInput(st, en, 14.0, 14.0)).ok
    assert next_weekday_time(datetime(2026, 11, 6, 18, 0), 0, 8) == datetime(2026, 11, 9, 8, 0)
    assert next_weekday_time(datetime(2026, 11, 9, 7, 0), 0, 8) == datetime(2026, 11, 9, 8, 0)
    assert next_weekday_time(datetime(2026, 11, 9, 9, 0), 0, 8) == datetime(2026, 11, 16, 8, 0)


def test_allowable_storage_of_wet_corn_follows_table():
    """Допустимый срок хранения сырой кукурузы: узлы таблицы FSA1058 точно, между ними — монотонно."""
    from app.weekend import STORAGE_DAYS, STORAGE_MC, STORAGE_TF, allowable_storage_days

    def c_of(f):
        return (f - 32) * 5 / 9
    for i, tf in enumerate(STORAGE_TF):
        for j, mc in enumerate(STORAGE_MC):
            d, flag = allowable_storage_days(mc, c_of(tf))
            assert abs(d - STORAGE_DAYS[i][j]) < 1e-6 * STORAGE_DAYS[i][j] and flag == ""
    assert abs(allowable_storage_days(25, c_of(60))[0] - 10) < 1e-6 and abs(allowable_storage_days(30, c_of(70))[0] - 4) < 1e-6
    prev = None
    for w in (16, 20, 24, 27, 30):                      # влажнее — меньше
        d = allowable_storage_days(w, 12.0)[0]
        assert prev is None or d < prev
        prev = d
    prev = None
    for t in (0, 5, 10, 15, 20, 23):                    # теплее — меньше
        d = allowable_storage_days(26, t)[0]
        assert prev is None or d < prev
        prev = d
    assert allowable_storage_days(26, -10)[0] == allowable_storage_days(26, c_of(35))[0]   # холоднее таблицы — с запасом
    hot, flag = allowable_storage_days(30, 30.0)
    assert hot < 3 and "T выше" in flag and "30 %" in allowable_storage_days(33, 10)[1]
    assert allowable_storage_days(14, 20)[0] is None


def test_weekend_tab_ui_queue_and_settings():
    """Раздел «Масса на выходные»: начало — ближайший час, если нет ни журнала, ни очереди; расчётная масса уходит
    в очередь; при следующем открытии начало — после очереди, день и час выхода хозяйства запомнены."""
    app = _qapp()
    from PySide6.QtCore import QDateTime
    from app import icons, pages
    from app.gui import MainWindow
    from app.weekend import next_weekday_time
    errors = []
    old_hook = sys.excepthook
    sys.excepthook = lambda *a: errors.append(a)
    try:
        db = _tmp("weekend.db")
        win = MainWindow(Storage(db))
        names = [win.tabs.tabText(i) for i in range(win.tabs.count())]
        assert pages.WEEKEND in names and "weekend" in icons.names()
        win.tabs.setCurrentIndex(names.index(pages.WEEKEND))
        wt = win.tab_weekend
        assert "ближайший час" in wt.lbl_start_src.text() and not wt.b_queue.isVisibleTo(win)
        st = next_weekday_time(datetime.now(), 4, 18)          # ближайшая пятница 18:00
        sun = next_weekday_time(st, 6, 20)                     # человек выбрал воскресенье 20:00
        wt.start.setDateTime(QDateTime(st.year, st.month, st.day, st.hour, 0, 0))
        wt.end.setDateTime(QDateTime(sun.year, sun.month, sun.day, sun.hour, 0, 0))
        wt.w0.setValue(28.0)
        wt.wk.setValue(14.0)
        wt.already.setValue(200.0)
        r = wt.result
        assert r.ok and abs(r.hours - 50.0) < 1e-6 and wt.lbl_head.text().startswith("Намолотить ≈")
        assert "домолотить ещё" in wt.lbl_sub.text() and wt.tbl_opt.rowCount() == len(r.options) == 3
        assert len(wt.gantt.entries) == 3 and wt.gantt.marks[0][0] == sun
        assert "задано вручную" in wt.lbl_start_src.text()
        wt._add_to_queue()
        q = win.tab_batches._queue()
        assert len(q) == 1 and q[0]["name"] == f"Выходные {st:%d.%m}" and q[0]["mass"] == round(r.mass / 10) * 10
        assert q[0]["ready"] == f"{st:%d.%m.%Y %H:%M}"
        q_end = win.tab_batches.queue_end()
        assert q_end is not None and q_end > st
        d = win.storage.get_json("ui_weekend")
        assert d["w0"] == 28.0 and (d["ret_dow"], d["ret_h"], d["ret_m"]) == (6, 20, 0)
        win.close()
        win2 = MainWindow(Storage(db))                       # следующий раз: начало — после очереди
        win2.tabs.setCurrentIndex(names.index(pages.WEEKEND))
        wt2 = win2.tab_weekend
        assert wt2.b_queue.isVisibleTo(win2) and "очереди" in wt2.lbl_start_src.text()
        s2 = wt2.start.dateTime().toPython()
        assert abs((s2 - win2.tab_batches.queue_end()).total_seconds()) < 60
        e2 = wt2.end.dateTime().toPython()
        assert (e2.weekday(), e2.hour, e2.minute) == (6, 20, 0) and e2 > s2 and wt2.w0.value() == 28.0
        for _ in range(5):
            app.processEvents()
        win2.close()
    finally:
        sys.excepthook = old_hook
    assert not errors, "".join(str(e[1]) for e in errors)


# ---------------------------------------------------------------------------
# v1.3: поправка влагомера — только внесённая; число проходов задаётся вручную
# ---------------------------------------------------------------------------

def test_photo_meter_bias_removed_once_with_recalibration():
    """Поправка +0,8 с фото журнала октября убирается один раз, и модель перекалибровывается по журналу без неё (как
    «Калибровать модель»); поправку, внесённую по парам проб, не трогаем; стартовая модель без журнала — новая
    стартовая без поправки."""
    import shutil
    app = _qapp()
    from app import worker
    from app.gui import MainWindow
    db = _tmp("photo.db")
    shutil.copy(OLD_DBS[-1], db)
    win = MainWindow(Storage(db))
    old = win.params
    assert old.out_bias == 0.8 and win._has_photo_bias()
    win._startup_model()
    worker.wait_all()
    for _ in range(30):
        app.processEvents()
    p = win.params
    ref, _rep = calibrate(win.journal_rows(), replace(old, out_bias=0.0), half_life_days=win.tab_check.half_life(),
                          weather=win.weather_series())
    assert p.out_bias == 0.0 and abs(p.r_ref - ref.r_ref) < 1e-9 and p.r_ref < 0.95 * old.r_ref and p.calibrated
    assert "фото" in p.calib_info and "убрана" in win.statusBar().currentMessage()
    assert win.storage.get_json("meter_photo")["how"] == "перекалибровано" and not win._has_photo_bias()
    win.close()
    _cleanup_windows()
    win2 = MainWindow(Storage(db))                      # второй раз не трогает
    assert win2.params.out_bias == 0.0 and not win2._has_photo_bias()
    win2.close()
    _cleanup_windows()
    db3 = _tmp("pairs.db")                              # поправка по парам проб — своя
    shutil.copy(OLD_DBS[-1], db3)
    st = Storage(db3)
    st.save_params(replace(st.load_params(), out_bias=0.82))
    st.close()
    win3 = MainWindow(Storage(db3))
    assert not win3._has_photo_bias()
    win3.close()
    _cleanup_windows()
    db4 = _tmp("start.db")                              # журнала нет, модель стартовая
    st = Storage(db4)
    st.save_params(replace(DryerParams(), out_bias=0.8, r_ref=1.83, b_w=0.69, b_dt=0.87, eta=0.52,
                           calib_info="стартовая: журнал 18–25.10.2026, 4 прохода, ошибка съёма ±0,14 п.п."))
    st.close()
    win4 = MainWindow(Storage(db4))
    win4._startup_model()
    d = DryerParams()
    q = win4.params
    assert q.out_bias == 0.0 and (q.r_ref, q.b_w, q.eta, q.calib_info) == (d.r_ref, d.b_w, d.eta, d.calib_info)
    win4.close()


def test_default_start_model_is_calibrated_without_meter_bias():
    """Стартовая модель — калибровка журнала октября без поправки влагомера (с поправкой +0,8 R₀ было бы на ≈14 %
    больше: показание выше истинной — съём меньше)."""
    rows, _ = read_journal(SAMPLE)
    p, rep = calibrate(rows, P)
    assert P.out_bias == 0.0 and rep["n_used"] == 120
    for k in ("r_ref", "b_w", "b_dt", "eta"):
        assert abs(getattr(p, k) - getattr(P, k)) < 0.01, k
    with_bias, _ = calibrate(rows, replace(P, out_bias=0.8))
    assert with_bias.r_ref > 1.1 * p.r_ref


def test_plan_pass_count_set_by_hand():
    """«Число проходов» в «План сушки партии»: с 35 % в 3 прохода — ★ этот сценарий, хотя съём 7 п.п. больше
    желаемых 6; цвет и пояснение говорят, в каком он лимите. Сохраняется и восстанавливается."""
    app = _qapp()
    from app.gui import MainWindow
    db = _tmp("npass.db")
    win = MainWindow(Storage(db))
    win.in_w0.setValue(35.0)
    win.in_wk.setValue(14.0)
    auto = [pl for pl in win.plans if "★" in win.tbl_sc.item(win.plans.index(pl), 0).text()]
    assert len(auto[0].passes) == 4                     # авто: 21 п.п. при желаемых 6 — 4 прохода
    win.in_npass.setValue(3)
    rows = [i for i in range(win.tbl_sc.rowCount()) if "★" in win.tbl_sc.item(i, 0).text()]
    assert len(rows) == 1 and len(win.plans[rows[0]].passes) == 3 and "задано" in win.tbl_sc.item(rows[0], 0).text()
    assert win.tbl_sc.selectionModel().selectedRows()[0].row() == rows[0]
    assert all(abs(x.dw - 7.0) < 0.3 for x in win.plans[rows[0]].passes)      # D — целые секунды
    assert "Число проходов задано: 3" in win.status.text() and "больше желаемого 6" in win.status.text()
    assert win.plan_passes() == 3
    win.tab_batches._q_add()                            # строка очереди из плана — с заданным числом проходов
    assert win.tab_batches._queue()[-1]["passes"] == 3
    for _ in range(3):
        app.processEvents()
    win.close()
    win2 = MainWindow(Storage(db))
    assert win2.in_npass.value() == 3 and win2.plan_passes() == 3
    win2.in_npass.setValue(0)
    assert win2.plan_passes() is None
    win2.close()


def test_pass_count_in_weekend_queue_and_live():
    """Заданное число проходов — и в «Масса на выходные», и в очереди партий, и в «Текущий проход» (остаток партии:
    заданное число минус номер текущего прохода)."""
    from app.weekend import WeekendInput, weekend_mass
    c = Conditions(60, 8, 75)
    st, en = datetime(2026, 11, 6, 18, 0), datetime(2026, 11, 9, 8, 0)
    auto = weekend_mass(P, c, WeekendInput(st, en, 35.0, 14.0))
    three = weekend_mass(P, c, WeekendInput(st, en, 35.0, 14.0, passes=3))
    assert auto.n_passes == 4 and three.n_passes == 3 and len(three.plan.passes) == 3 and not three.notes
    assert abs(three.plan.total_time_h - 62.0) < 0.02
    two = weekend_mass(P, c, WeekendInput(st, en, 35.0, 14.0, passes=2))
    assert two.n_passes == 2 and "больше допустимого 10" in two.notes[0]
    q3 = weekend_mass(P, c, WeekendInput(st, en, 35.0, 14.0, method="q", passes=3))
    assert q3.n_passes == 3 and "лимита" in q3.notes[0]          # 7 п.п. при лимите 6
    items = [{"name": "А", "mass": 500, "w0": 35, "wk": 14, "passes": 3}, {"name": "Б", "mass": 500, "w0": 35, "wk": 14}]
    es = schedule(items, P, c, st)
    assert [e.passes for e in es] == [3, 4] and "задано: 3" in es[0].note
    es_q = schedule(items, P, c, st, method="q", lim=6.0)
    assert [e.passes for e in es_q] == [3, 4]
    rows, _row, t0 = _silo_journal()
    cut = [r for r in rows if r["ts"] < (t0 + timedelta(hours=19)).strftime("%d.%m.%y %H:%M")]
    s1 = live_state(cut, P, final_w=15.0)
    cc = Conditions(s1.t_proc, 10.0, 70.0)
    fa = live_forecast(P, cc, s1, 1000.0, 21.0, 14.0, 6.0, 10.0)
    f4 = live_forecast(P, cc, s1, 1000.0, 21.0, 14.0, 6.0, 10.0, n_total=4)
    f1 = live_forecast(P, cc, s1, 1000.0, 21.0, 14.0, 6.0, 10.0, n_total=1)
    assert s1.pass_no == 1 and len(fa.rest_plan.passes) == 2 and len(f4.rest_plan.passes) == 3
    assert f4.batch_end > fa.batch_end and any("последний" in w for w in f1.warnings)
    assert len(f1.rest_plan.passes) == len(fa.rest_plan.passes)


def _cleanup_windows():
    """Окна прошлых тестов удаляются: каждое новое окно ставит оформление на всё приложение и перерисовывает все
    живые окна — без уборки тесты с окнами замедляются квадратично (в программе окно одно)."""
    if "app.gui" not in sys.modules:
        return
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    from app import worker
    from app.gui import MainWindow
    qa = QApplication.instance()
    if qa is None:
        return
    worker.wait_all()
    qa.processEvents()
    for w in qa.topLevelWidgets():
        if isinstance(w, MainWindow):
            for t in w.findChildren(type(w._save_timer)):
                t.stop()
            w.hide()
            w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


try:                                    # под pytest — после каждого теста; без pytest — в цикле ниже
    import pytest

    @pytest.fixture(autouse=True)
    def _windows_cleanup():
        yield
        _cleanup_windows()
except ImportError:
    pass


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                _cleanup_windows()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                import traceback
                print("FAIL", name, "->", repr(e))
                traceback.print_exc()
    sys.exit(1 if failed else 0)
