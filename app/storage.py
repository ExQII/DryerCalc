"""Локальное хранилище (SQLite): журнал и настройки."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .calibration import FIELDS, TEXT_FIELDS
from .model import DryerParams


def default_dir() -> Path:
    base = os.environ.get("APPDATA") or os.path.join(str(Path.home()), ".local", "share")
    d = Path(base) / "DryerCalc"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Storage:
    def __init__(self, path: Optional[str] = None):
        self.path = Path(path) if path else default_dir() / "dryer.db"
        self.conn = sqlite3.connect(str(self.path))
        self.migrate()

    def migrate(self) -> None:
        """Создать недостающие таблицы и колонки (старые базы и восстановленные копии)."""
        self.conn.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
        cols = ", ".join(f"{f} {'TEXT' if f in TEXT_FIELDS else 'REAL'}" for f in FIELDS)
        self.conn.execute(f"CREATE TABLE IF NOT EXISTS journal(id INTEGER PRIMARY KEY AUTOINCREMENT, {cols})")
        # миграция старых баз: добавить недостающие колонки
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(journal)").fetchall()}
        for f in FIELDS:
            if f not in have:
                self.conn.execute(f"ALTER TABLE journal ADD COLUMN {f} {'TEXT' if f in TEXT_FIELDS else 'REAL'}")
        self.conn.execute("CREATE TABLE IF NOT EXISTS weather(dt TEXT PRIMARY KEY, t REAL, rh REAL, src TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, "
                          "detail TEXT)")
        self.conn.commit()

    # --- настройки -----------------------------------------------------
    def get_json(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            return default

    def set_json(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                          (key, json.dumps(value, ensure_ascii=False)))
        self.conn.commit()

    def load_params(self) -> DryerParams:
        d = self.get_json("params")
        return DryerParams.from_dict(d) if isinstance(d, dict) else DryerParams()

    def save_params(self, p: DryerParams) -> None:
        self.set_json("params", p.to_dict())

    # --- журнал --------------------------------------------------------
    def load_journal(self) -> List[Dict]:
        cur = self.conn.execute(f"SELECT {', '.join(FIELDS)} FROM journal ORDER BY id")
        rows = [dict(zip(FIELDS, r)) for r in cur.fetchall()]
        for r in rows:
            for f in TEXT_FIELDS:
                if r.get(f) is None:
                    r[f] = ""
        return rows

    def replace_journal(self, rows: List[Dict]) -> None:
        self.conn.execute("DELETE FROM journal")
        ph = ", ".join("?" for _ in FIELDS)
        self.conn.executemany(
            f"INSERT INTO journal({', '.join(FIELDS)}) VALUES({ph})",
            [tuple(r.get(f) for f in FIELDS) for r in rows])
        self.conn.commit()

    # --- погода ----------------------------------------------------------
    def load_weather(self) -> List[Dict]:
        out = []
        for dt, t, rh, src in self.conn.execute("SELECT dt, t, rh, src FROM weather ORDER BY dt"):
            try:
                out.append({"dt": datetime.fromisoformat(dt), "t": t, "rh": rh, "src": src or ""})
            except ValueError:
                continue
        return out

    def replace_weather(self, points: List[Dict]) -> None:
        self.conn.execute("DELETE FROM weather")
        self.conn.executemany(
            "INSERT OR REPLACE INTO weather(dt, t, rh, src) VALUES(?, ?, ?, ?)",
            [(p["dt"].strftime("%Y-%m-%dT%H:%M"), p.get("t"), p.get("rh"), p.get("src", "")) for p in points])
        self.conn.commit()

    # --- списки (партии, очередь, пары влагомера) в настройках ------------
    def get_list(self, key: str) -> List[Dict]:
        v = self.get_json(key, [])
        return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []

    def set_list(self, key: str, items: List[Dict]) -> None:
        self.set_json(key, items)

    # --- журнал событий (пилотный запуск: как пользуются программой) ------
    def log_event(self, kind: str, detail: Any = None, ts: Optional[datetime] = None) -> None:
        try:
            self.conn.execute("INSERT INTO events(ts, kind, detail) VALUES(?, ?, ?)",
                              ((ts or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S"), kind,
                               json.dumps(detail, ensure_ascii=False) if detail is not None else None))
            self.conn.commit()
        except sqlite3.Error:
            pass

    def load_events(self, kind: Optional[str] = None) -> List[Dict]:
        q = "SELECT id, ts, kind, detail FROM events" + (" WHERE kind=?" if kind else "") + " ORDER BY id"
        out = []
        for i, ts, k, det in self.conn.execute(q, (kind,) if kind else ()):
            try:
                d = json.loads(det) if det else None
            except ValueError:
                d = det
            try:
                t = datetime.fromisoformat(ts)
            except (TypeError, ValueError):
                continue
            out.append({"id": i, "ts": t, "kind": k, "detail": d})
        return out

    def clear_events(self, keep_feedback: bool = True) -> None:
        self.conn.execute("DELETE FROM events" + (" WHERE kind != 'feedback'" if keep_feedback else ""))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
