"""Вкладка «Справка»: инструкция для пользователя и техническое описание (интернет не нужен)."""
from __future__ import annotations

import shutil

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QTabWidget,
                               QTextBrowser, QVBoxLayout, QWidget)

from .resources import resource_path

MANUAL = "docs/manual_ru.txt"
README = "README.md"


class HelpTab(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Найти:"))
        self.find = QLineEdit()
        self.find.setPlaceholderText("слово из инструкции, например «рециркуляция» — Enter: следующее")
        self.find.returnPressed.connect(self._find_next)
        bar.addWidget(self.find, 1)
        b = QPushButton("Найти далее")
        b.clicked.connect(self._find_next)
        bar.addWidget(b)
        b = QPushButton("Сохранить инструкцию в файл…")
        b.clicked.connect(lambda: self._save(MANUAL, "Инструкция DryerCalc.txt", "Текст (*.txt)"))
        bar.addWidget(b)
        b = QPushButton("Сохранить техническое описание…")
        b.clicked.connect(lambda: self._save(README, "DryerCalc — техническое описание.md", "Markdown (*.md)"))
        bar.addWidget(b)
        lay.addLayout(bar)
        self.tabs = QTabWidget()
        self.manual = QPlainTextEdit()
        self.manual.setReadOnly(True)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSize(10)
        self.manual.setFont(mono)
        self.tabs.addTab(self.manual, "Инструкция для пользователя")
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        self.tabs.addTab(self.view, "Для инженера (как устроены модели)")
        lay.addWidget(self.tabs, 1)
        self._loaded = False

    def on_show(self) -> None:
        if self._loaded:
            return
        src = resource_path(MANUAL)
        self.manual.setPlainText(src.read_text(encoding="utf-8-sig") if src.exists() else "Файл инструкции не найден.")
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
        w = self.manual if self.tabs.currentIndex() == 0 else self.view
        if not w.find(text):
            cur = w.textCursor()
            cur.movePosition(cur.MoveOperation.Start)
            w.setTextCursor(cur)
            w.find(text)

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
