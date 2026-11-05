"""Проверки ядра. Запуск: python tests/test_model.py   или   python -m pytest tests"""
import csv
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.calibration import (assign_times, calibrate, map_header, observe_all, parse_pair,  # noqa: E402
                             parse_ts, prepare, read_journal, to_float, write_template)
from app.model import (Conditions, DryerParams, d_at, d_for_target, plan_by_passes, plan_manual,  # noqa: E402
                       plan_simple, q_at, q_full, recommend, scenarios, solve_pass, water_rate)

P = DryerParams()
C = Conditions(60.0, 10.0, 70.0)
SAMPLE = os.path.join(ROOT, "examples", "journal_sample_oct2026.csv")


# ---------------------------------------------------------------------------
# Q(D)
# ---------------------------------------------------------------------------

def test_q_of_d_matches_sheet():
    assert abs(q_at(P, 100) - 52.0) < 1e-9
    assert abs(q_at(P, 35) - 18.2) < 1e-9          # «норма» сушильщиков: D=35 ≈ 18,2 т/ч
    assert abs(q_full(P) - 52.0) < 1e-9
    assert abs(d_at(P, 18.2) - 35.0) < 1e-9


def test_q_table_interpolation_and_inverse():
    p = DryerParams(q_table=[[10, 4.0], [50, 28.0], [100, 52.0]])
    assert abs(q_at(p, 50) - 28.0) < 1e-9
    assert abs(q_at(p, 30) - 16.0) < 1e-9          # линейно между 10 и 50
    assert abs(d_at(p, 16.0) - 30.0) < 1e-9
    assert abs(q_full(p) - 52.0) < 1e-9
    assert q_at(p, 5) < 4.0                         # ниже первой точки — к нулю
    # немонотонная таблица игнорируется (линейная от q100)
    bad = DryerParams(q_table=[[10, 8.0], [50, 6.0]])
    assert abs(q_at(bad, 50) - 26.0) < 1e-9


# ---------------------------------------------------------------------------
# Решатель прохода и планы
# ---------------------------------------------------------------------------

def test_solver_balance():
    ps = solve_pass(P, C, 34.0, 500.0, 80.0)
    q = q_at(P, 80.0)
    lhs = q * (34.0 - ps.w_out) / (100.0 - 34.0)
    rhs = water_rate(P, 0.5 * (34.0 + ps.w_out), C)
    assert abs(lhs - rhs) < 1e-6
    assert abs(ps.m_out * (100 - ps.w_out) - 500.0 * (100 - 34.0)) < 1e-6   # сухое вещество сохраняется
    assert ps.w_out < 34.0


def test_lower_d_removes_more():
    a = solve_pass(P, C, 30.0, 500.0, 100.0).w_out
    b = solve_pass(P, C, 30.0, 500.0, 50.0).w_out
    assert b < a


def test_d35_removal_in_realistic_range():
    """При D=35 (18,2 т/ч) и влажности 20–33 % съём за проход должен быть в границах журнала (4–11 п.п.)."""
    for w_in in (20.0, 25.0, 30.0, 33.0):
        dw = w_in - solve_pass(P, Conditions(60, 10, 70), w_in, 400.0, 35).w_out
        assert 3.5 <= dw <= 11.5, (w_in, dw)


def test_d_for_target_inverse():
    for w_in, target in [(34, 28), (28, 22), (22, 16), (18, 14)]:
        d = d_for_target(P, C, w_in, target)
        if 1.0 <= d <= 100.0:
            assert abs(solve_pass(P, C, w_in, 100.0, d).w_out - target) < 1e-4


def test_plan_mass_balance_and_target():
    plan = plan_by_passes(P, C, 500.0, 34.0, 14.0, 6)
    assert plan.reaches_target
    expected_water = 500.0 - 500.0 * (100 - 34.0) / (100 - plan.w_final)
    assert abs(plan.water_total - expected_water) < 1e-6
    for ps in plan.passes:
        assert abs(ps.time_h * ps.rate - ps.water) < 1e-9
        assert abs(ps.time_h - ps.m_out / ps.q) < 1e-9          # время прохода = масса выхода / Q


def test_pass_time_is_mass_out_over_q():
    """Проход ≈ 400 т при D=35 (18,2 т/ч): m/Q ≈ 19–22 ч; по журналу проходы идут 20–22 ч."""
    ps = solve_pass(P, C, 24.0, 407.4, 35)
    assert abs(ps.time_h - ps.m_out / 18.2) < 1e-9
    assert 18.0 < ps.time_h < 22.5


def test_single_pass_is_rejected_by_quality_limit():
    plan = plan_by_passes(P, C, 500.0, 34.0, 14.0, 1)
    assert plan.reaches_target
    assert abs(plan.max_dw - 20.0) < 1.0
    assert plan.verdict(6, 10)[0] == 2


def test_last_pass_never_misses_target_by_rounding():
    for n in (1, 2, 3, 4):
        assert plan_by_passes(P, C, 500.0, 34.0, 14.0, n).reaches_target


def test_many_passes_hit_d100_limit():
    """Восемь проходов = съём 2,5 п.п./проход меньше, чем даёт D=100: пометка «предел», не провал."""
    plan = plan_by_passes(P, C, 500.0, 34.0, 14.0, 8)
    assert plan.passes[0].d == 100.0 and not plan.passes[0].feasible
    level, text = plan.verdict(6, 10)
    assert level == 1 and "D=100" in text


def test_recommend_picks_min_passes_in_limit():
    plans = scenarios(P, C, 500.0, 34.0, 14.0, 6)
    i = recommend(plans, 6.0, 10.0)
    assert i is not None
    assert plans[i].verdict(6.0, 10.0)[0] == 0
    assert plans[i].max_dw <= 6.0 + 1e-9
    for j in range(i):
        assert plans[j].verdict(6.0, 10.0)[0] > 0


def test_manual_plan_repeats_last_d_and_trims():
    plan = plan_manual(P, C, 500.0, 34.0, 14.0, [80])
    assert plan.reaches_target
    assert len(plan.passes) >= 3


def test_manual_plan_d35_like_the_dryers_do():
    """D=35 на всех проходах: 500 т с 34 → 14 % занимает 3–4 прохода и 3–4 суток."""
    plan = plan_manual(P, C, 500.0, 34.0, 14.0, [35], t_list=[60, 60, 60])
    assert plan.reaches_target
    assert 3 <= len(plan.passes) <= 4
    assert 60 < plan.total_time_h < 100, plan.total_time_h


def test_temperature_by_pass_changes_result():
    hot = plan_manual(P, C, 500.0, 34.0, 14.0, [35], t_list=[75])
    mild = plan_manual(P, C, 500.0, 34.0, 14.0, [35], t_list=[50])
    assert hot.passes[0].w_out < mild.passes[0].w_out
    assert hot.passes[0].t_proc == 75 and mild.passes[0].t_proc == 50


def test_work_time_nearly_independent_of_pass_count():
    t = [plan_by_passes(P, C, 500.0, 34.0, 14.0, n).work_h for n in (1, 2, 3, 4)]
    assert max(t) / min(t) < 1.1


def test_plan_simple_time_and_balance():
    pl = plan_simple(P, 500.0, 34.0, 14.0, [52.0], 3)
    assert len(pl.passes) == 3 and pl.reaches_target
    assert abs(pl.m_final - 500.0 * 66.0 / 86.0) < 1e-6
    assert abs(pl.work_h - sum(ps.m_out for ps in pl.passes) / 52.0) < 1e-9
    assert all(abs(ps.dw - 20.0 / 3) < 1e-9 for ps in pl.passes)
    assert abs(pl.total_time_h - (pl.work_h + 3 * P.overhead_h)) < 1e-9
    pl2 = plan_simple(P, 500.0, 34.0, 14.0, [50.0, 40.0], 3)
    assert [ps.q for ps in pl2.passes] == [50.0, 40.0, 40.0]


def test_plan_simple_final_cooling_pass():
    a = plan_simple(P, 500.0, 34.0, 14.0, [18.2], 3, cool=False)
    b = plan_simple(P, 500.0, 34.0, 14.0, [18.2], 3, cool=True)
    assert a.cool_h == 0.0
    assert abs(b.cool_h - b.m_final / 52.0) < 1e-9              # охлаждение на D=100 без нагрева
    assert abs(b.total_time_h - a.total_time_h - b.cool_h) < 1e-9
    assert b.timeline()[-1][0] > a.timeline()[-1][0]


# ---------------------------------------------------------------------------
# Разбор журнала
# ---------------------------------------------------------------------------

def test_parsing_values():
    assert parse_pair("60-55") == (60.0, 55.0)
    assert parse_pair("26°-24°") == (26.0, 24.0)
    assert parse_pair("55 – 60 °C") == (55.0, 60.0)
    assert parse_pair("57,5") is None
    assert to_float("27,5 %") == 27.5
    assert to_float("") is None
    assert to_float("—") is None and to_float("-") is None and to_float("–") is None
    assert to_float(None) is None


def test_parsing_timestamps():
    d, t = parse_ts("21.10.26 08:30")
    assert (d.year, d.month, d.day, t.hour, t.minute) == (2026, 10, 21, 8, 30)
    d, t = parse_ts("08:30")
    assert d is None and (t.hour, t.minute) == (8, 30)
    d, t = parse_ts("21.10 23:30")
    assert d is not None and d.month == 10 and d.day == 21
    assert parse_ts("мусор") == (None, None)
    assert parse_ts("") == (None, None)


def test_time_of_day_only_rolls_over_midnight():
    rows = [{"ts": s} for s in ("22:00", "23:00", "23:30", "00:30", "01:00")]
    times, reliable = assign_times(rows)
    assert reliable
    assert [(t - times[0]).total_seconds() / 3600 for t in times] == [0.0, 1.0, 1.5, 2.5, 3.0]


def test_headers_mapping():
    assert map_header("Влага вход, %") == "w_in"
    assert map_header("Влага выход") == "w_out"
    assert map_header("Вход") == "w_in" and map_header("Выход") == "w_out"
    assert map_header("Выгрузка") == "d" and map_header("Разгрузка D, с") == "d" and map_header("Загрузка") == "d"
    assert map_header("T агента 1") == "t_a1"
    assert map_header("Т агента (датчик 2)") == "t_a2"
    assert map_header("T зерна низ, °C") == "t_a1" and map_header("T зерна верх, °C") == "t_a2"
    assert map_header("Т кукурузы") == "grain_pair"
    assert map_header("Т топочных блоков") == "block_pair"
    assert map_header("Топочные блоки") == "block_pair"
    assert map_header("T блока верх, °C") == "t_up"
    assert map_header("T блока 1, °C") == "t_up" and map_header("T блока 2, °C") == "t_low"
    assert map_header("RH наружная, %") == "rh"
    assert map_header("T наружная, °C") == "t_amb"
    assert map_header("Дата-время") == "ts"
    assert map_header("Примечание") == "note"
    assert map_header("Партия") == "batch" and map_header("Проход") == "pass_no"


def test_read_journal_csv_with_pairs_dashes_and_decimal_comma():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "j.csv")
        with open(path, "w", encoding="cp1251", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Время", "Вход", "Выход", "Т кукурузы", "Т топочных блоков", "Разгрузка", "Примечание"])
            w.writerow(["25.10 08:00", "28,4", "23,1", "26°-24°", "60-55", "35", "Запуск"])
            w.writerow(["25.10 08:30", "28,2", "22,9", "27-25", "—", "—", ""])
            w.writerow(["25.10 09:00", "", "", "", "", "", ""])
            w.writerow(["25.10 09:30", "", "", "", "", "", "Остановка на 20 мин"])
        rows, info = read_journal(path)
        assert len(rows) == 3                                   # пустая строка пропущена, строка с примечанием — нет
        assert rows[0]["w_in"] == 28.4 and rows[0]["t_up"] == 60.0 and rows[0]["t_low"] == 55.0
        assert rows[0]["t_a1"] == 26.0 and rows[0]["t_a2"] == 24.0
        assert rows[1]["t_up"] is None and rows[1]["d"] is None  # «—» = без изменений
        assert rows[2]["note"].startswith("Остановка")
        assert info["mapped"]["block_pair"] == "Т топочных блоков"
        tpl = os.path.join(d, "tpl.csv")
        write_template(tpl)
        rows2, _ = read_journal(tpl)
        assert len(rows2) == 2


def test_prepare_forward_fills_blocks_and_d_and_splits_segments():
    rows = [
        {"ts": "08:00", "w_in": 20.0, "w_out": 16.0, "t_up": 60.0, "t_low": 60.0, "d": 35.0, "note": "", "batch": "A", "pass_no": 3},
        {"ts": "08:30", "w_in": 20.0, "w_out": 16.0, "t_up": None, "t_low": None, "d": None, "note": "", "batch": "A", "pass_no": 3},
        {"ts": "09:00", "w_in": None, "w_out": None, "d": None, "note": "Остановка", "batch": "A", "pass_no": 3},
        {"ts": "09:30", "w_in": 20.0, "w_out": 16.0, "t_up": 70.0, "t_low": 75.0, "d": 40.0, "note": "", "batch": "A", "pass_no": 3},
        {"ts": "12:30", "w_in": 20.0, "w_out": 16.0, "d": None, "note": "", "batch": "A", "pass_no": 3},   # разрыв > 1,5 ч
    ]
    prep, reliable = prepare(rows)
    assert reliable
    assert prep[1]["t_up_f"] == 60.0 and prep[1]["d_f"] == 35.0
    assert prep[3]["t_up_f"] == 70.0 and prep[3]["t_low_f"] == 75.0 and prep[3]["d_f"] == 40.0
    segs = [r["seg"] for r in prep]
    assert segs[0] == segs[1] < segs[2] <= segs[3] < segs[4]


# ---------------------------------------------------------------------------
# Калибровка
# ---------------------------------------------------------------------------

def _no_bias(**kw) -> DryerParams:
    return DryerParams(out_bias=0.0, **kw)


def _synthetic(true: DryerParams, n=80, seed=1, noise=0.1):
    """Строки без времени: вход и выход из одной строки (лаг не учитывается)."""
    rnd = random.Random(seed)
    rows = []
    for _ in range(n):
        w_in = round(rnd.uniform(18, 36), 1)
        d = rnd.choice([30, 35, 40, 50, 60, 70, 80, 100])
        t_up = round(rnd.uniform(55, 70))
        t_low = t_up - 5
        t_amb = round(rnd.uniform(0, 15), 1)
        rh = round(rnd.uniform(50, 95))
        c = Conditions((t_up + t_low) / 2, t_amb, rh)
        w_out = solve_pass(true, c, w_in, 1.0, d).w_out + rnd.gauss(0, noise)
        rows.append({"w_in": w_in, "w_out": round(w_out, 1), "d": float(d), "t_up": float(t_up),
                     "t_low": float(t_low), "t_amb": t_amb, "rh": float(rh)})
    return rows


def test_calibration_recovers_r_ref():
    true = _no_bias(r_ref=2.6)
    new_p, rep = calibrate(_synthetic(true), _no_bias())
    assert abs(new_p.r_ref / true.r_ref - 1) < 0.06, new_p.r_ref
    assert rep["mae"] < 0.3
    assert new_p.calibrated and not rep["lag"]


def test_calibration_small_journal_fits_intercept_only():
    true = _no_bias(r_ref=2.2)
    new_p, rep = calibrate(_synthetic(true, n=8, seed=3), _no_bias())
    assert abs(new_p.r_ref / true.r_ref - 1) < 0.15
    assert new_p.b_w == DryerParams().b_w and new_p.b_dt == DryerParams().b_dt


def test_calibration_needs_rows():
    try:
        calibrate([{"w_in": 30.0, "w_out": 25.0, "d": 80.0}], DryerParams())
    except ValueError:
        return
    raise AssertionError("ожидалась ошибка")


def _timed_series(true: DryerParams, hours=26.0, d=35.0, step=0.5, bias=0.8):
    """Журнал с временем: выход в момент t отражает вход в момент t − τ; влага входа дрейфует."""
    c = Conditions(60.0, 10.0, 70.0)
    tau = true.lag_k * true.m_dryer / q_at(true, d)
    t0 = datetime(2026, 10, 24, 8, 0)
    win = lambda t: 34.0 - 0.45 * t if t > 0 else 34.0          # noqa: E731
    rows = []
    n = int(hours / step) + 1
    for i in range(n):
        t = i * step
        w_out_true = solve_pass(true, c, max(win(t - tau), 9.0), 1.0, d).w_out if t >= tau else 40.0
        rows.append({"ts": (t0 + timedelta(hours=t)).strftime("%d.%m.%y %H:%M"),
                     "w_in": round(win(t), 1), "w_out": round(w_out_true + bias, 1),
                     "t_up": 60.0 if i == 0 else None, "t_low": 60.0 if i == 0 else None,
                     "d": d if i == 0 else None, "note": "", "batch": "X", "pass_no": 1})
    return rows, tau


def test_calibration_with_lag_recovers_r_ref_and_drops_leading_hours():
    true = DryerParams(r_ref=2.4, b_w=0.69, b_dt=0.87)
    rows, tau = _timed_series(true)
    start = DryerParams(r_ref=1.5, b_w=0.69, b_dt=0.87, out_bias=0.8)   # показатели и поправка влагомера журнала известны
    obs, skipped, lag = observe_all(rows, start)
    assert lag and skipped.get("ещё идёт прежнее зерно", 0) >= int(tau / 0.5) - 1
    assert min(o["t"] for o in obs) >= tau - 1e-6
    new_p, rep = calibrate(rows, start)
    assert abs(new_p.r_ref / true.r_ref - 1) < 0.07, new_p.r_ref
    assert rep["mae"] < 0.3


def test_ignoring_lag_would_bias_calibration():
    """Контроль: без учёта лага (вход из той же строки) при дрейфе влаги получается неверный R."""
    true = DryerParams(r_ref=2.4)
    rows, _ = _timed_series(true)
    with_lag, _ = calibrate(rows, DryerParams(r_ref=1.5))
    no_ts = [dict(r, ts="") for r in rows]
    no_lag, rep = calibrate(no_ts, DryerParams(r_ref=1.5))
    assert not rep["lag"]
    assert abs(with_lag.r_ref / true.r_ref - 1) < abs(no_lag.r_ref / true.r_ref - 1)


def test_out_bias_is_removed_before_balance():
    true = DryerParams(r_ref=2.4, out_bias=0.8)
    rows, _ = _timed_series(true, bias=0.8)
    a, _ = calibrate(rows, DryerParams(r_ref=1.5, out_bias=0.8))
    b, _ = calibrate(rows, DryerParams(r_ref=1.5, out_bias=0.0))
    assert abs(a.r_ref / 2.4 - 1) < 0.07
    assert b.r_ref < a.r_ref * 0.95         # без поправки показание завышено → съём и R занижены


# ---------------------------------------------------------------------------
# Регрессия на реальном журнале (фото 18–25.10.2026)
# ---------------------------------------------------------------------------

def test_sample_journal_is_readable():
    rows, info = read_journal(SAMPLE)
    assert len(rows) >= 150 and not info["unmapped"]
    assert {"w_in", "w_out", "t_a1", "t_up", "d", "batch", "pass_no", "ts", "note"} <= set(info["mapped"])
    ds = {r["d"] for r in rows if r["d"] is not None}
    assert 35.0 in ds and 40.0 in ds


def test_sample_journal_calibration_regression():
    rows, _ = read_journal(SAMPLE)
    new_p, rep = calibrate(rows, DryerParams())
    assert rep["lag"] and rep["n_used"] >= 100
    assert 1.5 <= new_p.r_ref <= 2.2, new_p.r_ref
    assert 0.2 <= new_p.b_w <= 1.2 and 0.3 <= new_p.b_dt <= 1.6
    assert rep["mae"] < 0.7
    assert len(rep["segments"]) >= 4
    assert rep["seg_mae"] < 0.35                                     # съём за проход ±0,3 п.п.
    for s in rep["segments"]:
        assert abs(s["dw_pred"] - s["dw_obs"]) < 0.6, s


def test_default_params_reproduce_sample_segments_without_calibration():
    """Стартовые параметры (R_ref=1,83) уже дают съём по проходам ближе 0,6 п.п. — без калибровки."""
    from app.calibration import segment_summary
    rows, _ = read_journal(SAMPLE)
    obs, _, _ = observe_all(rows, DryerParams())
    segs = segment_summary(obs, DryerParams())
    assert len(segs) >= 4
    for s in segs:
        assert abs(s["dw_pred"] - s["dw_obs"]) < 0.6, s


def test_leave_one_pass_out_prediction():
    """Честная проверка: калибруем по трём проходам журнала (нейтральные стартовые значения),
    предсказываем съём четвёртого. Ошибка по проходу не хуже ±0,7 п.п., в среднем ≈ ±0,25."""
    from app.calibration import segment_summary
    rows, _ = read_journal(SAMPLE)
    keys = {}
    for i, r in enumerate(rows):
        keys.setdefault((r["batch"], r["pass_no"]), []).append(i)
    assert len(keys) == 4
    neutral = DryerParams(r_ref=4.0, b_w=0.7, b_dt=1.0, calibrated=False)
    errs = []
    for idxs in keys.values():
        hold = set(idxs)
        train = [r for i, r in enumerate(rows) if i not in hold]
        test = [rows[i] for i in idxs]
        new_p, _ = calibrate(train, neutral)
        obs, _, _ = observe_all(test, new_p)
        seg = segment_summary(obs, new_p)[0]
        errs.append(seg["dw_pred"] - seg["dw_obs"])
    assert max(abs(e) for e in errs) < 0.7, errs
    assert sum(abs(e) for e in errs) / len(errs) < 0.35, errs


def test_params_roundtrip_and_unknown_keys():
    p = DryerParams(q_table=[[10, 5.2], [100, 52.0]], out_bias=1.2)
    q = DryerParams.from_dict(dict(p.to_dict(), garbage=1))
    assert q == p


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print("FAIL", name, "->", repr(e))
    sys.exit(1 if failed else 0)
