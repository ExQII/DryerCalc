"""Печатные формы без интернета и без принтерных модулей: PDF через QPdfWriter (входит в QtGui), XLSX через openpyxl.

* бланк журнала сушки на смену (A4 альбомный, 30-минутные строки, колонки как в программе);
* отчёт по партии (PDF и XLSX): проходы по журналу, сверка с весовой, вода, топливо, тонно-проценты, деньги;
* сводка по всем партиям сезона (XLSX).
"""
from __future__ import annotations

import html
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence

from PySide6.QtCore import QMarginsF, QRectF, Qt
from PySide6.QtGui import QFont, QPageLayout, QPageSize, QPainter, QPdfWriter, QPen, QTextDocument

from . import __version__
from .batches import balance, fuel_metrics, journal_summary, parse_when

BLANK_COLS = [
    ("Время", 1.0), ("Влага\nвход, %", 1.0), ("Влага\nвыход, %", 1.0), ("T зерна\nниз, °C", 1.0),
    ("T зерна\nверх, °C", 1.0), ("T блока 1,\n°C", 1.0), ("T блока 2,\n°C", 1.0), ("Разгрузка\nD, с", 1.0),
    ("T нар.,\n°C", 0.9), ("RH нар.,\n%", 0.9), ("Примечание", 4.2),
]


def journal_blank_pdf(path: str, start: Optional[datetime] = None, hours: int = 12, step_min: int = 30,
                      batch: str = "", pass_no: str = "") -> int:
    """Бланк журнала. start — время первой строки (None — пустые клетки времени). Возвращает число страниц."""
    rows_total = max(1, int(hours * 60 // step_min))
    per_page = 24
    pages = (rows_total + per_page - 1) // per_page
    w = QPdfWriter(path)
    w.setPageSize(QPageSize(QPageSize.A4))
    w.setPageOrientation(QPageLayout.Landscape)
    w.setPageMargins(QMarginsF(10, 9, 10, 9), QPageLayout.Millimeter)
    w.setResolution(300)
    w.setTitle("Журнал сушки зерна")
    w.setCreator(f"DryerCalc {__version__}")
    p = QPainter(w)
    try:
        page = w.pageLayout().paintRectPixels(w.resolution())
        W, H = page.width(), page.height()
        mm = w.resolution() / 25.4
        f_title = QFont("Arial", 14, QFont.Bold)
        f_head = QFont("Arial", 8, QFont.Bold)
        f_cell = QFont("Arial", 9)
        f_small = QFont("Arial", 7)
        for pg in range(pages):
            if pg:
                w.newPage()
            y = 0
            p.setFont(f_title)
            p.drawText(QRectF(0, y, W, 9 * mm), Qt.AlignLeft | Qt.AlignVCenter, "Журнал сушки зерна")
            p.setFont(f_cell)
            fields = (f"Дата: {start:%d.%m.%Y}" if start else "Дата: ____________") + \
                     "     Смена: ______     Сушильщик: ______________________     " + \
                     (f"Партия: {batch}" if batch else "Партия: ________") + "     " + \
                     (f"Проход: {pass_no}" if pass_no else "Проход: ____") + f"     Лист {pg + 1} из {pages}"
            p.drawText(QRectF(0, y + 9 * mm, W, 7 * mm), Qt.AlignLeft | Qt.AlignVCenter, fields)
            y += 18 * mm
            head_h = 11 * mm
            foot_h = 9 * mm
            row_h = (H - y - head_h - foot_h) / per_page
            total_w = sum(k for _, k in BLANK_COLS)
            xs = [0.0]
            for _, k in BLANK_COLS:
                xs.append(xs[-1] + W * k / total_w)
            p.setPen(QPen(Qt.black, max(1, int(0.25 * mm))))
            p.setFont(f_head)
            for (name, _), x0, x1 in zip(BLANK_COLS, xs, xs[1:]):
                r = QRectF(x0, y, x1 - x0, head_h)
                p.drawRect(r)
                p.drawText(r, Qt.AlignCenter | Qt.TextWordWrap, name)
            y += head_h
            p.setFont(f_cell)
            for i in range(per_page):
                k = pg * per_page + i
                for x0, x1 in zip(xs, xs[1:]):
                    p.drawRect(QRectF(x0, y, x1 - x0, row_h))
                if start is not None and k < rows_total:
                    t = start + timedelta(minutes=step_min * k)
                    p.drawText(QRectF(xs[0], y, xs[1] - xs[0], row_h), Qt.AlignCenter, f"{t:%H:%M}")
                y += row_h
            p.setFont(f_small)
            p.drawText(QRectF(0, y + 1 * mm, W, foot_h), Qt.AlignLeft | Qt.TextWordWrap,
                       "Пробы каждые 30 мин. T блоков и разгрузку D пишите только при изменении (пусто = прежнее). "
                       "Остановки, запуски, смену силоса и окончание прохода — в примечании словами «Остановка», "
                       "«Запуск», «Закончился …». T и RH наружные — если есть термометр-гигрометр.")
    finally:
        p.end()
    return pages


def _fmt(v, nd: int = 1, unit: str = "") -> str:
    if v is None:
        return "—"
    s = f"{v:,.{nd}f}".replace(",", " ").replace(".", ",")
    return s + (f" {unit}" if unit else "")


def batch_report_data(b: Dict, rows: List[Dict], p, lhv: Optional[float] = None) -> Dict:
    start, end = parse_when(b.get("start") or ""), parse_when(b.get("end") or "")
    return {"batch": b, "balance": balance(b), "fuel": fuel_metrics(b, lhv),
            "journal": journal_summary(rows, p, start, end) if (start or end) else {"passes": [], "hours": 0.0}}


def batch_report_html(data: Dict, unit: str = "ед.") -> str:
    b, bal, fu, js = data["batch"], data["balance"], data["fuel"], data["journal"]
    e = html.escape

    def kv(rows):
        return "".join(f"<tr><td>{e(k)}</td><td align='right'><b>{e(v)}</b></td></tr>" for k, v in rows)

    head = [("Начало", b.get("start") or "—"), ("Конец", b.get("end") or "—"),
            ("Масса до сушки (весовая)", _fmt(_num(b.get("m_in")), 1, "т")),
            ("Влажность до сушки", _fmt(_num(b.get("w_in")), 1, "%")),
            ("Масса после сушки (весовая)", _fmt(_num(b.get("m_out")), 1, "т")),
            ("Влажность после сушки", _fmt(_num(b.get("w_out")), 1, "%")),
            ("Отходы", _fmt(_num(b.get("waste")), 1, "т"))]
    bal_rows = [
        ("Сухое вещество", _fmt(bal.get("dm"), 1, "т")),
        ("Масса после сушки по расчёту", _fmt(bal.get("m_out_expected"), 1, "т")),
        ("Расхождение с весовой", (_fmt(bal.get("m_diff"), 1, "т") + f" ({_fmt(bal.get('m_diff_pct'), 1, '%')})")
         if bal.get("m_diff") is not None else "—"),
        ("Влажность после сушки по массам", _fmt(bal.get("w_out_by_mass"), 2, "%")),
        ("Разница с лабораторной", _fmt(bal.get("w_diff"), 2, "п.п.")),
        ("Убыль массы по формуле", _fmt(bal.get("shrink_formula_pct"), 2, "%")),
        ("Убыль массы фактическая", _fmt(bal.get("shrink_actual_pct"), 2, "%")),
        ("Испарено воды (по весовой)", _fmt(bal.get("water_by_mass"), 1, "т")),
        ("Испарено воды (по влажности)", _fmt(bal.get("water_by_moisture"), 1, "т")),
        ("Тонно-проценты", _fmt(bal.get("ton_percent"), 0, "т·%")),
    ]
    fuel_rows = [
        ("Топливо", _fmt(fu.get("fuel"), 0, unit)),
        ("На 1 т испарённой воды", _fmt(fu.get("per_water"), 1, unit)),
        ("На 1 тонно-процент", _fmt(fu.get("per_ton_percent"), 2, unit)),
        ("На 1 т сухого зерна", _fmt(fu.get("per_ton_dried"), 2, unit)),
        ("Стоимость топлива", _fmt(fu.get("cost"), 0, "₽")),
        ("₽ на 1 т сухого зерна", _fmt(fu.get("cost_per_ton_dried"), 0, "₽")),
        ("₽ на 1 тонно-процент", _fmt(fu.get("cost_per_ton_percent"), 1, "₽")),
        ("₽ на 1 т воды", _fmt(fu.get("cost_per_water"), 0, "₽")),
        ("Тепло топлива", _fmt(fu.get("energy_gj"), 1, "ГДж")),
        ("Удельный расход тепла", _fmt(fu.get("mj_per_kg"), 2, "МДж/кг воды")),
        ("Тепловой КПД (2,5 МДж/кг / удельный расход)", _fmt(100 * fu["thermal_eff"], 0, "%") if fu.get("thermal_eff") else "—"),
    ]
    quality_rows = [("Назначение", b.get("purpose") or "—"),
                    ("Трещиноватость", _fmt(_num(b.get("cracks")), 1, "%")),
                    ("Битое/дроблёное", _fmt(_num(b.get("broken")), 1, "%")),
                    ("Натура", _fmt(_num(b.get("test_weight")), 0, "г/л")),
                    ("Всхожесть", _fmt(_num(b.get("germ")), 0, "%"))]
    passes = js.get("passes", [])
    pr = "".join(
        f"<tr><td>{i + 1}</td><td>{x['start']:%d.%m %H:%M}</td><td>{x['end']:%d.%m %H:%M}</td>"
        f"<td align='right'>{_fmt(x['hours'], 1)}</td><td align='right'>{_fmt(x['w_in'], 1)}</td>"
        f"<td align='right'>{_fmt(x['w_out_read'], 1)}</td><td align='right'>{_fmt(x['dw'], 1)}</td>"
        f"<td align='right'>{_fmt(x['d'], 0)}</td><td align='right'>{_fmt(x['t'], 0)}</td>"
        f"<td align='right'>{_fmt(x['unloaded'], 0)}</td></tr>" for i, x in enumerate(passes))
    note = e(b.get("note") or "")
    css = ("body{font-family:Arial;font-size:10pt} h1{font-size:15pt;margin:0} h2{font-size:11.5pt;margin:10px 0 4px} "
           "table{border-collapse:collapse} td,th{border:1px solid #999;padding:3px 6px} th{background:#eee}")
    return f"""<html><head><style>{css}</style></head><body>
<h1>Отчёт по партии «{e(str(b.get('name') or ''))}»</h1>
<p>Сформирован {datetime.now():%d.%m.%Y %H:%M}, DryerCalc {__version__}. {note}</p>
<h2>Партия</h2><table width='75%'>{kv(head)}</table>
<h2>Сверка по сухому веществу</h2><table width='75%'>{kv(bal_rows)}</table>
<p>Если влажность по массам расходится с лабораторной больше чем на 0,3–0,5 п.п. на нескольких партиях подряд —
влагомеры врут систематически или есть неучтённые потери (пыль, аспирация, просыпи).</p>
<h2>Топливо, деньги, тепло</h2><table width='75%'>{kv(fuel_rows)}</table>
<h2>Качество зерна</h2><table width='75%'>{kv(quality_rows)}</table>
<h2>Проходы по журналу</h2>
<table width='100%'><tr><th>№</th><th>Начало</th><th>Конец</th><th>Часов</th><th>W вх, %</th><th>W вых (вл-р), %</th>
<th>Съём, п.п.</th><th>D, с</th><th>T, °C</th><th>Выгр., т</th></tr>{pr or "<tr><td colspan=10>нет строк журнала за время партии</td></tr>"}</table>
<p>Работа по журналу: {_fmt(js.get('hours'), 1, 'ч')}. Съём — по истинной влажности (показание выходного влагомера
минус поправка).</p>
</body></html>"""


def _num(v):
    try:
        return float(str(v).replace(",", ".")) if v not in (None, "") else None
    except ValueError:
        return None


def html_to_pdf(html_text: str, path: str, landscape: bool = False) -> None:
    doc = QTextDocument()
    doc.setHtml(html_text)
    w = QPdfWriter(path)
    w.setPageSize(QPageSize(QPageSize.A4))
    w.setPageOrientation(QPageLayout.Landscape if landscape else QPageLayout.Portrait)
    w.setPageMargins(QMarginsF(15, 12, 15, 12), QPageLayout.Millimeter)
    w.setCreator(f"DryerCalc {__version__}")
    doc.print_(w)


def batch_report_xlsx(path: str, data: Dict, unit: str = "ед.") -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    b, bal, fu, js = data["batch"], data["balance"], data["fuel"], data["journal"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Партия"
    rows = [
        ("Партия", b.get("name")), ("Начало", b.get("start")), ("Конец", b.get("end")),
        ("Масса до сушки, т", _num(b.get("m_in"))), ("W до сушки, %", _num(b.get("w_in"))),
        ("Масса после сушки, т", _num(b.get("m_out"))), ("W после сушки, %", _num(b.get("w_out"))),
        ("Отходы, т", _num(b.get("waste"))), ("", None),
        ("Сухое вещество, т", bal.get("dm")), ("Масса после сушки по расчёту, т", bal.get("m_out_expected")),
        ("Расхождение с весовой, т", bal.get("m_diff")), ("W после сушки по массам, %", bal.get("w_out_by_mass")),
        ("Разница с лабораторной, п.п.", bal.get("w_diff")), ("Убыль по формуле, %", bal.get("shrink_formula_pct")),
        ("Убыль фактическая, %", bal.get("shrink_actual_pct")), ("Вода по весовой, т", bal.get("water_by_mass")),
        ("Вода по влажности, т", bal.get("water_by_moisture")), ("Тонно-проценты, т·%", bal.get("ton_percent")),
        ("", None), (f"Топливо, {unit}", fu.get("fuel")), (f"{unit} на 1 т воды", fu.get("per_water")),
        (f"{unit} на 1 т·%", fu.get("per_ton_percent")), (f"{unit} на 1 т сухого зерна", fu.get("per_ton_dried")),
        ("Стоимость топлива, ₽", fu.get("cost")), ("₽ на 1 т сухого зерна", fu.get("cost_per_ton_dried")),
        ("₽ на 1 т·%", fu.get("cost_per_ton_percent")), ("₽ на 1 т воды", fu.get("cost_per_water")),
        ("Тепло топлива, ГДж", fu.get("energy_gj")), ("Удельный расход тепла, МДж/кг воды", fu.get("mj_per_kg")),
        ("Тепловой КПД, %", 100 * fu["thermal_eff"] if fu.get("thermal_eff") else None), ("", None),
        ("Назначение", b.get("purpose")), ("Трещиноватость, %", _num(b.get("cracks"))),
        ("Битое/дроблёное, %", _num(b.get("broken"))), ("Натура, г/л", _num(b.get("test_weight"))),
        ("Всхожесть, %", _num(b.get("germ"))),
        ("Примечание", b.get("note")),
    ]
    for k, v in rows:
        ws.append([k, round(v, 3) if isinstance(v, float) else v])
    ws.column_dimensions["A"].width = 36
    ws.column_dimensions["B"].width = 22
    ws2 = wb.create_sheet("Проходы")
    hdr = ["№", "Начало", "Конец", "Часов", "W вх, %", "W вых (влагомер), %", "Съём, п.п.", "D, с", "T, °C", "Выгружено, т"]
    ws2.append(hdr)
    for c in ws2[1]:
        c.font = Font(bold=True)
    for i, x in enumerate(js.get("passes", [])):
        ws2.append([i + 1, x["start"], x["end"], round(x["hours"], 2), _r(x["w_in"]), _r(x["w_out_read"]),
                    _r(x["dw"]), _r(x["d"], 0), _r(x["t"], 0), _r(x["unloaded"], 1)])
    for col, wdt in zip("ABCDEFGHIJ", (5, 17, 17, 8, 9, 18, 11, 7, 7, 13)):
        ws2.column_dimensions[col].width = wdt
    for row in ws2.iter_rows(min_row=2, min_col=2, max_col=3):
        for c in row:
            c.number_format = "DD.MM.YYYY HH:MM"
    wb.save(path)


def _r(v, nd: int = 2):
    return round(v, nd) if isinstance(v, (int, float)) else v


def season_xlsx(path: str, batches: Sequence[Dict], unit: str = "ед.", lhv: Optional[float] = None) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Сезон"
    hdr = ["Партия", "Начало", "Конец", "Масса вх, т", "W вх, %", "Масса вых, т", "W вых, %", "Отходы, т",
           "Вода, т", "Тонно-проценты", "W вых по массам, %", "Разница с лаб., п.п.", f"Топливо, {unit}",
           f"{unit}/т воды", f"{unit}/т·%", "Стоимость, ₽", "₽/т сухого", "₽/т·%", "МДж/кг воды", "КПД, %",
           "Трещиноватость, %", "Битое, %", "Натура, г/л", "Всхожесть, %"]
    ws.append(hdr)
    for c in ws[1]:
        c.font = Font(bold=True)
    tot = {"m_in": 0.0, "water": 0.0, "tp": 0.0, "fuel": 0.0, "cost": 0.0}
    for b in batches:
        bal, fu = balance(b), fuel_metrics(b, lhv)
        water = bal.get("water_by_mass", bal.get("water_by_moisture"))
        ws.append([b.get("name"), b.get("start"), b.get("end"), _num(b.get("m_in")), _num(b.get("w_in")),
                   _num(b.get("m_out")), _num(b.get("w_out")), _num(b.get("waste")), _r(water),
                   _r(bal.get("ton_percent"), 0), _r(bal.get("w_out_by_mass")), _r(bal.get("w_diff")),
                   _r(fu.get("fuel"), 1), _r(fu.get("per_water")), _r(fu.get("per_ton_percent"), 3),
                   _r(fu.get("cost"), 0), _r(fu.get("cost_per_ton_dried"), 1), _r(fu.get("cost_per_ton_percent"), 2),
                   _r(fu.get("mj_per_kg")), _r(100 * fu["thermal_eff"], 0) if fu.get("thermal_eff") else None,
                   _num(b.get("cracks")), _num(b.get("broken")), _num(b.get("test_weight")), _num(b.get("germ"))])
        tot["m_in"] += _num(b.get("m_in")) or 0.0
        tot["water"] += water or 0.0
        tot["tp"] += bal.get("ton_percent") or 0.0
        tot["fuel"] += fu.get("fuel") or 0.0
        tot["cost"] += fu.get("cost") or 0.0
    ws.append([])
    ws.append(["Итого", None, None, round(tot["m_in"], 1), None, None, None, None, round(tot["water"], 1),
               round(tot["tp"], 0), None, None, round(tot["fuel"], 1),
               round(tot["fuel"] / tot["water"], 2) if tot["water"] else None,
               round(tot["fuel"] / tot["tp"], 3) if tot["tp"] else None, round(tot["cost"], 0)])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    for i, wdt in enumerate((14, 17, 17, 11, 8, 12, 8, 9, 9, 13, 16, 16, 12, 11, 11, 13, 11, 9, 12, 8, 14, 9, 11, 11)):
        ws.column_dimensions[chr(ord("A") + i)].width = wdt
    wb.save(path)
