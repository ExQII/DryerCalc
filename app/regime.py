"""Режим сушки (D, T блоков) в журнале и строка быстрого ввода.

* journal_carry() — что подставляет строка быстрого ввода: режим последней записи журнала, T наружную и время
  следующей пробы. Окно вызывает её после любой правки журнала, поэтому исправленные в таблице D или T блоков сразу
  переходят в быстрый ввод.
* plan_regime_change() — как записать смену режима отдельной строкой с её настоящим временем: куда вставить,
  что написать в примечании и какие строки после неё записаны со старым режимом.
* RegimeDialog — окно «Изменение режима сушки»: и по кнопке в быстром вводе, и когда в последней строке с пробой
  поменяли D или T блоков (опечатка это или режим поменяли после пробы).

Почему смена режима — отдельной строкой, а не правкой строки с пробой. В строке журнала D — это разгрузка, при
которой взята проба, и «выгружено» считается от этой строки вперёд. Проба в 10:00 взята при D = 35; если после неё
в 10:05 поставили 40 и исправили D в строке 10:00, калибровка решит, что зерно этой пробы шло при Q(40) — скорость
сушки этого наблюдения завысится на Q(40)/Q(35) − 1 ≈ 14 %, а «выгружено» прибавит 5 минут лишнего расхода.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .calibration import FIELDS, TEXT_FIELDS, assign_times, insert_position, parse_ts, prepare

QE_CARRY = ("t_amb", "t_up", "t_low", "d", "batch", "pass_no")   # что строка быстрого ввода переносит с прошлой записи
AMB_CARRY_H = 3.0         # T наружная переносится, если записана не раньше чем за 3 ч до следующей пробы
REGIME_FIELDS = ("d", "t_up", "t_low")
REGIME_NAMES = {"d": "D", "t_up": "T блока 1", "t_low": "T блока 2"}
TS_FORMAT = "%d.%m.%y %H:%M"


def has_sample(row: Dict) -> bool:
    return row.get("w_in") is not None or row.get("w_out") is not None


def fmt_num(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:g}".replace(".", ",")


def journal_carry(rows: List[Dict], interval_min: float = 30.0) -> Tuple[Dict[str, object], Optional[datetime]]:
    """(значения, время следующей пробы) для строки быстрого ввода.

    Значения — режим последней записи журнала: T блоков и D — последние известные (пустая клетка = прежнее значение),
    партия и проход — последней строки. T наружная — последняя записанная, если она не старше AMB_CARRY_H до следующей
    пробы: воздух меняется медленно, и сушильщик пишет её в каждую строку, но после остановки на ночь или выходные
    прежнее значение уже не про сегодня — поле остаётся пустым. Время — последняя проба (строка с влажностью)
    + интервал; если после неё записано событие позже этого срока («Запуск» после остановки) — событие + интервал.
    Поэтому строка смены режима в 10:05 между пробами 10:00 и 10:30 не сдвигает следующую пробу на 10:35.
    None — время в журнале не распознано (тогда и возраст T наружной не проверить — переносится последняя)."""
    if not rows:
        return {}, None
    prep, reliable = prepare(rows)
    last = prep[-1]
    vals: Dict[str, object] = {"t_up": last.get("t_up_f"), "t_low": last.get("t_low_f"), "d": last.get("d_f"),
                               "pass_no": last.get("pass_no"), "batch": (last.get("batch") or None)}
    amb = next((r for r in reversed(prep) if r.get("t_amb") is not None), None)
    vals["t_amb"] = amb["t_amb"] if amb is not None else None
    if not reliable:
        return vals, None
    times = [r["dt"] for r in prep if r.get("dt") is not None]
    if not times:
        return vals, None
    step = timedelta(minutes=interval_min)
    samples = [r["dt"] for r in prep if r.get("dt") is not None and has_sample(r)]
    last_any = max(times)
    nxt = max(samples) + step if samples else last_any + step
    if last_any >= nxt:
        nxt = last_any + step
    if amb is not None and (amb.get("dt") is None or nxt - amb["dt"] > timedelta(hours=AMB_CARRY_H)):
        vals["t_amb"] = None
    return vals, nxt


def regime_at(rows: List[Dict], pos: int) -> Dict[str, Optional[float]]:
    """Режим, действующий после строки pos − 1: последние известные D и T блоков сверху."""
    out: Dict[str, Optional[float]] = {k: None for k in REGIME_FIELDS}
    for r in rows[:pos]:
        for k in REGIME_FIELDS:
            if r.get(k) is not None:
                out[k] = r[k]
    return out


def changed_fields(old: Dict[str, Optional[float]], new: Dict[str, Optional[float]]) -> List[str]:
    return [k for k in REGIME_FIELDS if new.get(k) is not None and (old.get(k) is None or abs(new[k] - old[k]) > 1e-9)]


def regime_note(old: Dict[str, Optional[float]], new: Dict[str, Optional[float]]) -> str:
    """Примечание строки смены режима: «Режим: D 35→40 с; T блоков 60/55→65/60 °C». Без слов-команд журнала."""
    ch = changed_fields(old, new)
    parts = []
    if "d" in ch:
        parts.append(f"D {fmt_num(old.get('d'))}→{fmt_num(new['d'])} с")
    if "t_up" in ch or "t_low" in ch:
        n1 = new.get("t_up") if new.get("t_up") is not None else old.get("t_up")
        n2 = new.get("t_low") if new.get("t_low") is not None else old.get("t_low")
        parts.append(f"T блоков {fmt_num(old.get('t_up'))}/{fmt_num(old.get('t_low'))}→{fmt_num(n1)}/{fmt_num(n2)} °C")
    return "Режим: " + "; ".join(parts) if parts else ""


def plan_regime_change(rows: List[Dict], ts: datetime, new: Dict[str, Optional[float]], comment: str = "") -> Dict:
    """Как записать смену режима в момент ts.

    Возвращает {"pos": индекс вставки в rows, "row": строка журнала, "old": прежний режим, "changed": [поля],
    "fixes": [(индекс в rows, поле, новое значение)]}. fixes — строки ПОСЛЕ события (если его вписывают задним
    числом), в которых записан ещё старый режим: подряд идущие строки со старым значением поля до первой строки,
    где оно другое (там режим уже меняли ещё раз — дальше не трогаем). Пустые клетки пропускаются: они и так
    наследуют значение сверху."""
    ts_txt = ts.strftime(TS_FORMAT)
    pos = insert_position(rows, ts_txt)
    old = regime_at(rows, pos)
    ch = changed_fields(old, new)
    prev = rows[pos - 1] if pos > 0 else {}
    row: Dict = {f: ("" if f in TEXT_FIELDS else None) for f in FIELDS}
    row["ts"] = ts_txt
    for k in REGIME_FIELDS:
        row[k] = new[k] if new.get(k) is not None else old[k]
    row["batch"] = prev.get("batch") or ""
    row["pass_no"] = prev.get("pass_no")
    note = regime_note(old, new)
    comment = (comment or "").strip()
    row["note"] = f"{note} — {comment}" if note and comment else (note or comment)
    fixes: List[Tuple[int, str, float]] = []
    for k in ch:
        if old[k] is None:
            continue
        for j in range(pos, len(rows)):
            v = rows[j].get(k)
            if v is None:
                continue
            if abs(v - old[k]) > 1e-9:
                break
            fixes.append((j, k, new[k]))
    return {"pos": pos, "row": row, "old": old, "changed": ch, "fixes": sorted(fixes)}


def row_time(row: Dict) -> Optional[datetime]:
    d, t = parse_ts(row.get("ts"))
    return datetime.combine(d, t) if d is not None and t is not None else None


def default_change_time(rows: List[Dict], now: datetime, after: Optional[datetime] = None) -> datetime:
    """Время смены режима по умолчанию: «сейчас», если оно не раньше последней записи (или строки after) и не позже
    её больше чем на час (запись идёт по ходу сушки); иначе — минута после последней записи (журнал вносят задним
    числом с бумаги — время всё равно нужно поправить)."""
    now = now.replace(second=0, microsecond=0)
    if after is None:
        times, _ = assign_times(rows)
        after = max((t for t in times if t is not None), default=None)
    if after is None:
        return now
    if after < now <= after + timedelta(hours=1):
        return now
    return after + timedelta(minutes=1)


# ----------------------------------------------------------------------------------------------------------------
# Окно «Изменение режима сушки»
# ----------------------------------------------------------------------------------------------------------------

def _make_dialog_class():
    from PySide6.QtCore import QDateTime
    from PySide6.QtWidgets import (QCheckBox, QDateTimeEdit, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                                   QLabel, QLineEdit, QPushButton, QVBoxLayout)

    from .calibration import to_float

    class RegimeDialog(QDialog):
        """Запись смены режима отдельной строкой. changes — что меняется (новые значения); остальные поля показывают
        режим, действовавший в выбранное время, и сами следуют за временем, пока их не поправили руками. edit_mode —
        окно открыто после правки D/T блоков в последней строке с пробой: «Это опечатка» (и Esc) оставляет правку
        как есть."""
        TYPO = 2

        def __init__(self, parent, rows: List[Dict], changes: Dict[str, float], ts: datetime,
                     min_ts: Optional[datetime] = None, intro: str = "", edit_mode: bool = False):
            super().__init__(parent)
            self.setWindowTitle("Изменение режима сушки")
            self.setMinimumWidth(600)
            self.rows = rows
            self.plan: Optional[Dict] = None
            self._dirty = {k for k, v in changes.items() if k in REGIME_FIELDS and v is not None}
            self._busy = False
            lay = QVBoxLayout(self)
            if intro:
                lb = QLabel(intro)
                lb.setWordWrap(True)
                lay.addWidget(lb)
            form = QFormLayout()
            self.ts = QDateTimeEdit(QDateTime(ts))
            self.ts.setDisplayFormat("dd.MM.yy HH:mm")
            self.ts.setCalendarPopup(True)
            if min_ts is not None:
                self.ts.setMinimumDateTime(QDateTime(min_ts))
            form.addRow("Когда изменили режим", self.ts)
            self.ed: Dict[str, QLineEdit] = {}
            for k in REGIME_FIELDS:
                le = QLineEdit(fmt_num(changes[k]) if k in self._dirty else "")
                le.setFixedWidth(80)
                le.textChanged.connect(lambda _t, key=k: self._mark(key))
                self.ed[k] = le
                form.addRow({"d": "Разгрузка D, с", "t_up": "T блока 1, °C", "t_low": "T блока 2, °C"}[k], le)
            self.comment = QLineEdit()
            self.comment.setPlaceholderText("необязательно: почему, например «по совету»")
            form.addRow("Комментарий", self.comment)
            lay.addLayout(form)
            self.preview = QLabel()
            self.preview.setWordWrap(True)
            lay.addWidget(self.preview)
            self.cb_fix = QCheckBox()
            self.cb_fix.setChecked(True)
            self.cb_fix.setVisible(False)
            lay.addWidget(self.cb_fix)
            bb = QDialogButtonBox()
            self.btn_ok = bb.addButton("Записать изменение режима", QDialogButtonBox.AcceptRole)
            self.btn_ok.setProperty("role", "primary")        # главное действие окна — золотом (design.py)
            if edit_mode:
                typo = QPushButton("Это опечатка — оставить исправление")
                typo.clicked.connect(lambda: self.done(self.TYPO))
                bb.addButton(typo, QDialogButtonBox.RejectRole)
                typo.setDefault(True)
                typo.setAutoDefault(True)
            else:
                bb.addButton("Отмена", QDialogButtonBox.RejectRole)
            bb.accepted.connect(self._accept)
            bb.rejected.connect(self.reject)
            hl = QHBoxLayout()
            hl.addStretch(1)
            hl.addWidget(bb)
            lay.addLayout(hl)
            self.ts.dateTimeChanged.connect(self._update)
            for le in list(self.ed.values()) + [self.comment]:
                le.textChanged.connect(self._update)
            self._update()

        def _mark(self, key: str) -> None:
            """Текст поля поменял человек (свои подстановки окно делает под флагом _busy) — значит, это изменение."""
            if not self._busy:
                self._dirty.add(key)

        def chosen_time(self) -> datetime:
            return self.ts.dateTime().toPython().replace(second=0, microsecond=0)

        def new_values(self) -> Optional[Dict[str, Optional[float]]]:
            """Только то, что меняется (поля, поправленные человеком); None — в поле не число."""
            out: Dict[str, Optional[float]] = {}
            for k in self._dirty:
                txt = self.ed[k].text().strip()
                v = to_float(txt) if txt else None
                if txt and v is None:
                    return None
                out[k] = v
            return out

        def _update(self, *_a) -> None:
            if self._busy:
                return
            self._busy = True
            try:
                self._refresh()
            finally:
                self._busy = False

        def _refresh(self) -> None:
            ts = self.chosen_time()
            old = regime_at(self.rows, insert_position(self.rows, ts.strftime(TS_FORMAT)))
            for k, le in self.ed.items():              # неизменяемые поля — режим, действовавший в это время
                if k not in self._dirty:
                    le.setText("" if old[k] is None else fmt_num(old[k]))
            vals = self.new_values()
            if vals is None:
                self.plan = None
                from .design import tokens_for
                self.preview.setText(f"<span style='color:{tokens_for(self).rust}'>D и T блоков — числа, например 40 "
                                     f"или 62,5.</span>")
                self.btn_ok.setEnabled(False)
                self.cb_fix.setVisible(False)
                return
            plan = plan_regime_change(self.rows, ts, vals, self.comment.text())
            self.plan = plan
            if not plan["changed"]:
                self.preview.setText("Впишите новые значения: сейчас они совпадают с режимом, который действовал в "
                                     "это время, — записывать нечего.")
                self.btn_ok.setEnabled(False)
                self.cb_fix.setVisible(False)
                return
            self.btn_ok.setEnabled(True)
            where = ("в конец журнала" if plan["pos"] >= len(self.rows)
                     else f"на своё место по времени — перед строкой {self.rows[plan['pos']].get('ts')}")
            self.preview.setText(f"Добавится строка <b>{plan['row']['ts']}</b> ({where}) с примечанием "
                                 f"«{plan['row']['note']}». Пробы до неё останутся со старым режимом, после — "
                                 f"с новым; время следующей пробы быстрый ввод возьмёт от последней пробы.")
            fx = plan["fixes"]
            if fx:
                idx = sorted({j for j, _, _ in fx})
                shown = ", ".join(str(self.rows[j].get("ts", "")).split(" ")[-1] for j in idx[:5]) + (
                    " …" if len(idx) > 5 else "")
                self.cb_fix.setText(f"Строки после изменения записаны со старым режимом ({len(idx)}: {shown}) — "
                                    f"исправить в них на новый")
            self.cb_fix.setVisible(bool(fx))

        def _accept(self) -> None:
            if self.plan is not None and self.plan["changed"]:
                self.accept()

        def fix_following(self) -> bool:
            return not self.cb_fix.isHidden() and self.cb_fix.isChecked()

    return RegimeDialog


_DIALOG = None


def regime_dialog_class():
    """Класс окна (создаётся при первом обращении: модуль можно импортировать и без Qt — в тестах логики)."""
    global _DIALOG
    if _DIALOG is None:
        _DIALOG = _make_dialog_class()
    return _DIALOG
