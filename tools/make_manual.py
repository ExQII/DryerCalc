"""Готовит текстовые файлы для Блокнота Windows: UTF-8 с BOM и переводы строк CRLF.

python tools/make_manual.py [папка]   ->   <папка>/Инструкция DryerCalc.txt
                                          <папка>/Правила работы DryerCalc.txt   (по умолчанию dist)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = (("manual_ru.txt", "Инструкция DryerCalc.txt"),
        ("rules_ru.txt", "Правила работы DryerCalc.txt"))
out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dist")
os.makedirs(out_dir, exist_ok=True)
for src, name in DOCS:
    with open(os.path.join(ROOT, "docs", src), encoding="utf-8-sig") as f:
        text = f.read().replace("\r\n", "\n")
    dst = os.path.join(out_dir, name)
    with open(dst, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(text)
    print(dst)
