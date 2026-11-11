"""Раздел «Культура»: с каким зерном сейчас работает сушилка.

Карточка на культуру: главное о её сушке, цель и пределы, сколько тонн в час и в шахте, что уже есть в программе
(журнал, модель). Кнопка карточки переключает культуру: у каждой свои журнал, модель, партии и поправка влагомера
(storage.CROP_KEYS), а сушилка, погода и напоминания — общие. Откуда берётся стартовая модель культуры — crops.py.
"""
from __future__ import annotations

from typing import Dict

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from . import crops
from . import model as mdl
from .ui_common import Collapsible, mute, scroll_wrap, set_role

ABOUT = (
    "У каждой культуры свои журнал проб, модель сушки, партии, очередь и поправка влагомера. Переключили культуру — "
    "работаете с её данными, а данные остальных лежат нетронутыми. Сушилка, погода, напоминания и режим — общие."
    "<br><br><b>Пока по культуре нет своего журнала</b>, программа считает по стартовой оценке: это модель вашей "
    "сушилки на кукурузе, пересчитанная на другую культуру. Пересчёт идёт по равновесной влажности воздуха — "
    "рапс при 10 % отдаёт влагу так же охотно, как кукуруза при 15 %. Для пшеницы скорость ещё умножена на 1,6: "
    "по нормам шахтная сушилка даёт на кукурузе 0,6 от производительности на пшенице. Точность оценки — около "
    "±30 %, поэтому допуски в планах широкие."
    "<br><br><b>После 12 проб с влажностью</b> нажмите «Калибровать модель» в «Журнале проб» — дальше программа "
    "считает по вашим данным, как для кукурузы."
    "<br><br>Тонны в час и вместимость шахты пересчитаны по насыпной плотности культуры. Пределы нагрева и съём "
    "за проход — по Инструкции № 9-3-82 для продовольственного зерна; для семенного зерна и сильной пшеницы "
    "они ниже — поправьте в «Настройках сушилки».")


def _num(v: float, nd: int = 0) -> str:
    return f"{v:.{nd}f}".replace(".", ",")


class _Card(QFrame):
    def __init__(self, crop: crops.Crop, on_pick):
        super().__init__()
        self.crop = crop
        self.setObjectName("tile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(4)
        head = QHBoxLayout()
        self.lbl_name = QLabel(crop.name)
        self.lbl_name.setProperty("role", "tileValue")
        self.lbl_now = QLabel("сейчас")
        self.lbl_now.setProperty("role", "factVal")
        head.addWidget(self.lbl_name)
        head.addStretch(1)
        head.addWidget(self.lbl_now, 0, Qt.AlignVCenter)
        lay.addLayout(head)
        self.lbl_note = QLabel(crop.note)
        self.lbl_note.setWordWrap(True)
        lay.addWidget(self.lbl_note)
        lay.addSpacing(4)
        self.lbl_facts = mute(QLabel())
        self.lbl_facts.setWordWrap(True)
        lay.addWidget(self.lbl_facts)
        self.lbl_state = mute(QLabel())
        self.lbl_state.setWordWrap(True)
        lay.addWidget(self.lbl_state)
        lay.addStretch(1)
        self.btn = QPushButton("Выбрать")
        self.btn.clicked.connect(lambda: on_pick(crop.key))
        lay.addWidget(self.btn, 0, Qt.AlignLeft)

    def fill(self, info: Dict, current: bool) -> None:
        c, p = self.crop, info["params"]
        nb = "\u00a0"
        lines = [f"Цель {_num(p.w_store, 0 if p.w_store == int(p.w_store) else 1)}{nb}%  ·  съём до {c.soft:g}{nb}п.п. "
                 f"за проход"]
        lim = []
        if p.t_grain_max:
            lim.append(f"зерно до {p.t_grain_max:g}{nb}°C")
        if p.t_agent_max:
            lim.append(f"блоки до {p.t_agent_max:g}{nb}°C")
        if lim:
            lines.append("Нагрев: " + ", ".join(lim))
        lines.append(f"Натура {_num(p.natura, 0)}{nb}г/л  ·  до {_num(mdl.q_full(p), 0)}{nb}т/ч  ·  в шахте "
                     f"{_num(p.m_dryer, 0)}{nb}т")
        self.lbl_facts.setText("\n".join(lines))
        rows = info["rows"]
        if p.calib_info.startswith("стартовая"):
            model = "модель — стартовая оценка" if c.key != crops.DEFAULT else "модель — стартовая"
        elif p.calibrated:
            model = "модель откалибрована по журналу"
        else:
            model = "модель ждёт калибровки"
        self.lbl_state.setText((f"В журнале {rows} строк" if rows else "Журнал пуст") + "  ·  " + model)
        self.lbl_now.setVisible(current)
        self.btn.setVisible(not current)
        set_role(self.btn, "primary" if not current else "")


class CropTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 8, 0)
        grid = QGridLayout()
        grid.setSpacing(12)
        self.cards: Dict[str, _Card] = {}
        for i, c in enumerate(crops.CROPS):
            card = _Card(c, self._pick)
            self.cards[c.key] = card
            grid.addWidget(card, i // 3, i % 3)
        for col in range(3):
            grid.setColumnStretch(col, 1)
        lay.addLayout(grid)
        lay.addSpacing(6)
        self.about = Collapsible("Как программа считает новую культуру", ABOUT)
        mute(self.about.content)
        lay.addWidget(self.about)
        lay.addStretch(1)
        outer.addWidget(scroll_wrap(inner))
        self.refresh()

    def _pick(self, key: str) -> None:
        self.win.switch_crop(key)

    def refresh(self) -> None:
        info = self.win.storage.crop_summary()
        for key, card in self.cards.items():
            card.fill(info[key], key == self.win.storage.crop)

    def on_show(self) -> None:
        self.refresh()
