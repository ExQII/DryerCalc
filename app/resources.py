"""Пути к ресурсам: работает и из исходников, и из собранного .exe (PyInstaller)."""
from __future__ import annotations

import sys
from pathlib import Path


def base_dir() -> Path:
    """Папка с ресурсами: во временной распаковке .exe или корень проекта."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent


def resource_path(rel: str) -> Path:
    return base_dir() / rel
