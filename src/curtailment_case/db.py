"""SQLite 存储层。

双时间设计：
- 业务时间（starts_at/ends_at/interval_start...）：事实生效的区间；
- 事务时间（recorded_at）：系统得知该事实的时刻（取自注入时钟）。

所有原始表只追加不更新，"当时可见的信息"通过 recorded_at <= as_of 重建。
结算版本表一旦 CONFIRMED 即不可变，任何变更只能产生新的版本行。
"""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS grid_points (
    grid_point_id TEXT PRIMARY KEY,
    name          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plants (
    plant_id      TEXT PRIMARY KEY,
    grid_point_id TEXT NOT NULL REFERENCES grid_points(grid_point_id),
    name          TEXT NOT NULL,
    capacity_mw   REAL NOT NULL CHECK (capacity_mw > 0)
);

-- 调度指令事件流：同一 instruction_id 的 下发/补发(issue)、更正(correct)、撤销(revoke)
CREATE TABLE IF NOT EXISTS instruction_events (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    instruction_id TEXT NOT NULL,
    plant_id       TEXT NOT NULL REFERENCES plants(plant_id),
    event_type     TEXT NOT NULL CHECK (event_type IN ('issue', 'correct', 'revoke')),
    cap_mw         REAL,
    starts_at      TEXT NOT NULL,
    ends_at        TEXT NOT NULL,
    issued_at      TEXT NOT NULL,   -- 调度侧声称的签发时刻（业务时间）
    recorded_at    TEXT NOT NULL,   -- 本系统记录时刻（事务时间）
    reason         TEXT NOT NULL DEFAULT '',
    actor          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_instruction_plant ON instruction_events(plant_id, recorded_at);

CREATE TABLE IF NOT EXISTS available_power (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id       TEXT NOT NULL REFERENCES plants(plant_id),
    interval_start TEXT NOT NULL,
    interval_end   TEXT NOT NULL,
    avg_mw         REAL NOT NULL CHECK (avg_mw >= 0),
    source         TEXT NOT NULL DEFAULT '',
    recorded_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_avail_plant ON available_power(plant_id, recorded_at);

CREATE TABLE IF NOT EXISTS metered_energy (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id       TEXT NOT NULL REFERENCES plants(plant_id),
    interval_start TEXT NOT NULL,
    interval_end   TEXT NOT NULL,
    energy_mwh     REAL NOT NULL CHECK (energy_mwh >= 0),
    source         TEXT NOT NULL DEFAULT '',
    recorded_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meter_plant ON metered_energy(plant_id, recorded_at);

CREATE TABLE IF NOT EXISTS equipment_faults (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id    TEXT NOT NULL REFERENCES plants(plant_id),
    starts_at   TEXT NOT NULL,
    ends_at     TEXT NOT NULL,
    derated_mw  REAL NOT NULL CHECK (derated_mw >= 0),
    description TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS network_constraints (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    grid_point_id TEXT NOT NULL REFERENCES grid_points(grid_point_id),
    starts_at     TEXT NOT NULL,
    ends_at       TEXT NOT NULL,
    limit_mw      REAL NOT NULL CHECK (limit_mw >= 0),
    description   TEXT NOT NULL DEFAULT '',
    recorded_at   TEXT NOT NULL
);

-- 争议案件：同一场站同一限发区间只允许一个案件（防重复申诉）
CREATE TABLE IF NOT EXISTS cases (
    case_pk        INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id        TEXT NOT NULL UNIQUE,
    plant_id       TEXT NOT NULL REFERENCES plants(plant_id),
    starts_at      TEXT NOT NULL,
    ends_at        TEXT NOT NULL,
    attribution    TEXT NOT NULL,
    state          TEXT NOT NULL CHECK (state IN ('open', 'appealed', 'reviewed', 'settled')),
    reason         TEXT NOT NULL DEFAULT '',
    appeal_reason  TEXT NOT NULL DEFAULT '',
    snapshot_json  TEXT NOT NULL,   -- 立案时计算快照（含来源引用），此后不随新数据改写
    opened_at      TEXT NOT NULL,
    appealed_at    TEXT,
    reviewed_at    TEXT,
    settled_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_cases_plant ON cases(plant_id, starts_at, ends_at);

-- 复核结论版本：结论变更只追加新版本
CREATE TABLE IF NOT EXISTS review_versions (
    review_pk             INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id               TEXT NOT NULL REFERENCES cases(case_id),
    version               INTEGER NOT NULL,
    outcome               TEXT NOT NULL CHECK (outcome IN ('upheld', 'rejected', 'partial')),
    adjusted_attribution  TEXT,
    adjusted_energy_mwh   REAL,
    note                  TEXT NOT NULL DEFAULT '',
    created_at            TEXT NOT NULL,
    UNIQUE (case_id, version)
);

-- 结算版本：CONFIRMED 后整行不可变；变更只能准备新版本
CREATE TABLE IF NOT EXISTS settlement_versions (
    settlement_pk         INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id         TEXT NOT NULL UNIQUE,
    plant_id              TEXT NOT NULL REFERENCES plants(plant_id),
    period                TEXT NOT NULL,
    version_no            INTEGER NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('draft', 'confirmed')),
    payload_json          TEXT NOT NULL,   -- {"lines": [...], "pending_cases": [...]}
    total_curtailed_mwh   REAL NOT NULL,
    total_compensable_mwh REAL NOT NULL,
    content_hash          TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    confirmed_at          TEXT,
    UNIQUE (plant_id, period, version_no)
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
