"""SQLite 存储层。

事实表(指令、可用功率、表计读数、停机申报)只追加、不改写:
更正/补发/撤销都体现为新行,配合 recorded_at 可还原任意时点的可见信息。
结算版本表确认后不可变,没有 UPDATE 路径。
"""

from __future__ import annotations

import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS grid_points (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    recorded_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS plants (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    grid_point_id TEXT NOT NULL REFERENCES grid_points(id),
    capacity_mw   REAL NOT NULL,
    recorded_at   INTEGER NOT NULL
);

-- 调度指令:同一 instruction_id 的补发/更正/撤销按 version 递增追加。
CREATE TABLE IF NOT EXISTS instructions (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    instruction_id TEXT NOT NULL,
    version        INTEGER NOT NULL,
    plant_id       TEXT NOT NULL REFERENCES plants(id),
    target_mw      REAL,
    start_ts       INTEGER NOT NULL,
    end_ts         INTEGER NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    issued_at      INTEGER NOT NULL,   -- 业务签发时间
    recorded_at    INTEGER NOT NULL,   -- 系统记录时间(注入时钟)
    source         TEXT,
    UNIQUE (instruction_id, version)
);

-- 可用功率估计:允许同一 (plant_id, ts) 多次上报,取 recorded_at 最新者。
CREATE TABLE IF NOT EXISTS available_power (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id     TEXT NOT NULL REFERENCES plants(id),
    ts           INTEGER NOT NULL,
    mw           REAL NOT NULL,
    source       TEXT,
    recorded_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_avail ON available_power (plant_id, ts);

-- 表计电量:同一区间可重发(迟到/更正),取 recorded_at 最新者。
CREATE TABLE IF NOT EXISTS meter_readings (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id     TEXT NOT NULL REFERENCES plants(id),
    start_ts     INTEGER NOT NULL,
    end_ts       INTEGER NOT NULL,
    kwh          REAL NOT NULL,
    source       TEXT,
    recorded_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meter ON meter_readings (plant_id, start_ts);

-- 场站停机申报(设备故障信号)。
CREATE TABLE IF NOT EXISTS outages (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id     TEXT NOT NULL REFERENCES plants(id),
    start_ts     INTEGER NOT NULL,
    end_ts       INTEGER NOT NULL,
    reason       TEXT,
    source       TEXT,
    recorded_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS appeals (
    id          TEXT PRIMARY KEY,
    plant_id    TEXT NOT NULL REFERENCES plants(id),
    start_ts    INTEGER NOT NULL,
    end_ts      INTEGER NOT NULL,
    attribution TEXT,
    reason      TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN
        ('submitted', 'under_review', 'accepted', 'rejected', 'withdrawn', 'settled')),
    created_at  INTEGER NOT NULL
);

-- 复核结论:可变更,按 version 追加,旧版本保留。
CREATE TABLE IF NOT EXISTS reviews (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    appeal_id    TEXT NOT NULL REFERENCES appeals(id),
    version      INTEGER NOT NULL,
    decision     TEXT NOT NULL CHECK (decision IN ('accepted', 'rejected')),
    adjusted_kwh REAL,
    attribution  TEXT,
    note         TEXT,
    decided_at   INTEGER NOT NULL,
    UNIQUE (appeal_id, version)
);

-- 结算版本:确认即不可变(items_json + digest 快照),变化只能产生新版本。
CREATE TABLE IF NOT EXISTS settlements (
    id           TEXT PRIMARY KEY,
    plant_id     TEXT NOT NULL REFERENCES plants(id),
    period       TEXT NOT NULL,
    version_no   INTEGER NOT NULL,
    items_json   TEXT NOT NULL,
    total_kwh    REAL NOT NULL,
    digest       TEXT NOT NULL,
    confirmed_at INTEGER NOT NULL,
    UNIQUE (plant_id, period, version_no)
);
"""


class Database:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def one(self, sql: str, params: tuple = ()):
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()) -> list:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def close(self) -> None:
        self.conn.close()
