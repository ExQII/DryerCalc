"""Напоминания сушильщику (работают без интернета, пока программа открыта).

* «Пора внести пробу» — последняя проба в журнале старше интервала проб + запас, а сушка идёт
  (последний участок журнала не закончен охлаждением/остановкой и последней записи меньше 8 ч).
* «Проход заканчивается» — по прогнозу раздела «Текущий проход» до конца прохода осталось меньше заданного времени
  (одно напоминание на проход): пора готовить силос и следующий проход.
* «Выход ушёл от цели» — последнее показание выходного влагомера отличается от цели прохода (в пересчёте
  на показание влагомера) больше чем на заданный порог (одно напоминание на запись).

Логика — чистая функция check(): её проверяют тесты; окно только показывает сообщения
(всплывающее уведомление Windows у часов, строка состояния, мигание кнопки на панели задач).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from . import model as mdl

STOP_WORDS = ("остановк", "охлажд")


@dataclass
class ReminderCfg:
    enabled: bool = True
    sample_min: float = 30.0      # интервал проб, мин
    grace_min: float = 10.0       # запас, мин
    pass_lead_min: float = 60.0   # предупредить о конце прохода за, мин
    deviation_pp: float = 1.0     # порог отклонения выхода от цели, п.п.

    @classmethod
    def from_dict(cls, d: Optional[Dict]) -> "ReminderCfg":
        d = d or {}
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in names})

    def to_dict(self) -> Dict:
        return asdict(self)


def drying_active(rows: List[Dict], now: datetime) -> Tuple[bool, Optional[datetime], Optional[int]]:
    """(идёт ли сушка, время последней пробы с влажностью, номер участка)."""
    from .calibration import is_feed_note, pass_of_segment, prepare
    prep, reliable = prepare(rows)
    if not prep or not reliable:
        return False, None, None
    last = prep[-1]
    data = [r for r in prep if (r.get("w_in") is not None or r.get("w_out") is not None) and r.get("dt")]
    if not data or last.get("dt") is None:
        return False, None, None
    last_sample = data[-1]["dt"]
    note = (last.get("note") or "").lower()
    has_data = last.get("w_in") is not None or last.get("w_out") is not None
    # «Закончился 2 с., сушим с 1 с.» — переключение силоса, сушка идёт; «Закончились партию» без пробы — стоп
    ended = "закончил" in note and not is_feed_note(note) and "рецирк" not in note
    stopped = bool(last.get("burner_off")) or ((any(w in note for w in STOP_WORDS) or ended) and not has_data)
    fresh = (now - last["dt"]) <= timedelta(hours=8)
    return (not stopped) and fresh, last_sample, pass_of_segment(prep).get(last["seg"], last["seg"])


def check(rows: List[Dict], now: datetime, state: Dict, cfg: ReminderCfg, pass_end: Optional[datetime] = None,
          target_read: Optional[float] = None) -> List[Tuple[str, str, str]]:
    """Список (ключ, заголовок, текст) новых напоминаний; state обновляется (что и когда уже напоминали)."""
    out: List[Tuple[str, str, str]] = []
    if not cfg.enabled:
        return out
    active, last_sample, seg = drying_active(rows, now)
    if not active or last_sample is None:
        return out
    # 1) проба
    due = last_sample + timedelta(minutes=cfg.sample_min + cfg.grace_min)
    last_told = state.get("sample_told")
    if now >= due and (last_told is None or now - last_told >= timedelta(minutes=cfg.sample_min)
                       or (state.get("sample_for") != last_sample)):
        mins = int((now - last_sample).total_seconds() // 60)
        out.append(("sample", "Пора внести пробу",
                    f"Последняя проба в журнале — в {last_sample:%H:%M} ({mins} мин назад). Отберите пробу на входе и "
                    f"выходе и внесите её (раздел «Журнал проб», строка быстрого ввода)."))
        state["sample_told"] = now
        state["sample_for"] = last_sample
    # 2) конец прохода
    if pass_end is not None and state.get("pass_end_seg") != seg:
        left = (pass_end - now).total_seconds() / 60.0
        if 0 < left <= cfg.pass_lead_min:
            out.append(("pass_end", "Проход скоро закончится",
                        f"По прогнозу проход закончится около {pass_end:%H:%M} (через {left:.0f} мин). "
                        f"Подготовьте силос и следующий проход."))
            state["pass_end_seg"] = seg
    # 3) отклонение выхода
    if target_read is not None:
        last_out = None
        from .calibration import assign_times
        times, _ = assign_times(rows)
        for r, dt in zip(rows, times):
            if r.get("w_out") is not None and dt is not None:
                last_out = (dt, r["w_out"])
        if last_out and state.get("dev_for") != last_out[0]:
            dev = last_out[1] - target_read
            if abs(dev) > cfg.deviation_pp:
                side = "выше" if dev > 0 else "ниже"
                out.append(("deviation", "Выход ушёл от цели",
                            f"В {last_out[0]:%H:%M} выходной влагомер показал {last_out[1]:.1f} % — на {abs(dev):.1f} п.п. "
                            f"{side} цели ({target_read:.1f} %). Проверьте D в разделе «Текущий проход»; помните, что смена D "
                            f"видна на выходе только через несколько часов."))
            state["dev_for"] = last_out[0]
    return out


def live_numbers(rows: List[Dict], p: mdl.DryerParams, ui_live: Dict, soft: float, hard: float, weather=None,
                 now: Optional[datetime] = None, plan: Optional[Tuple[float, float]] = None
                 ) -> Tuple[Optional[datetime], Optional[float]]:
    """(ожидаемый конец текущего прохода, цель прохода в показаниях влагомера) — как в разделе «Текущий проход».
    Во время рециркуляции подача прохода уже закончилась — «проход скоро закончится» не напоминаем.
    plan — (масса партии, W₀) из «План сушки партии»: при «авто» масса на входе прохода считается, как в разделе."""
    from .live import live_forecast, live_state, pass_mass
    st = live_state(rows, p, now=now, weather=weather, final_w=float(ui_live.get("wk", 14.0)) + 1.0)
    if not st.ok:
        return None, None
    if st.recirc:
        return None, mdl.read_from_true(p, float(ui_live.get("w_target", 24.0)), st.t_grain)
    w_target = float(ui_live.get("w_target", 24.0))
    c = mdl.Conditions(st.t_proc, float(ui_live.get("t_amb", p.t_amb_ref)), float(ui_live.get("rh", p.rh_ref)))
    m_pass = float(ui_live.get("m_pass", 500.0))
    if plan is not None and ui_live.get("m_auto", True):
        m_auto, _how = pass_mass(plan[0], plan[1], st.batch_passes)
        m_pass = m_auto if m_auto is not None else m_pass
    fc = live_forecast(p, c, st, m_pass, w_target, float(ui_live.get("wk", 14.0)),
                       soft, hard, use_corr=bool(ui_live.get("corr", False)) and ui_live.get("corr_v") == 2, now=now,
                       silo=bool(ui_live.get("silo", True)), silo_lim=float(ui_live.get("silo_lim", 1.0)))
    return fc.pass_end, mdl.read_from_true(p, w_target, st.t_grain)
