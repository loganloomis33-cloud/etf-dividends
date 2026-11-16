"""Tiny SQLite-backed key/value cache with TTL (thread-safe)."""
import json
import os
import sqlite3
import threading
import time

_CACHE_DIR = os.environ.get("CACHE_DIR", os.path.join(os.path.dirname(__file__), ".cache"))
os.makedirs(_CACHE_DIR, exist_ok=True)
_DB_PATH = os.path.join(_CACHE_DIR, "cache.sqlite3")
_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(_DB_PATH, timeout=30, check_same_thread=False)
    c.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, ts REAL)")
    return c


_db = _conn()


def get(key, ttl_seconds):
    with _lock:
        row = _db.execute("SELECT v, ts FROM kv WHERE k=?", (key,)).fetchone()
    if not row:
        return None
    v, ts = row
    if ttl_seconds is not None and time.time() - ts > ttl_seconds:
        return None
    return json.loads(v)


def set(key, value):  # noqa: A001 - simple module API
    with _lock:
        _db.execute(
            "INSERT OR REPLACE INTO kv (k, v, ts) VALUES (?, ?, ?)",
            (key, json.dumps(value), time.time()),
        )
        _db.commit()


def clear():
    with _lock:
        _db.execute("DELETE FROM kv")
        _db.commit()
