"""Резервные копии базы (журнал, погода, партии, настройки): автоматически раз в сутки и вручную на флешку."""
from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

KEEP_DAYS = 14


def backup_dir(db_path: Path) -> Path:
    d = Path(db_path).parent / "backup"
    d.mkdir(parents=True, exist_ok=True)
    return d


def copy_db(src_conn: sqlite3.Connection, dst_path: str) -> None:
    """Целостная копия работающей базы (sqlite backup API — без риска скопировать «половину» записи)."""
    tmp = str(dst_path) + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    dst = sqlite3.connect(tmp)
    try:
        src_conn.backup(dst)
    finally:
        dst.close()
    os.replace(tmp, dst_path)


def auto_backup(conn: sqlite3.Connection, db_path: Path, keep: int = KEEP_DAYS, today: Optional[date] = None) -> Optional[Path]:
    """Копия за сегодня, если её ещё нет; хранятся последние keep копий. Возвращает путь новой копии."""
    today = today or date.today()
    d = backup_dir(db_path)
    target = d / f"dryer_{today:%Y-%m-%d}.db"
    made = None
    if not target.exists():
        copy_db(conn, str(target))
        made = target
    olds = sorted(d.glob("dryer_????-??-??.db"))
    for old in olds[:-keep] if keep > 0 else []:
        try:
            old.unlink()
        except OSError:
            pass
    return made


def list_backups(db_path: Path) -> List[Path]:
    return sorted(backup_dir(db_path).glob("dryer_*.db"), reverse=True)


def check_db(path: str) -> str:
    """Пустая строка — файл похож на базу DryerCalc; иначе текст ошибки."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return "файл базы повреждён"
        finally:
            conn.close()
    except sqlite3.Error as e:
        return f"это не база SQLite ({e})"
    if not {"settings", "journal"} <= names:
        return "в файле нет таблиц журнала и настроек DryerCalc"
    return ""


def restore_into(conn: sqlite3.Connection, src_path: str) -> None:
    """Заменить содержимое открытой базы содержимым файла (текущая база перед этим сохраняется в backup)."""
    err = check_db(src_path)
    if err:
        raise ValueError(err)
    src = sqlite3.connect(src_path)
    try:
        src.backup(conn)
    finally:
        src.close()


def safety_copy(conn: sqlite3.Connection, db_path: Path) -> Path:
    target = backup_dir(db_path) / f"before_restore_{datetime.now():%Y-%m-%d_%H%M%S}.db"
    copy_db(conn, str(target))
    return target
