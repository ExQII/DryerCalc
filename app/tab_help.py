"""Вкладка «Справка»: инструкция, правила работы и техническое описание (интернет не нужен)."""
from __future__ import annotations

import shutil

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QTabWidget,
                               QTextBrowser, QVBoxLayout, QWidget)

from .resources import resource_path

MANUAL = "docs/manual_ru.txt"
RULES = "docs/rules_ru.txt"
README = "README.md"

# (файл в программе, подвкладка, имя файла при сохранении, фильтр)
DOCS = ((MANUAL, "Инструкция для пользователя", "Инструкция DryerCalc.txt", "Текст (*.txt)"),
        (RULES, "Как правильно работать", "Правила работы DryerCalc.txt", "Текст (*.txt)"),
        (README, "Для инженера (как устроены модели)", "DryerCalc — техническое описание.md", "Markdown (*.md)"))


class HelpTab(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Найти:"))
        self.find = QLineEdit()
        self.find.setPlaceholderText("слово из текста, например «рециркуляция» — Enter: следующее")
        self.find.returnPressed.connect(self._find_next)
        bar.addWidget(self.find, 1)
        b = QPushButton("Найти далее")
        b.clicked.connect(self._find_next)
        bar.addWidget(b)
        b = QPushButton("Сохранить в файл…")
        b.setToolTip("Сохранить открытый документ (например, на флешку или чтобы распечатать)")
        b.clicked.connect(self._save_current)
        bar.addWidget(b)
        lay.addLayout(bar)
        self.tabs = QTabWidget()
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSize(10)
        self.manual = QPlainTextEdit()
        self.rules = QPlainTextEdit()
        for w in (self.manual, self.rules):
            w.setReadOnly(True)
            w.setFont(mono)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        self.widgets = (self.manual, self.rules, self.view)
        for w, (_, title, _, _) in zip(self.widgets, DOCS):
            self.tabs.addTab(w, title)
        lay.addWidget(self.tabs, 1)
        self._loaded = False

    def on_show(self) -> None:
        if self._loaded:
            return
        for w, (rel, _, _, _) in zip((self.manual, self.rules), DOCS):
            src = resource_path(rel)
            w.setPlainText(src.read_text(encoding="utf-8-sig") if src.exists() else f"Файл {rel} не найден.")
        src = resource_path(README)
        if src.exists():
            self.view.setMarkdown(src.read_text(encoding="utf-8"))
        else:
            self.view.setPlainText("Файл технического описания не найден.")
        self._loaded = True

    def _find_next(self) -> None:
        text = self.find.text().strip()
        if not text:
            return
        w = self.widgets[self.tabs.currentIndex()]
        if not w.find(text):
            cur = w.textCursor()
            cur.movePosition(cur.MoveOperation.Start)
            w.setTextCursor(cur)
            w.find(text)

    def _save_current(self) -> None:
        rel, _, name, flt = DOCS[self.tabs.currentIndex()]
        self._save(rel, name, flt)

    def _save(self, rel: str, name: str, flt: str) -> None:
        src = resource_path(rel)
        if not src.exists():
            return
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить", name, flt)
        if path:
            if rel.endswith(".txt"):
                text = src.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\n", "\r\n")
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    f.write(text)
            else:
                shutil.copyfile(src, path)
