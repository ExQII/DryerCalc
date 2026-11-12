"""Локальное хранилище (SQLite): журнал и настройки.

Культуры (v1.8). У каждой культуры свои журнал, модель, партии, очередь, пары влагомера и поля ввода; сушилка,
погода, напоминания, режим и оформление — общие. Активная культура — Storage.crop (ключ настроек «crop»).
Журнал: колонка crop (пусто — кукуруза: строки прежних версий). Настройки из CROP_KEYS для кукурузы лежат под
прежними ключами («params», «batches»…), для другой культуры — «params@wheat», «batches@wheat»… Поэтому база
прежней версии открывается без переноса данных, а всё, что в ней было, остаётся кукурузой."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import crops
from .calibration import FIELDS, TEXT_FIELDS
from .model import DryerParams

# настройки, которые у каждой культуры свои
CROP_KEYS = frozenset(("params", "ui", "ui_simple", "ui_live", "ui_weekend", "ui_queue", "ui_silos", "batches",
                       "queue", "meter_pairs"))


def default_dir() -> Path:
    base = os.environ.get("APPDATA") or os.path.join(str(Path.home()), ".local", "share")
    d = Path(base) / "DryerCalc"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Storage:
    def __init__(self, path: Optional[str] = None):
        self.path = Path(path) if path else default_dir() / "dryer.db"
        self.conn = sqlite3.connect(str(self.path))
        self.crop = crops.DEFAULT
        self.migrate()
        self.load_crop()

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
        if "crop" not in have and "crop" not in {r[1] for r in self.conn.execute("PRAGMA table_info(journal)")}:
            self.conn.execute("ALTER TABLE journal ADD COLUMN crop TEXT")
        self.conn.execute("CREATE TABLE IF NOT EXISTS weather(dt TEXT PRIMARY KEY, t REAL, rh REAL, src TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, "
                          "detail TEXT)")
        self.conn.commit()

    # --- культура ------------------------------------------------------
    def load_crop(self) -> str:
        """Активная культура из базы (после открытия и после восстановления из копии)."""
        self.crop = crops.DEFAULT
        d = self.get_json("crop", {})
        key = d.get("key") if isinstance(d, dict) else None
        self.crop = key if key in crops.BY_KEY else crops.DEFAULT
        return self.crop

    def set_crop(self, key: str) -> bool:
        """Сделать культуру активной. True — культура открыта впервые: записаны её стартовая модель (пересчёт модели
        сушилки на кукурузе, crops.start_params) и стартовые значения полей ввода."""
        if key not in crops.BY_KEY:
            raise ValueError(f"нет такой культуры: {key}")
        corn = self.params_of(crops.DEFAULT)
        self.crop = key
        self.set_json("crop", {"key": key})
        if key == crops.DEFAULT or isinstance(self.get_json("params"), dict):
            return False
        c = crops.get(key)
        self.save_params(crops.start_params(c, corn))
        for k, v in crops.ui_defaults(c).items():
            self.set_json(k, v)
        return True

    def _k(self, key: str) -> str:
        return key if self.crop == crops.DEFAULT or key not in CROP_KEYS else f"{key}@{self.crop}"

    def params_of(self, crop: str) -> DryerParams:
        """Модель культуры, не меняя активную (для кукурузы без сохранённой модели — стартовая программы)."""
        key = "params" if crop == crops.DEFAULT else f"params@{crop}"
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        try:
            d = json.loads(row[0]) if row else None
        except (TypeError, ValueError):
            d = None
        if isinstance(d, dict):
            if "natura" not in d and crop != crops.DEFAULT:
                d["natura"] = crops.get(crop).density      # модель культуры сохранена до 1.9.2 — натура её профиля
            return DryerParams.from_dict(d)
        return crops.start_params(crops.get(crop), self.params_of(crops.DEFAULT) if crop != crops.DEFAULT else None)

    def crop_summary(self) -> Dict[str, Dict]:
        """По каждой культуре: строк журнала, есть ли своя модель и её статус — для раздела «Культура»."""
        n = {(k or crops.DEFAULT): c for k, c in
             self.conn.execute("SELECT COALESCE(NULLIF(crop, ''), ?), COUNT(*) FROM journal GROUP BY 1", (crops.DEFAULT,))}
        out = {}
        for c in crops.CROPS:
            key = "params" if c.key == crops.DEFAULT else f"params@{c.key}"
            has = self.conn.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone() is not None
            p = self.params_of(c.key)
            out[c.key] = {"rows": int(n.get(c.key, 0)), "opened": has or c.key == crops.DEFAULT, "params": p}
        return out

    # --- настройки -----------------------------------------------------
    def get_json(self, key: str, default: Any = None) -> Any:
        key = self._k(key)
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            return default

    def set_json(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                          (self._k(key), json.dumps(value, ensure_ascii=False)))
        self.conn.commit()

    def load_params(self) -> DryerParams:
        return self.params_of(self.crop)

    def save_params(self, p: DryerParams) -> None:
        self.set_json("params", p.to_dict())

    # --- журнал --------------------------------------------------------
    def load_journal(self) -> List[Dict]:
        cur = self.conn.execute(f"SELECT {', '.join(FIELDS)} FROM journal WHERE COALESCE(NULLIF(crop, ''), ?) = ? "
                                f"ORDER BY id", (crops.DEFAULT, self.crop))
        rows = [dict(zip(FIELDS, r)) for r in cur.fetchall()]
        for r in rows:
            for f in TEXT_FIELDS:
                if r.get(f) is None:
                    r[f] = ""
        return rows

    def replace_journal(self, rows: List[Dict]) -> None:
        self.conn.execute("DELETE FROM journal WHERE COALESCE(NULLIF(crop, ''), ?) = ?", (crops.DEFAULT, self.crop))
        ph = ", ".join("?" for _ in FIELDS)
        self.conn.executemany(
            f"INSERT INTO journal({', '.join(FIELDS)}, crop) VALUES({ph}, ?)",
            [tuple(r.get(f) for f in FIELDS) + (self.crop,) for r in rows])
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
