"""
Refresh History Collector - Tự động thu thập & tích lũy lịch sử refresh Power BI
Lưu vào SQLite (dedup tự động theo request_id), hỗ trợ export CSV.

Cách dùng:
  python refresh_history_collector.py              # Chạy 1 lần (thu thập ngay)
  python refresh_history_collector.py --schedule   # Chạy định kỳ mỗi 6 giờ
  python refresh_history_collector.py --export     # Xuất refresh history ra Parquet
  python refresh_history_collector.py --export-views # Xuất page view events ra Parquet
  python refresh_history_collector.py --collect-audit-events # Kéo full audit events (mọi activity type)
  python refresh_history_collector.py --stats      # Xem thống kê nhanh
  python refresh_history_collector.py --list-ignored
  python refresh_history_collector.py --ignore-key <ws_id:ds_id>
  python refresh_history_collector.py --unignore-key <ws_id:ds_id>
  python refresh_history_collector.py --validate-ignored
  python refresh_history_collector.py --collect-views
  python refresh_history_collector.py --collect-views --views-lookback-days 28
"""

import csv
import json
import sys
import os
import time
import hashlib
import sqlite3
import yaml
import argparse
import pandas as pd
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Optional, Tuple, List, Dict, Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from powerbi_client import PowerBIClient

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "powerbi_dashboard.db")
CONFIG_FILE = "pbi_config.yaml"
MAX_TOP = 60  # Giới hạn cứng Power BI REST API
DEFAULT_BACKUP_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "backups", "db"
)
DEFAULT_BACKUP_FILE = "refresh_history_latest.db"
SCHEDULE_LOCK_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "refresh_history_collector.schedule.lock"
)
RUN_LOCK_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "refresh_history_collector.run.lock"
)

# ─── Export Path ──────────────────────────────────
EXPORT_DIR = os.getenv("PBI_EXPORT_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports"))
PARQUET_FILE = os.path.join(EXPORT_DIR, "refresh_history_master.parquet")
PARQUET_VIEW_EVENTS_FILE = os.path.join(EXPORT_DIR, "page_view_master.parquet")



# ─── Database ──────────────────────────────────────
def init_db():
    """Tạo bảng SQLite nếu chưa tồn tại."""
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS refresh_history (
            request_id     TEXT PRIMARY KEY,
            dataset_id     TEXT NOT NULL,
            dataset_name   TEXT,
            workspace_id   TEXT,
            workspace_name TEXT,
            status         TEXT,
            refresh_type   TEXT,
            start_time     TEXT,
            end_time       TEXT,
            duration_seconds REAL,
            error          TEXT,
            collected_at   TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_start_time ON refresh_history(start_time)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_dataset ON refresh_history(dataset_name)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pbi_report_view_events_raw (
            event_key          TEXT PRIMARY KEY,
            api_event_id       TEXT,
            activity_id        TEXT,
            creation_time_service TEXT,
            event_date_service TEXT,
            creation_time_utc  TEXT NOT NULL,
            event_date_utc     TEXT NOT NULL,
            activity           TEXT,
            operation          TEXT,
            user_id            TEXT,
            user_key           TEXT,
            user_type          TEXT,
            workspace_id       TEXT,
            workspace_name     TEXT,
            report_id          TEXT,
            report_name        TEXT,
            dataset_id         TEXT,
            artifact_id        TEXT,
            client_ip          TEXT,
            user_agent         TEXT,
            consumption_method TEXT,
            distribution_method TEXT,
            raw_payload_json   TEXT NOT NULL,
            collected_at       TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(pbi_report_view_events_raw)")
    }
    if "api_event_id" not in existing_cols:
        conn.execute("ALTER TABLE pbi_report_view_events_raw ADD COLUMN api_event_id TEXT")
    if "activity_id" not in existing_cols:
        conn.execute("ALTER TABLE pbi_report_view_events_raw ADD COLUMN activity_id TEXT")
    if "creation_time_service" not in existing_cols:
        conn.execute("ALTER TABLE pbi_report_view_events_raw ADD COLUMN creation_time_service TEXT")
    if "event_date_service" not in existing_cols:
        conn.execute("ALTER TABLE pbi_report_view_events_raw ADD COLUMN event_date_service TEXT")
    conn.execute(
        """
        DELETE FROM pbi_report_view_events_raw
        WHERE rowid NOT IN (
            SELECT MIN(rowid)
            FROM pbi_report_view_events_raw
            WHERE api_event_id IS NOT NULL AND api_event_id <> ''
            GROUP BY api_event_id
        )
        AND api_event_id IS NOT NULL AND api_event_id <> ''
        """
    )
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pbi_view_date_report
        ON pbi_report_view_events_raw(event_date_utc, report_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pbi_view_date_user
        ON pbi_report_view_events_raw(event_date_utc, user_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pbi_view_creation_time
        ON pbi_report_view_events_raw(creation_time_utc)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pbi_view_service_date
        ON pbi_report_view_events_raw(event_date_service)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pbi_view_api_event_id
        ON pbi_report_view_events_raw(api_event_id)
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_pbi_view_api_event_id
        ON pbi_report_view_events_raw(api_event_id)
        WHERE api_event_id IS NOT NULL AND api_event_id <> ''
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pbi_audit_events_raw (
            event_key             TEXT PRIMARY KEY,
            api_event_id          TEXT,
            activity_id           TEXT,
            creation_time_service TEXT,
            event_date_service    TEXT,
            creation_time_utc     TEXT NOT NULL,
            event_date_utc        TEXT NOT NULL,
            activity              TEXT,
            operation             TEXT,
            item_name             TEXT,
            object_id             TEXT,
            object_type           TEXT,
            artifact_id           TEXT,
            artifact_kind         TEXT,
            workspace_id          TEXT,
            workspace_name        TEXT,
            dashboard_id          TEXT,
            dashboard_name        TEXT,
            report_id             TEXT,
            report_name           TEXT,
            dataset_id            TEXT,
            app_id                TEXT,
            app_name              TEXT,
            capacity_id           TEXT,
            capacity_name         TEXT,
            user_id               TEXT,
            user_key              TEXT,
            user_type             TEXT,
            client_ip             TEXT,
            user_agent            TEXT,
            consumption_method    TEXT,
            distribution_method   TEXT,
            raw_payload_json      TEXT NOT NULL,
            collected_at          TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    audit_cols = {
        "event_key",
        "api_event_id",
        "activity_id",
        "creation_time_service",
        "event_date_service",
        "creation_time_utc",
        "event_date_utc",
        "activity",
        "operation",
        "item_name",
        "object_id",
        "object_type",
        "artifact_id",
        "artifact_kind",
        "workspace_id",
        "workspace_name",
        "dashboard_id",
        "dashboard_name",
        "report_id",
        "report_name",
        "dataset_id",
        "app_id",
        "app_name",
        "capacity_id",
        "capacity_name",
        "user_id",
        "user_key",
        "user_type",
        "client_ip",
        "user_agent",
        "consumption_method",
        "distribution_method",
        "raw_payload_json",
        "collected_at",
    }
    existing_audit_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(pbi_audit_events_raw)")
    }
    if "raw_payload_json" not in existing_audit_cols and "raw_json" in existing_audit_cols:
        try:
            conn.execute(
                "ALTER TABLE pbi_audit_events_raw "
                "RENAME COLUMN raw_json TO raw_payload_json"
            )
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE pbi_audit_events_raw ADD COLUMN raw_payload_json TEXT")
            conn.execute(
                "UPDATE pbi_audit_events_raw "
                "SET raw_payload_json = raw_json "
                "WHERE raw_payload_json IS NULL"
            )
        existing_audit_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(pbi_audit_events_raw)")
        }
    for col in sorted(audit_cols - existing_audit_cols):
        conn.execute(f"ALTER TABLE pbi_audit_events_raw ADD COLUMN {col} TEXT")
    conn.execute(
        """
        DELETE FROM pbi_audit_events_raw
        WHERE rowid NOT IN (
            SELECT MIN(rowid)
            FROM pbi_audit_events_raw
            WHERE api_event_id IS NOT NULL AND api_event_id <> ''
            GROUP BY api_event_id
        )
        AND api_event_id IS NOT NULL AND api_event_id <> ''
        """
    )
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pbi_audit_date
        ON pbi_audit_events_raw(event_date_utc)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pbi_audit_operation
        ON pbi_audit_events_raw(operation)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pbi_audit_activity
        ON pbi_audit_events_raw(activity)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pbi_audit_workspace_report
        ON pbi_audit_events_raw(workspace_id, report_id, event_date_utc)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pbi_audit_user_date
        ON pbi_audit_events_raw(user_id, event_date_utc)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pbi_audit_creation_time
        ON pbi_audit_events_raw(creation_time_utc)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pbi_audit_service_date
        ON pbi_audit_events_raw(event_date_service)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pbi_audit_api_event_id
        ON pbi_audit_events_raw(api_event_id)
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_pbi_audit_api_event_id
        ON pbi_audit_events_raw(api_event_id)
        WHERE api_event_id IS NOT NULL AND api_event_id <> ''
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS data_volume_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            file_size_bytes INTEGER NOT NULL,
            previous_size_bytes INTEGER,
            delta_bytes INTEGER,
            delta_ratio REAL,
            signal TEXT NOT NULL,
            diagnosis TEXT,
            collected_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_data_volume_signal_file_time
        ON data_volume_signals(file_path, collected_at DESC)
    """)
    conn.commit()
    return conn


def insert_records(conn, records):
    """Chèn records vào DB, tự bỏ qua bản ghi trùng request_id."""
    inserted = 0
    for r in records:
        try:
            before_changes = conn.total_changes
            conn.execute("""
                INSERT INTO refresh_history 
                (request_id, dataset_id, dataset_name, workspace_id, workspace_name,
                 status, refresh_type, start_time, end_time, duration_seconds, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    status = excluded.status,
                    end_time = excluded.end_time,
                    duration_seconds = excluded.duration_seconds,
                    error = excluded.error,
                    collected_at = datetime('now', 'localtime')
            """, (
                r["request_id"], r["dataset_id"], r["dataset_name"],
                r["workspace_id"], r["workspace_name"],
                r["status"], r["refresh_type"],
                r["start_time"], r["end_time"],
                r["duration_seconds"], r["error"]
            ))
            if conn.total_changes > before_changes:
                inserted += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    return inserted


def insert_activity_event_records(conn, records: List[Dict[str, Any]]) -> int:
    inserted = 0
    for r in records:
        try:
            before_changes = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO pbi_report_view_events_raw (
                    event_key, api_event_id, activity_id, creation_time_service, event_date_service,
                    creation_time_utc, event_date_utc, activity, operation,
                    user_id, user_key, user_type, workspace_id, workspace_name,
                    report_id, report_name, dataset_id, artifact_id, client_ip,
                    user_agent, consumption_method, distribution_method, raw_payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r.get("event_key"),
                    r.get("api_event_id"),
                    r.get("activity_id"),
                    r.get("creation_time_service"),
                    r.get("event_date_service"),
                    r.get("creation_time_utc"),
                    r.get("event_date_utc"),
                    r.get("activity"),
                    r.get("operation"),
                    r.get("user_id"),
                    r.get("user_key"),
                    r.get("user_type"),
                    r.get("workspace_id"),
                    r.get("workspace_name"),
                    r.get("report_id"),
                    r.get("report_name"),
                    r.get("dataset_id"),
                    r.get("artifact_id"),
                    r.get("client_ip"),
                    r.get("user_agent"),
                    r.get("consumption_method"),
                    r.get("distribution_method"),
                    r.get("raw_payload_json"),
                ),
            )
            if conn.total_changes > before_changes:
                inserted += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    return inserted


def insert_audit_event_records(conn, records: List[Dict[str, Any]]) -> int:
    inserted = 0
    for r in records:
        try:
            before_changes = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO pbi_audit_events_raw (
                    event_key, api_event_id, activity_id, creation_time_service, event_date_service,
                    creation_time_utc, event_date_utc, activity, operation, item_name, object_id,
                    object_type, artifact_id, artifact_kind, workspace_id, workspace_name,
                    dashboard_id, dashboard_name, report_id, report_name, dataset_id,
                    app_id, app_name, capacity_id, capacity_name, user_id, user_key, user_type,
                    client_ip, user_agent, consumption_method, distribution_method, raw_payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r.get("event_key"),
                    r.get("api_event_id"),
                    r.get("activity_id"),
                    r.get("creation_time_service"),
                    r.get("event_date_service"),
                    r.get("creation_time_utc"),
                    r.get("event_date_utc"),
                    r.get("activity"),
                    r.get("operation"),
                    r.get("item_name"),
                    r.get("object_id"),
                    r.get("object_type"),
                    r.get("artifact_id"),
                    r.get("artifact_kind"),
                    r.get("workspace_id"),
                    r.get("workspace_name"),
                    r.get("dashboard_id"),
                    r.get("dashboard_name"),
                    r.get("report_id"),
                    r.get("report_name"),
                    r.get("dataset_id"),
                    r.get("app_id"),
                    r.get("app_name"),
                    r.get("capacity_id"),
                    r.get("capacity_name"),
                    r.get("user_id"),
                    r.get("user_key"),
                    r.get("user_type"),
                    r.get("client_ip"),
                    r.get("user_agent"),
                    r.get("consumption_method"),
                    r.get("distribution_method"),
                    r.get("raw_payload_json"),
                ),
            )
            if conn.total_changes > before_changes:
                inserted += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    return inserted


def resolve_max_workers(config, total_ds: int) -> int:
    """Resolve worker count with sane defaults to reduce API throttling."""
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}

    configured = collector_cfg.get("max_workers")
    if isinstance(configured, int) and configured > 0:
        return min(max(1, configured), max(1, total_ds))

    if total_ds <= 1:
        return 1

    dynamic_workers = min(12, max(2, (total_ds // 20) + 1))
    return min(dynamic_workers, total_ds)


def get_rate_limit_per_minute(config) -> Optional[int]:
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}
    value = collector_cfg.get("rate_limit_per_minute")
    if isinstance(value, int) and value >= 0:
        return value
    return None


def get_data_volume_monitor_enabled(config) -> bool:
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}
    value = collector_cfg.get("data_volume_monitor_enabled")
    if isinstance(value, bool):
        return value
    return True


def get_data_volume_monitor_files(config) -> List[str]:
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}
    configured = collector_cfg.get("data_volume_monitor_files")
    files = []
    if isinstance(configured, list):
        for item in configured:
            if not isinstance(item, str):
                continue
            path = item.strip()
            if not path:
                continue
            if not os.path.isabs(path):
                path = os.path.abspath(path)
            files.append(path)
    if files:
        return files
    return [os.path.abspath(PARQUET_FILE), os.path.abspath(DB_FILE)]


def get_data_volume_drop_ratio_threshold(config) -> float:
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}
    value = collector_cfg.get("data_volume_drop_ratio_threshold")
    if isinstance(value, (int, float)) and 0 < float(value) < 1:
        return float(value)
    return 0.2


def get_data_volume_spike_ratio_threshold(config) -> float:
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}
    value = collector_cfg.get("data_volume_spike_ratio_threshold")
    if isinstance(value, (int, float)) and 0 < float(value) < 10:
        return float(value)
    return 0.3


def get_data_volume_min_change_mb(config) -> float:
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}
    value = collector_cfg.get("data_volume_min_change_mb")
    if isinstance(value, (int, float)) and float(value) >= 0:
        return float(value)
    return 10.0


def normalize_activity_filter(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.lower() in {"all", "*", "none", "null"}:
        return None
    return normalized


def get_activity_events_enabled(config) -> bool:
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}
    value = collector_cfg.get("activity_events_enabled")
    if isinstance(value, bool):
        return value
    return True


def get_activity_events_lookback_days(config) -> int:
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}
    value = collector_cfg.get("activity_events_lookback_days")
    if isinstance(value, int) and value > 0:
        return value
    return 2


def get_activity_events_activity_filter(config) -> Optional[str]:
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}
    value = collector_cfg.get("activity_events_activity_filter")
    normalized = normalize_activity_filter(value)
    if normalized:
        return normalized
    return "ViewReport"


def get_activity_events_max_backfill_days(config) -> int:
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}
    value = collector_cfg.get("activity_events_max_backfill_days")
    if isinstance(value, int) and value > 0:
        return value
    return 28


def get_activity_events_initial_backfill_days(config) -> int:
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}
    value = collector_cfg.get("activity_events_initial_backfill_days")
    if isinstance(value, int) and value > 0:
        return value
    return get_activity_events_max_backfill_days(config)


def get_audit_events_enabled(config) -> bool:
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}
    value = collector_cfg.get("audit_events_enabled")
    if isinstance(value, bool):
        return value
    return True


def get_audit_events_lookback_days(config) -> int:
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}
    value = collector_cfg.get("audit_events_lookback_days")
    if isinstance(value, int) and value > 0:
        return value
    return 2


def get_audit_events_activity_filter(config) -> Optional[str]:
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}
    return normalize_activity_filter(collector_cfg.get("audit_events_activity_filter"))


def get_audit_events_max_backfill_days(config) -> int:
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}
    value = collector_cfg.get("audit_events_max_backfill_days")
    if isinstance(value, int) and value > 0:
        return value
    return 28


def get_audit_events_initial_backfill_days(config) -> int:
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}
    value = collector_cfg.get("audit_events_initial_backfill_days")
    if isinstance(value, int) and value > 0:
        return value
    return get_audit_events_max_backfill_days(config)


def _format_bytes_human(size_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size_bytes)
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    return f"{value:.2f} {units[idx]}"


def classify_data_volume_signal(
    current_size_bytes: int,
    previous_size_bytes: Optional[int],
    drop_ratio_threshold: float,
    spike_ratio_threshold: float,
    min_change_bytes: int,
) -> Dict[str, Any]:
    if previous_size_bytes is None or previous_size_bytes <= 0:
        return {
            "signal": "insufficient_history",
            "diagnosis": "Chua du lich su de danh gia xu huong dung luong.",
            "delta_bytes": None,
            "delta_ratio": None,
        }

    delta_bytes = int(current_size_bytes) - int(previous_size_bytes)
    delta_ratio = delta_bytes / float(previous_size_bytes)
    abs_delta = abs(delta_bytes)

    if (
        delta_bytes < 0
        and abs_delta >= min_change_bytes
        and abs(delta_ratio) >= drop_ratio_threshold
    ):
        signal = "decrease_anomaly"
        diagnosis = "Giam bat thuong: co kha nang thieu data."
    elif (
        delta_bytes > 0
        and abs_delta >= min_change_bytes
        and delta_ratio >= spike_ratio_threshold
    ):
        signal = "increase_anomaly"
        diagnosis = "Tang bat thuong: can kiem tra kha nang lap data."
    else:
        signal = "normal_increase_or_stable"
        diagnosis = "Tang deu hoac dao dong nhe: xu huong binh thuong."

    return {
        "signal": signal,
        "diagnosis": diagnosis,
        "delta_bytes": delta_bytes,
        "delta_ratio": round(delta_ratio, 6),
    }


def monitor_data_volume_signals(
    config,
    target_files: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    if not get_data_volume_monitor_enabled(config):
        return []

    files = target_files or get_data_volume_monitor_files(config)
    if not files:
        return []

    drop_ratio_threshold = get_data_volume_drop_ratio_threshold(config)
    spike_ratio_threshold = get_data_volume_spike_ratio_threshold(config)
    min_change_bytes = int(get_data_volume_min_change_mb(config) * 1024 * 1024)

    conn = init_db()
    inserted_rows = []
    try:
        for file_path in files:
            abs_path = os.path.abspath(file_path)
            if not os.path.isfile(abs_path):
                print(f"  [size-monitor] Skip missing file: {abs_path}")
                continue

            current_size = os.path.getsize(abs_path)
            previous_row = conn.execute(
                """
                SELECT file_size_bytes
                FROM data_volume_signals
                WHERE file_path = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (abs_path,),
            ).fetchone()
            previous_size = previous_row[0] if previous_row else None

            signal_info = classify_data_volume_signal(
                current_size_bytes=current_size,
                previous_size_bytes=previous_size,
                drop_ratio_threshold=drop_ratio_threshold,
                spike_ratio_threshold=spike_ratio_threshold,
                min_change_bytes=min_change_bytes,
            )
            conn.execute(
                """
                INSERT INTO data_volume_signals (
                    file_path,
                    file_size_bytes,
                    previous_size_bytes,
                    delta_bytes,
                    delta_ratio,
                    signal,
                    diagnosis
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    abs_path,
                    int(current_size),
                    int(previous_size) if previous_size is not None else None,
                    signal_info["delta_bytes"],
                    signal_info["delta_ratio"],
                    signal_info["signal"],
                    signal_info["diagnosis"],
                ),
            )
            inserted_rows.append(
                {
                    "file_path": abs_path,
                    "file_size_bytes": int(current_size),
                    "previous_size_bytes": int(previous_size)
                    if previous_size is not None
                    else None,
                    "delta_bytes": signal_info["delta_bytes"],
                    "signal": signal_info["signal"],
                    "diagnosis": signal_info["diagnosis"],
                }
            )
    finally:
        conn.commit()
        conn.close()

    for row in inserted_rows:
        delta_bytes = row.get("delta_bytes")
        delta_label = (
            "N/A"
            if delta_bytes is None
            else f"{'+' if delta_bytes >= 0 else '-'}{_format_bytes_human(abs(delta_bytes))}"
        )
        print(
            "  [size-monitor] "
            f"{row['signal']} | {os.path.basename(row['file_path'])} | "
            f"size={_format_bytes_human(row['file_size_bytes'])} | delta={delta_label}"
        )

    return inserted_rows




def resolve_activity_event_lookback_days_by_latest(
    latest_event_date_utc: Optional[str],
    safety_lookback_days: int,
    max_backfill_days: int,
    initial_backfill_days: int,
    now_utc: Optional[datetime] = None,
) -> int:
    """Resolve how many UTC days should be pulled this run."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    max_backfill_days = max(1, max_backfill_days)
    safety_lookback_days = max(1, safety_lookback_days)
    initial_backfill_days = max(1, initial_backfill_days)

    if not latest_event_date_utc:
        return min(initial_backfill_days, max_backfill_days)

    try:
        latest_day = datetime.strptime(latest_event_date_utc, "%Y-%m-%d").date()
    except ValueError:
        return min(initial_backfill_days, max_backfill_days)

    today = now_utc.date()
    gap_days = (today - latest_day).days + 1
    if gap_days < 1:
        gap_days = 1

    needed_days = max(safety_lookback_days, gap_days)
    return min(needed_days, max_backfill_days)


def _as_utc_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # Power BI ActivityEvents often returns naive UTC timestamps.
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def compute_activity_event_day_windows(lookback_days: int, now_utc: Optional[datetime] = None) -> List[Dict[str, str]]:
    """Return day windows in UTC from today backward."""
    if lookback_days <= 0:
        return []
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    windows: List[Dict[str, str]] = []
    day_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(lookback_days):
        start_dt = day_start - timedelta(days=i)
        end_dt = start_dt + timedelta(hours=23, minutes=59, seconds=59, milliseconds=999)
        windows.append({
            "date_utc": start_dt.strftime("%Y-%m-%d"),
            "start_utc": start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "end_utc": end_dt.strftime("%Y-%m-%dT%H:%M:%S.999Z"),
        })
    return windows


def build_activity_event_record(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize one raw Power BI activity event payload into DB row shape."""
    creation_raw = payload.get("CreationTime") or payload.get("creationTime")
    created_at = _as_utc_datetime(creation_raw)
    if created_at is None:
        return None
    creation_service = str(creation_raw) if creation_raw is not None else None
    event_date_service = creation_service[:10] if creation_service else None

    row = {
        "api_event_id": payload.get("Id") or payload.get("id"),
        "activity_id": payload.get("ActivityId") or payload.get("activityId"),
        "creation_time_service": creation_service,
        "event_date_service": event_date_service,
        "creation_time_utc": created_at.isoformat(),
        "event_date_utc": created_at.strftime("%Y-%m-%d"),
        "activity": payload.get("Activity") or payload.get("activity"),
        "operation": payload.get("Operation") or payload.get("operation"),
        "user_id": payload.get("UserId") or payload.get("userId"),
        "user_key": payload.get("UserKey") or payload.get("userKey"),
        "user_type": payload.get("UserType") or payload.get("userType"),
        "workspace_id": payload.get("WorkspaceId") or payload.get("workspaceId"),
        "workspace_name": (
            payload.get("WorkspaceName")
            or payload.get("WorkSpaceName")
            or payload.get("workspaceName")
        ),
        "report_id": payload.get("ReportId") or payload.get("reportId"),
        "report_name": payload.get("ReportName") or payload.get("reportName"),
        "dataset_id": payload.get("DatasetId") or payload.get("datasetId"),
        "artifact_id": payload.get("ArtifactId") or payload.get("artifactId"),
        "client_ip": payload.get("ClientIP") or payload.get("clientIP"),
        "user_agent": payload.get("UserAgent") or payload.get("userAgent"),
        "consumption_method": payload.get("ConsumptionMethod") or payload.get("consumptionMethod"),
        "distribution_method": payload.get("DistributionMethod") or payload.get("distributionMethod"),
        "raw_payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
    }
    row["event_key"] = build_activity_event_key(row)
    return row


def build_activity_event_key(row: Dict[str, Any]) -> str:
    api_event_id = row.get("api_event_id")
    if api_event_id:
        return str(api_event_id)

    key_parts = [
        row.get("creation_time_utc", ""),
        row.get("activity", ""),
        row.get("operation", ""),
        row.get("user_id", ""),
        row.get("workspace_id", ""),
        row.get("report_id", ""),
        row.get("artifact_id", ""),
    ]
    source = "|".join(key_parts)
    return hashlib.sha1(source.encode("utf-8")).hexdigest()


def build_audit_event_record(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    creation_raw = payload.get("CreationTime") or payload.get("creationTime")
    created_at = _as_utc_datetime(creation_raw)
    if created_at is None:
        return None

    creation_service = str(creation_raw) if creation_raw is not None else None
    event_date_service = creation_service[:10] if creation_service else None

    row = {
        "api_event_id": payload.get("Id") or payload.get("id"),
        "activity_id": payload.get("ActivityId") or payload.get("activityId"),
        "creation_time_service": creation_service,
        "event_date_service": event_date_service,
        "creation_time_utc": created_at.isoformat(),
        "event_date_utc": created_at.strftime("%Y-%m-%d"),
        "activity": payload.get("Activity") or payload.get("activity"),
        "operation": payload.get("Operation") or payload.get("operation"),
        "item_name": payload.get("ItemName") or payload.get("itemName"),
        "object_id": payload.get("ObjectId") or payload.get("objectId"),
        "object_type": payload.get("ObjectType") or payload.get("objectType"),
        "artifact_id": payload.get("ArtifactId") or payload.get("artifactId"),
        "artifact_kind": payload.get("ArtifactKind") or payload.get("artifactKind"),
        "workspace_id": payload.get("WorkspaceId") or payload.get("workspaceId"),
        "workspace_name": (
            payload.get("WorkspaceName")
            or payload.get("WorkSpaceName")
            or payload.get("workspaceName")
        ),
        "dashboard_id": payload.get("DashboardId") or payload.get("dashboardId"),
        "dashboard_name": payload.get("DashboardName") or payload.get("dashboardName"),
        "report_id": payload.get("ReportId") or payload.get("reportId"),
        "report_name": payload.get("ReportName") or payload.get("reportName"),
        "dataset_id": payload.get("DatasetId") or payload.get("datasetId"),
        "app_id": payload.get("AppId") or payload.get("appId"),
        "app_name": payload.get("AppName") or payload.get("appName"),
        "capacity_id": payload.get("CapacityId") or payload.get("capacityId"),
        "capacity_name": payload.get("CapacityName") or payload.get("capacityName"),
        "user_id": payload.get("UserId") or payload.get("userId"),
        "user_key": payload.get("UserKey") or payload.get("userKey"),
        "user_type": payload.get("UserType") or payload.get("userType"),
        "client_ip": payload.get("ClientIP") or payload.get("clientIP"),
        "user_agent": payload.get("UserAgent") or payload.get("userAgent"),
        "consumption_method": payload.get("ConsumptionMethod") or payload.get("consumptionMethod"),
        "distribution_method": payload.get("DistributionMethod") or payload.get("distributionMethod"),
        "raw_payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
    }
    row["event_key"] = build_audit_event_key(row)
    return row


def build_audit_event_key(row: Dict[str, Any]) -> str:
    api_event_id = row.get("api_event_id")
    if api_event_id:
        return str(api_event_id)

    key_parts = [
        row.get("creation_time_utc", ""),
        row.get("activity", ""),
        row.get("operation", ""),
        row.get("user_id", ""),
        row.get("workspace_id", ""),
        row.get("dashboard_id", ""),
        row.get("report_id", ""),
        row.get("dataset_id", ""),
        row.get("object_id", ""),
        row.get("artifact_id", ""),
    ]
    source = "|".join(key_parts)
    return hashlib.sha1(source.encode("utf-8")).hexdigest()


def classify_activity_metrics(activity: Optional[str]) -> Tuple[int, int, int]:
    """
    Map Power BI activity to common metrics used for dashboard usage analysis.
    Returns: (report_open_count, export_report_count, interaction_count)
    """
    normalized = (activity or "").strip()
    report_open_count = 1 if normalized == "ViewReport" else 0
    export_report_count = 1 if normalized == "ExportReport" else 0
    interaction_count = 1 if normalized in {"ViewReport", "ExportReport"} else 0
    return report_open_count, export_report_count, interaction_count


def get_backup_enabled(config) -> bool:
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}
    value = collector_cfg.get("backup_enabled")
    if isinstance(value, bool):
        return value
    return True


def get_backup_keep_days(config) -> int:
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}
    value = collector_cfg.get("backup_keep_days")
    if isinstance(value, int) and value > 0:
        return value
    return 30


def get_backup_dir(config) -> str:
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}
    value = collector_cfg.get("backup_dir")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_BACKUP_DIR


def is_process_running(pid: Optional[int]) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_lock_pid(lock_path: str) -> Optional[int]:
    try:
        with open(lock_path, "r", encoding="utf-8") as fh:
            first_line = fh.readline().strip()
    except OSError:
        return None
    try:
        return int(first_line)
    except (TypeError, ValueError):
        return None


def cleanup_stale_lock(lock_path: str) -> bool:
    if not os.path.exists(lock_path):
        return False
    pid = read_lock_pid(lock_path)
    if is_process_running(pid):
        return False
    try:
        os.remove(lock_path)
        return True
    except OSError:
        return False


def is_collector_lock_active(lock_path: str) -> bool:
    if not os.path.exists(lock_path):
        return False
    cleanup_stale_lock(lock_path)
    if not os.path.exists(lock_path):
        return False
    return is_process_running(read_lock_pid(lock_path))


def acquire_collector_lock(lock_path: str) -> Optional[int]:
    lock_dir = os.path.dirname(lock_path)
    if lock_dir:
        os.makedirs(lock_dir, exist_ok=True)

    for _ in range(2):
        try:
            lock_handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            if cleanup_stale_lock(lock_path):
                continue
            return None

        payload = f"{os.getpid()}\n{datetime.now().isoformat()}\n".encode("utf-8")
        os.write(lock_handle, payload)
        return lock_handle

    return None


def release_collector_lock(lock_handle: Optional[int], lock_path: str):
    if lock_handle is None:
        return
    try:
        os.close(lock_handle)
    except OSError:
        pass


def run_with_collector_lock(lock_path: str, busy_message: str, func):
    lock_handle = acquire_collector_lock(lock_path)
    if lock_handle is None:
        print(busy_message)
        return None
    try:
        return func()
    finally:
        release_collector_lock(lock_handle, lock_path)
    try:
        os.remove(lock_path)
    except OSError:
        pass


def cleanup_old_backups(backup_dir: str, active_backup_path: Optional[str] = None):
    removed = 0
    if not os.path.isdir(backup_dir):
        return removed
    active_backup_path = (
        os.path.abspath(active_backup_path) if active_backup_path else None
    )
    for name in os.listdir(backup_dir):
        if not (name.startswith("refresh_history_") and name.endswith(".db")):
            continue
        path = os.path.join(backup_dir, name)
        try:
            if active_backup_path and os.path.abspath(path) == active_backup_path:
                continue
            if os.path.isfile(path):
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed


def backup_database_legacy(config) -> Optional[str]:
    """Backup SQLite DB to a single rotating file and prune redundant backups."""
    if not os.path.exists(DB_FILE):
        print("  ⚠️ Chưa có DB để backup.")
        return None

    if not get_backup_enabled(config):
        return None

    backup_dir = get_backup_dir(config)
    os.makedirs(backup_dir, exist_ok=True)

    backup_path = os.path.join(backup_dir, DEFAULT_BACKUP_FILE)

    src_conn = sqlite3.connect(DB_FILE)
    dst_conn = sqlite3.connect(backup_path)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    keep_days = get_backup_keep_days(config)
    removed = cleanup_old_backups(backup_dir, active_backup_path=backup_path)

    print(f"  🧷 DB backup: {backup_path}")
    if removed > 0:
        print(f"  🧹 Đã dọn {removed} file backup cũ (>{keep_days} ngày)")
    return backup_path


def backup_database(config) -> Optional[str]:
    """Backup SQLite DB to a single rotating file and prune redundant backups."""
    if not os.path.exists(DB_FILE):
        print("  No SQLite DB found to back up.")
        return None

    if not get_backup_enabled(config):
        return None

    backup_dir = get_backup_dir(config)
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(backup_dir, DEFAULT_BACKUP_FILE)

    src_conn = sqlite3.connect(DB_FILE)
    dst_conn = sqlite3.connect(backup_path)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    removed = cleanup_old_backups(backup_dir, active_backup_path=backup_path)
    print(f"  DB backup: {backup_path}")
    if removed > 0:
        print(f"  Removed {removed} redundant backup file(s)")
    return backup_path


def dataset_key(workspace_id: str, dataset_id: str) -> str:
    return f"{workspace_id}:{dataset_id}"


def get_ignored_dataset_keys(config) -> set:
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}
    return set(collector_cfg.get("ignored_dataset_keys", []) or [])


def split_dataset_key(key: str) -> Optional[Tuple[str, str]]:
    if ":" not in key:
        return None
    ws_id, ds_id = key.split(":", 1)
    ws_id = ws_id.strip()
    ds_id = ds_id.strip()
    if not ws_id or not ds_id:
        return None
    return ws_id, ds_id


def save_config(config, config_file: str = CONFIG_FILE):
    with open(config_file, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def persist_ignored_dataset_keys(config, new_keys, config_file: str = CONFIG_FILE) -> int:
    """Persist newly ignored dataset keys to config file and in-memory config."""
    if not new_keys:
        return 0

    collector_cfg = config.setdefault("collector", {})
    existing = set(collector_cfg.get("ignored_dataset_keys", []) or [])
    merged = existing | set(new_keys)
    if merged == existing:
        return 0

    collector_cfg["ignored_dataset_keys"] = sorted(merged)
    save_config(config, config_file=config_file)

    return len(merged) - len(existing)


def remove_ignored_dataset_keys(config, keys_to_remove, config_file: str = CONFIG_FILE) -> int:
    if not keys_to_remove:
        return 0

    collector_cfg = config.setdefault("collector", {})
    existing = set(collector_cfg.get("ignored_dataset_keys", []) or [])
    updated = existing - set(keys_to_remove)
    removed = len(existing) - len(updated)
    if removed <= 0:
        return 0

    collector_cfg["ignored_dataset_keys"] = sorted(updated)
    save_config(config, config_file=config_file)
    return removed


def normalize_dataset_keys(keys) -> Tuple[set, list]:
    normalized = set()
    invalid = []
    for key in keys or []:
        parsed = split_dataset_key(key)
        if parsed is None:
            invalid.append(key)
            continue
        ws_id, ds_id = parsed
        normalized.add(dataset_key(ws_id, ds_id))
    return normalized, invalid


def list_ignored_dataset_keys(config):
    ignored = sorted(get_ignored_dataset_keys(config))
    print(f"\nIgnored datasets: {len(ignored)}")
    for key in ignored:
        print(f"  - {key}")


def validate_ignored_dataset_keys(client: PowerBIClient, config) -> int:
    ignored = sorted(get_ignored_dataset_keys(config))
    if not ignored:
        print("\nKhông có dataset nào trong ignore list.")
        return 0

    print(f"\nĐang validate {len(ignored)} dataset trong ignore list...")
    keys_to_remove = set()

    for key in ignored:
        parsed = split_dataset_key(key)
        if parsed is None:
            print(f"  ⚠️ Key không hợp lệ (giữ nguyên): {key}")
            continue
        ws_id, ds_id = parsed
        try:
            client.get_refresh_history(ws_id, ds_id, top=1, suppress_errors=False)
            keys_to_remove.add(key)
            print(f"  ✅ Đã hoạt động lại: {key}")
        except Exception as e:
            msg = str(e)
            if "404" in msg and "ItemNotFound" in msg:
                print(f"  ⏭️ Vẫn 404, giữ ignore: {key}")
            else:
                print(f"  ⚠️ Lỗi tạm thời, giữ ignore: {key} | {msg[:120]}")

    removed = remove_ignored_dataset_keys(config, keys_to_remove)
    if removed > 0:
        print(f"\nĐã gỡ {removed} dataset khỏi ignore list.")
    else:
        print("\nKhông có dataset nào được gỡ khỏi ignore list.")
    return removed


def collect_activity_events_once(
    client: PowerBIClient,
    config,
    override_lookback_days: Optional[int] = None
) -> int:
    """Collect raw report view events from Power BI Activity Events API into SQLite."""
    if not get_activity_events_enabled(config):
        print("  ⏭️ Activity events collector đang tắt trong config.")
        return 0

    conn = init_db()
    latest_event_date_row = conn.execute(
        "SELECT MAX(event_date_utc) FROM pbi_report_view_events_raw"
    ).fetchone()
    latest_event_date_utc = latest_event_date_row[0] if latest_event_date_row else None

    safety_lookback_days = get_activity_events_lookback_days(config)
    max_backfill_days = get_activity_events_max_backfill_days(config)
    initial_backfill_days = get_activity_events_initial_backfill_days(config)

    if isinstance(override_lookback_days, int) and override_lookback_days > 0:
        lookback_days = override_lookback_days
    else:
        lookback_days = resolve_activity_event_lookback_days_by_latest(
            latest_event_date_utc=latest_event_date_utc,
            safety_lookback_days=safety_lookback_days,
            max_backfill_days=max_backfill_days,
            initial_backfill_days=initial_backfill_days,
        )
    activity_filter = get_activity_events_activity_filter(config)
    windows = compute_activity_event_day_windows(lookback_days=lookback_days)

    total_api_rows = 0
    normalized_rows: List[Dict[str, Any]] = []

    print(
        f"  👁️ Đang kéo activity events (filter={activity_filter}, lookback_days={lookback_days})..."
    )

    for win in windows:
        try:
            events = client.get_activity_events(
                start_datetime_utc=win["start_utc"],
                end_datetime_utc=win["end_utc"],
                activity_filter=activity_filter,
            )
            total_api_rows += len(events)
            for event in events:
                row = build_activity_event_record(event)
                if row is not None:
                    normalized_rows.append(row)
            print(f"    - {win['date_utc']}: {len(events)} events")
        except Exception as e:
            print(f"    ⚠️ Lỗi lấy activity events ngày {win['date_utc']}: {e}")

    before = conn.execute("SELECT COUNT(*) FROM pbi_report_view_events_raw").fetchone()[0]
    inserted = insert_activity_event_records(conn, normalized_rows)
    after = conn.execute("SELECT COUNT(*) FROM pbi_report_view_events_raw").fetchone()[0]
    conn.close()

    print(f"  📥 Activity API trả về: {total_api_rows} events")
    print(f"  ✅ Activity mới thêm: {inserted} | Trùng bỏ qua: {len(normalized_rows) - inserted}")
    print(f"  💾 Tổng events trong DB: {after}")

    return inserted


def collect_audit_events_once(
    client: PowerBIClient,
    config,
    override_lookback_days: Optional[int] = None,
    override_activity_filter: Optional[str] = None,
) -> int:
    """Collect full raw audit events from Power BI Activity Events API into SQLite."""
    if not get_audit_events_enabled(config):
        print("  ⏭️ Audit events collector đang tắt trong config.")
        return 0

    conn = init_db()
    latest_event_date_row = conn.execute(
        "SELECT MAX(event_date_utc) FROM pbi_audit_events_raw"
    ).fetchone()
    latest_event_date_utc = latest_event_date_row[0] if latest_event_date_row else None

    safety_lookback_days = get_audit_events_lookback_days(config)
    max_backfill_days = get_audit_events_max_backfill_days(config)
    initial_backfill_days = get_audit_events_initial_backfill_days(config)

    if isinstance(override_lookback_days, int) and override_lookback_days > 0:
        lookback_days = override_lookback_days
    else:
        lookback_days = resolve_activity_event_lookback_days_by_latest(
            latest_event_date_utc=latest_event_date_utc,
            safety_lookback_days=safety_lookback_days,
            max_backfill_days=max_backfill_days,
            initial_backfill_days=initial_backfill_days,
        )

    activity_filter = normalize_activity_filter(override_activity_filter)
    if activity_filter is None:
        activity_filter = get_audit_events_activity_filter(config)

    windows = compute_activity_event_day_windows(lookback_days=lookback_days)
    total_api_rows = 0
    normalized_rows: List[Dict[str, Any]] = []
    filter_label = activity_filter if activity_filter else "ALL"

    print(
        f"  📜 Đang kéo full audit events (filter={filter_label}, lookback_days={lookback_days})..."
    )

    for win in windows:
        try:
            events = client.get_activity_events(
                start_datetime_utc=win["start_utc"],
                end_datetime_utc=win["end_utc"],
                activity_filter=activity_filter,
            )
            total_api_rows += len(events)
            for event in events:
                row = build_audit_event_record(event)
                if row is not None:
                    normalized_rows.append(row)
            print(f"    - {win['date_utc']}: {len(events)} events")
        except Exception as e:
            print(f"    ⚠️ Lỗi lấy audit events ngày {win['date_utc']}: {e}")

    before = conn.execute("SELECT COUNT(*) FROM pbi_audit_events_raw").fetchone()[0]
    inserted = insert_audit_event_records(conn, normalized_rows)
    after = conn.execute("SELECT COUNT(*) FROM pbi_audit_events_raw").fetchone()[0]
    conn.close()

    print(f"  📥 Audit API trả về: {total_api_rows} events")
    print(f"  ✅ Audit mới thêm: {inserted} | Trùng bỏ qua: {len(normalized_rows) - inserted}")
    print(f"  💾 Tổng audit events trong DB: {after}")
    return inserted


def repair_activity_event_service_dates() -> int:
    """Backfill service-date columns from raw payload for historical rows."""
    conn = init_db()
    conn.execute("DROP INDEX IF EXISTS uq_pbi_view_api_event_id")
    cursor = conn.execute(
        """
        UPDATE pbi_report_view_events_raw
        SET
            api_event_id = COALESCE(api_event_id, json_extract(raw_payload_json, '$.Id')),
            activity_id = COALESCE(activity_id, json_extract(raw_payload_json, '$.ActivityId')),
            creation_time_service = json_extract(raw_payload_json, '$.CreationTime'),
            event_date_service = substr(json_extract(raw_payload_json, '$.CreationTime'), 1, 10)
        WHERE
            api_event_id IS NULL OR activity_id IS NULL
            OR creation_time_service IS NULL OR event_date_service IS NULL
        """
    )
    conn.execute(
        """
        DELETE FROM pbi_report_view_events_raw
        WHERE rowid NOT IN (
            SELECT MIN(rowid)
            FROM pbi_report_view_events_raw
            WHERE api_event_id IS NOT NULL AND api_event_id <> ''
            GROUP BY api_event_id
        )
        AND api_event_id IS NOT NULL AND api_event_id <> ''
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_pbi_view_api_event_id
        ON pbi_report_view_events_raw(api_event_id)
        WHERE api_event_id IS NOT NULL AND api_event_id <> ''
        """
    )
    conn.commit()
    changed = cursor.rowcount if cursor.rowcount is not None else 0
    conn.close()
    return max(0, changed)


# ─── Collector ─────────────────────────────────────
def collect_once(client, config):
    """Kéo dữ liệu từ Power BI API và lưu vào SQLite."""
    workspaces = config["powerbi"].get("workspaces", [])
    ignored_keys = get_ignored_dataset_keys(config)

    tasks = []
    skipped_ignored = 0
    for ws in workspaces:
        ws_id = ws["id"]
        ws_name = ws.get("name", ws_id)
        for ds in ws.get("datasets") or []:
            ds_id = ds["id"]
            ds_name = ds.get("name", ds_id)
            if dataset_key(ws_id, ds_id) in ignored_keys:
                skipped_ignored += 1
                continue
            tasks.append((ws_id, ws_name, ds_id, ds_name))

    total_ds = len(tasks)
    max_workers = resolve_max_workers(config, total_ds)
    print(f"  📡 Đang kéo dữ liệu từ {total_ds} datasets (top={MAX_TOP}, workers={max_workers})...")
    if skipped_ignored > 0:
        print(f"  ⏭️ Bỏ qua {skipped_ignored} dataset đã nằm trong ignore list")
    if total_ds == 0:
        print("  ⚠️ Không còn dataset hợp lệ để thu thập.")
        return 0

    all_records = []
    error_count = 0
    not_found_keys = set()
    counters_lock = Lock()

    def fetch_one(task_info):
        nonlocal error_count
        ws_id, ws_name, ds_id, ds_name = task_info
        rows = []
        try:
            history = client.get_refresh_history(
                ws_id, ds_id, top=MAX_TOP, suppress_errors=False
            )
            for h in history:
                start = h.get("start_time")
                end = h.get("end_time")
                duration = None
                if start and end:
                    duration = round((end - start).total_seconds(), 1)

                error_detail = ""
                if h.get("service_exception_json"):
                    try:
                        err_json = json.loads(h["service_exception_json"])
                        error_detail = err_json.get("errorDescription") or str(err_json)
                    except (json.JSONDecodeError, TypeError):
                        error_detail = str(h["service_exception_json"])

                req_id = h.get("request_id")
                if not req_id:
                    continue  # Bỏ qua bản ghi không có ID

                rows.append({
                    "request_id": req_id,
                    "dataset_id": ds_id,
                    "dataset_name": ds_name,
                    "workspace_id": ws_id,
                    "workspace_name": ws_name,
                    "status": h.get("status", "Unknown"),
                    "refresh_type": h.get("refresh_type", ""),
                    "start_time": start.strftime("%Y-%m-%d %H:%M:%S") if start else None,
                    "end_time": end.strftime("%Y-%m-%d %H:%M:%S") if end else None,
                    "duration_seconds": duration,
                    "error": error_detail
                })
        except Exception as e:
            message = str(e)
            if "404" in message and "ItemNotFound" in message:
                key = dataset_key(ws_id, ds_id)
                with counters_lock:
                    not_found_keys.add(key)
                print(f"  ⚠️ Dataset không tồn tại, sẽ tự ignore: {ds_name} ({key})")
            else:
                with counters_lock:
                    error_count += 1
        return rows

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for batch in executor.map(fetch_one, tasks):
            all_records.extend(batch)

    # Lưu vào SQLite
    conn = init_db()
    
    # Đếm trước khi insert
    before = conn.execute("SELECT COUNT(*) FROM refresh_history").fetchone()[0]
    insert_records(conn, all_records)
    after = conn.execute("SELECT COUNT(*) FROM refresh_history").fetchone()[0]
    new_records = after - before

    conn.close()

    print(f"  📥 API trả về: {len(all_records)} bản ghi")
    print(f"  ✅ Mới thêm: {new_records} | Trùng lặp bỏ qua: {len(all_records) - new_records}")
    print(f"  💾 Tổng trong DB: {after} bản ghi")
    newly_ignored = persist_ignored_dataset_keys(config, not_found_keys)
    if newly_ignored > 0:
        print(f"  🛡️ Đã thêm {newly_ignored} dataset vào ignore list trong {CONFIG_FILE}")
    if error_count > 0:
        print(f"  ⚠️  {error_count} dataset gặp lỗi API")

    try:
        collect_activity_events_once(client, config)
    except Exception as e:
        print(f"  ⚠️ Bỏ qua activity events do lỗi: {e}")

    try:
        collect_audit_events_once(client, config)
    except Exception as e:
        print(f"  ⚠️ Bỏ qua audit events do lỗi: {e}")
    
    # Tự động xuất Parquet lên OneDrive sau khi thu thập
    export_to_parquet()
    export_view_events_to_parquet()
    backup_database(config)
    try:
        monitor_data_volume_signals(config)
    except Exception as e:
        print(f"  [size-monitor] Skip due to error: {e}")
    
    return new_records


# ─── Export Parquet ────────────────────────────────
def export_to_parquet():
    """Xuất toàn bộ dữ liệu từ SQLite ra file Parquet trên OneDrive."""
    if not os.path.exists(DB_FILE):
        return

    try:
        if not os.path.exists(EXPORT_DIR):
            os.makedirs(EXPORT_DIR, exist_ok=True)

        conn = sqlite3.connect(DB_FILE)
        # Truy vấn toàn bộ dữ liệu, sắp xếp theo thời gian mới nhất
        df = pd.read_sql_query("SELECT * FROM refresh_history ORDER BY start_time DESC", conn)
        conn.close()

        if df.empty:
            print("  ⚠️  Database trống, không có dữ liệu để xuất Parquet.")
            return

        # Xuất ra file Parquet (ghi đè file cũ để giữ link Power BI)
        df.to_parquet(PARQUET_FILE, engine='pyarrow', index=False)
        
        print(f"  🚀 Đã đồng bộ Parquet:")
        print(f"     {PARQUET_FILE}")
        
    except Exception as e:
        print(f"  ❌ Lỗi khi xuất Parquet: {e}")


def export_view_events_to_parquet():
    """Xuất raw page view events từ SQLite ra file Parquet trên OneDrive."""
    if not os.path.exists(DB_FILE):
        return

    try:
        if not os.path.exists(EXPORT_DIR):
            os.makedirs(EXPORT_DIR, exist_ok=True)

        conn = sqlite3.connect(DB_FILE)
        df = pd.read_sql_query(
            "SELECT * FROM pbi_report_view_events_raw ORDER BY creation_time_utc DESC",
            conn
        )
        conn.close()

        if df.empty:
            print("  ⚠️  Chưa có page view events để xuất Parquet.")
            return

        # Ensure service date/time columns exist for stable reconciliation with Power BI Usage report.
        if "creation_time_service" not in df.columns:
            df["creation_time_service"] = None
        if "event_date_service" not in df.columns:
            df["event_date_service"] = None
        missing_service_date = df["event_date_service"].isna() | (df["event_date_service"].astype(str).str.len() == 0)
        df.loc[missing_service_date, "event_date_service"] = (
            df.loc[missing_service_date, "creation_time_service"].astype(str).str[:10]
        )

        # Derived metric flags to simplify Power BI measures.
        metric_flags = df["activity"].apply(classify_activity_metrics)
        df["report_open_count"] = metric_flags.apply(lambda x: x[0]).astype("int64")
        df["export_report_count"] = metric_flags.apply(lambda x: x[1]).astype("int64")
        df["interaction_count"] = metric_flags.apply(lambda x: x[2]).astype("int64")

        # UTC+7 helper columns for local reporting in Asia/Bangkok.
        dt_utc = pd.to_datetime(df["creation_time_utc"], utc=True, errors="coerce")
        dt_local = dt_utc.dt.tz_convert("Asia/Bangkok")
        df["creation_time_local_utc7"] = dt_local.dt.tz_localize(None)
        df["event_date_local_utc7"] = dt_local.dt.strftime("%Y-%m-%d")

        df.to_parquet(PARQUET_VIEW_EVENTS_FILE, engine="pyarrow", index=False)

        print("  🚀 Đã đồng bộ Parquet page view:")
        print(f"     {PARQUET_VIEW_EVENTS_FILE}")
    except Exception as e:
        print(f"  ❌ Lỗi khi xuất Parquet page view: {e}")


# ─── Export ────────────────────────────────────────
def export_to_csv():
    """Xuất toàn bộ dữ liệu tích lũy trong SQLite ra CSV."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    
    rows = conn.execute("""
        SELECT * FROM refresh_history ORDER BY start_time DESC
    """).fetchall()
    
    if not rows:
        print("  ❌ Database trống, chưa có dữ liệu để xuất.")
        conn.close()
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"refresh_history_full_{timestamp}.csv"
    filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)

    headers = [
        "STT", "ID Log", "Dataset ID", "Tên Dataset", "Workspace",
        "Status Refresh", "Type Refresh",
        "Thời gian bắt đầu", "Thời gian kết thúc",
        "Duration (giây)", "Description", "Ngày thu thập"
    ]

    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for i, row in enumerate(rows, 1):
            writer.writerow([
                i,
                row["request_id"],
                row["dataset_id"],
                row["dataset_name"],
                row["workspace_name"],
                row["status"],
                row["refresh_type"],
                row["start_time"],
                row["end_time"],
                row["duration_seconds"],
                row["error"],
                row["collected_at"]
            ])

    conn.close()
    print(f"\n  ✅ Đã xuất {len(rows)} dòng ra: {filepath}")


def show_stats():
    """Hiển thị thống kê nhanh từ DB."""
    if not os.path.exists(DB_FILE):
        print("  ❌ Chưa có database. Hãy chạy collector trước.")
        return

    conn = sqlite3.connect(DB_FILE)
    
    total = conn.execute("SELECT COUNT(*) FROM refresh_history").fetchone()[0]
    success = conn.execute("SELECT COUNT(*) FROM refresh_history WHERE status='Completed'").fetchone()[0]
    failed = conn.execute("SELECT COUNT(*) FROM refresh_history WHERE status='Failed'").fetchone()[0]
    ds_count = conn.execute("SELECT COUNT(DISTINCT dataset_name) FROM refresh_history").fetchone()[0]
    
    oldest = conn.execute("SELECT MIN(start_time) FROM refresh_history WHERE start_time IS NOT NULL").fetchone()[0]
    newest = conn.execute("SELECT MAX(start_time) FROM refresh_history WHERE start_time IS NOT NULL").fetchone()[0]
    
    last_collect = conn.execute("SELECT MAX(collected_at) FROM refresh_history").fetchone()[0]
    
    conn.close()

    rate = round(success / total * 100, 1) if total > 0 else 0

    print(f"\n{'=' * 55}")
    print(f"  📊 THỐNG KÊ REFRESH HISTORY DATABASE")
    print(f"{'=' * 55}")
    print(f"  💾 File DB     : {DB_FILE}")
    print(f"  📋 Tổng bản ghi: {total:,}")
    print(f"  📦 Datasets    : {ds_count}")
    print(f"  ✅ Thành công  : {success:,} ({rate}%)")
    print(f"  ❌ Thất bại    : {failed:,}")
    print(f"  📅 Dữ liệu từ : {oldest or 'N/A'}")
    print(f"  📅 Đến         : {newest or 'N/A'}")
    print(f"  🕐 Lần thu thập: {last_collect or 'N/A'}")
    print(f"{'=' * 55}")


# ─── Scheduler ─────────────────────────────────────
def show_data_volume_signals(limit: int = 20):
    if not os.path.exists(DB_FILE):
        print("  Chua co database de doc size monitor.")
        return

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT collected_at, file_path, file_size_bytes, previous_size_bytes,
                   delta_bytes, delta_ratio, signal, diagnosis
            FROM data_volume_signals
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()

    if not rows:
        print("  Chua co log dung luong file.")
        return

    print(f"\n{'=' * 55}")
    print("  DATA VOLUME SIGNALS")
    print(f"{'=' * 55}")
    for row in rows:
        delta_text = "N/A"
        if row["delta_bytes"] is not None:
            sign = "+" if row["delta_bytes"] >= 0 else "-"
            delta_text = f"{sign}{_format_bytes_human(abs(int(row['delta_bytes'])))}"
        ratio_text = "N/A"
        if row["delta_ratio"] is not None:
            ratio_text = f"{float(row['delta_ratio']) * 100:.2f}%"
        print(
            f"  [{row['collected_at']}] {row['signal']} | "
            f"{os.path.basename(row['file_path'])} | "
            f"size={_format_bytes_human(int(row['file_size_bytes']))} | "
            f"delta={delta_text} ({ratio_text})"
        )
        if row["diagnosis"]:
            print(f"    -> {row['diagnosis']}")


def run_scheduled_legacy(interval_hours=6):
    """Chạy collector lặp đi lặp lại theo chu kỳ."""
    interval_sec = interval_hours * 3600

    print(f"\n{'=' * 55}")
    print(f"  ⏰ CHẾ ĐỘ TỰ ĐỘNG - Mỗi {interval_hours} giờ")
    print(f"  Nhấn Ctrl+C để dừng")
    print(f"{'=' * 55}")

    config = load_config()
    client = PowerBIClient(rate_limit_per_minute=get_rate_limit_per_minute(config))
    client.get_access_token()

    cycle = 0
    while True:
        cycle += 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n── Chu kỳ #{cycle} | {now} ──")
        
        try:
            collect_once(client, config)
        except Exception as e:
            print(f"  ❌ Lỗi: {e}")
            # Thử refresh token
            try:
                client.get_access_token()
            except:
                pass

        next_run = datetime.now().strftime("%H:%M:%S")
        print(f"  ⏳ Lần tiếp theo sau {interval_hours} giờ...")
        time.sleep(interval_sec)


def run_scheduled(interval_hours=6):
    """Run collector in a single background schedule loop."""
    interval_sec = interval_hours * 3600
    schedule_lock = acquire_collector_lock(SCHEDULE_LOCK_FILE)
    if schedule_lock is None:
        print("  Skip schedule start: another background collector is already running.")
        return

    try:
        print(f"\n{'=' * 55}")
        print(f"  Schedule mode - every {interval_hours} hour(s)")
        print(f"{'=' * 55}")

        config = load_config()
        client = PowerBIClient(rate_limit_per_minute=get_rate_limit_per_minute(config))
        client.get_access_token()

        cycle = 0
        while True:
            cycle += 1
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n-- Cycle #{cycle} | {now} --")

            try:
                run_with_collector_lock(
                    RUN_LOCK_FILE,
                    "  Skip cycle: another collector run is in progress.",
                    lambda: collect_once(client, config),
                )
            except Exception as e:
                print(f"  Error: {e}")
                try:
                    client.get_access_token()
                except Exception:
                    pass

            print(f"  Next cycle in {interval_hours} hour(s)...")
            time.sleep(interval_sec)
    finally:
        release_collector_lock(schedule_lock, SCHEDULE_LOCK_FILE)


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─── Main ──────────────────────────────────────────
def main_legacy():
    parser = argparse.ArgumentParser(description="Power BI Refresh History Collector")
    parser.add_argument("--schedule", action="store_true", help="Chạy tự động định kỳ mỗi 6 giờ")
    parser.add_argument("--interval", type=int, default=6, help="Chu kỳ thu thập (giờ), mặc định 6")
    parser.add_argument("--export", action="store_true", help="Xuất refresh history ra Parquet")
    parser.add_argument("--export-views", action="store_true", help="Xuất raw page view events ra Parquet")
    parser.add_argument("--stats", action="store_true", help="Xem thống kê database")
    parser.add_argument("--backup-now", action="store_true", help="Backup DB ngay lập tức")
    parser.add_argument("--collect-views", action="store_true", help="Thu thập raw report view events từ Power BI Activity API")
    parser.add_argument("--views-lookback-days", type=int, default=0, help="Override số ngày lookback khi chạy --collect-views")
    parser.add_argument("--collect-audit-events", action="store_true", help="Thu thập full raw audit events từ Power BI Activity API")
    parser.add_argument("--audit-lookback-days", type=int, default=0, help="Override số ngày lookback khi chạy --collect-audit-events")
    parser.add_argument("--audit-activity-filter", type=str, default="", help="Filter Activity cụ thể khi chạy --collect-audit-events; để trống = all")
    parser.add_argument("--repair-view-dates", action="store_true", help="Backfill event_date_service từ raw payload cho dữ liệu cũ")
    parser.add_argument("--list-ignored", action="store_true", help="Liệt kê dataset đang bị ignore")
    parser.add_argument("--ignore-key", action="append", default=[], help="Thêm key ignore theo format workspace_id:dataset_id")
    parser.add_argument("--unignore-key", action="append", default=[], help="Gỡ key ignore theo format workspace_id:dataset_id")
    parser.add_argument("--validate-ignored", action="store_true", help="Kiểm tra lại dataset trong ignore list và tự gỡ nếu đã hoạt động")
    args = parser.parse_args()

    manage_ignore_mode = (
        args.list_ignored
        or bool(args.ignore_key)
        or bool(args.unignore_key)
        or args.validate_ignored
    )

    if manage_ignore_mode:
        config = load_config()

        if args.ignore_key:
            keys, invalid = normalize_dataset_keys(args.ignore_key)
            added = persist_ignored_dataset_keys(config, keys)
            print(f"\nĐã thêm {added} key vào ignore list.")
            for item in invalid:
                print(f"  ⚠️ Key không hợp lệ: {item}")

        if args.unignore_key:
            keys, invalid = normalize_dataset_keys(args.unignore_key)
            removed = remove_ignored_dataset_keys(config, keys)
            print(f"\nĐã gỡ {removed} key khỏi ignore list.")
            for item in invalid:
                print(f"  ⚠️ Key không hợp lệ: {item}")

        if args.validate_ignored:
            client = PowerBIClient(rate_limit_per_minute=get_rate_limit_per_minute(config))
            print("\n🔑 Đang xác thực để validate ignore list...")
            client.get_access_token()
            validate_ignored_dataset_keys(client, config)

        if args.list_ignored:
            list_ignored_dataset_keys(config)

        return

    if args.stats:
        show_stats()
        return

    if args.backup_now:
        config = load_config()
        run_with_collector_lock(
            RUN_LOCK_FILE,
            "  Skip backup: another collector run is in progress.",
            lambda: backup_database(config),
        )
        return

    if args.collect_views:
        config = load_config()
        client = PowerBIClient(rate_limit_per_minute=get_rate_limit_per_minute(config))
        print("\n  🔑 Đang xác thực...")
        client.get_access_token()
        print("  ✅ OK!\n")
        override_days = args.views_lookback_days if args.views_lookback_days > 0 else None
        collect_activity_events_once(client, config, override_lookback_days=override_days)
        repaired = repair_activity_event_service_dates()
        if repaired > 0:
            print(f"  🛠️ Đã backfill {repaired} dòng service-date.")
        export_view_events_to_parquet()
        return

    if args.collect_audit_events:
        config = load_config()
        client = PowerBIClient(rate_limit_per_minute=get_rate_limit_per_minute(config))
        print("\n  🔑 Đang xác thực...")
        client.get_access_token()
        print("  ✅ OK!\n")
        override_days = args.audit_lookback_days if args.audit_lookback_days > 0 else None
        override_filter = normalize_activity_filter(args.audit_activity_filter)
        collect_audit_events_once(
            client,
            config,
            override_lookback_days=override_days,
            override_activity_filter=override_filter,
        )
        return

    if args.repair_view_dates:
        repaired = repair_activity_event_service_dates()
        print(f"  🛠️ Đã backfill {repaired} dòng service-date.")
        export_view_events_to_parquet()
        return

    # Chạy đồng bộ thủ công ngay lập tức ra Parquet nếu cần
    if args.export:
        print("\n📥 ĐANG XUẤT DỮ LIỆU REFRESH RA PARQUET...")
        export_to_parquet()
        return

    if args.export_views:
        print("\n📥 ĐANG XUẤT PAGE VIEW EVENTS RA PARQUET...")
        export_view_events_to_parquet()
        return

    if args.schedule:
        run_scheduled(interval_hours=args.interval)
        return

    # Mặc định: chạy 1 lần
    print(f"\n{'=' * 55}")
    print(f"  📡 THU THẬP REFRESH HISTORY")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 55}")

    config = load_config()
    client = PowerBIClient(rate_limit_per_minute=get_rate_limit_per_minute(config))
    
    print("\n  🔑 Đang xác thực...")
    client.get_access_token()
    print("  ✅ OK!\n")

    collect_once(client, config)
    
    print(f"\n  💡 Gợi ý:")
    print(f"     --schedule    Chạy tự động định kỳ")
    print(f"     --export      Xuất DB ra CSV")
    print(f"     --stats       Xem thống kê DB")


def main():
    parser = argparse.ArgumentParser(description="Power BI Refresh History Collector")
    parser.add_argument("--schedule", action="store_true", help="Run background collector loop")
    parser.add_argument("--interval", type=int, default=6, help="Collector interval in hours")
    parser.add_argument("--export", action="store_true", help="Export refresh history to Parquet")
    parser.add_argument("--export-views", action="store_true", help="Export raw page view events to Parquet")
    parser.add_argument("--stats", action="store_true", help="Show database stats")
    parser.add_argument("--size-signals", action="store_true", help="Show data-volume monitoring signals")
    parser.add_argument("--size-signals-limit", type=int, default=20, help="Limit rows for --size-signals")
    parser.add_argument("--backup-now", action="store_true", help="Back up SQLite immediately")
    parser.add_argument("--collect-views", action="store_true", help="Collect raw page view events")
    parser.add_argument("--views-lookback-days", type=int, default=0, help="Override page-view lookback days")
    parser.add_argument("--collect-audit-events", action="store_true", help="Collect full audit events")
    parser.add_argument("--audit-lookback-days", type=int, default=0, help="Override audit lookback days")
    parser.add_argument("--audit-activity-filter", type=str, default="", help="Optional audit activity filter")
    parser.add_argument("--repair-view-dates", action="store_true", help="Repair missing service dates")
    parser.add_argument("--list-ignored", action="store_true", help="List ignored datasets")
    parser.add_argument("--ignore-key", action="append", default=[], help="Add ignored dataset key")
    parser.add_argument("--unignore-key", action="append", default=[], help="Remove ignored dataset key")
    parser.add_argument("--validate-ignored", action="store_true", help="Revalidate ignored datasets")
    args = parser.parse_args()

    manage_ignore_mode = (
        args.list_ignored
        or bool(args.ignore_key)
        or bool(args.unignore_key)
        or args.validate_ignored
    )

    if manage_ignore_mode:
        config = load_config()

        if args.ignore_key:
            keys, invalid = normalize_dataset_keys(args.ignore_key)
            added = persist_ignored_dataset_keys(config, keys)
            print(f"\nAdded {added} key(s) to ignore list.")
            for item in invalid:
                print(f"  Invalid key: {item}")

        if args.unignore_key:
            keys, invalid = normalize_dataset_keys(args.unignore_key)
            removed = remove_ignored_dataset_keys(config, keys)
            print(f"\nRemoved {removed} key(s) from ignore list.")
            for item in invalid:
                print(f"  Invalid key: {item}")

        if args.validate_ignored:
            client = PowerBIClient(rate_limit_per_minute=get_rate_limit_per_minute(config))
            print("\nAuthenticating to validate ignore list...")
            client.get_access_token()
            validate_ignored_dataset_keys(client, config)

        if args.list_ignored:
            list_ignored_dataset_keys(config)

        return

    if args.stats:
        show_stats()
        return

    if args.size_signals:
        show_data_volume_signals(limit=args.size_signals_limit)
        return

    if args.backup_now:
        config = load_config()
        run_with_collector_lock(
            RUN_LOCK_FILE,
            "  Skip backup: another collector run is in progress.",
            lambda: backup_database(config),
        )
        return

    if args.collect_views:
        config = load_config()
        client = PowerBIClient(rate_limit_per_minute=get_rate_limit_per_minute(config))
        print("\nAuthenticating...")
        client.get_access_token()
        print("  OK\n")
        override_days = args.views_lookback_days if args.views_lookback_days > 0 else None

        def run_collect_views():
            collect_activity_events_once(
                client, config, override_lookback_days=override_days
            )
            repaired = repair_activity_event_service_dates()
            if repaired > 0:
                print(f"  Repaired {repaired} service-date row(s).")
            export_view_events_to_parquet()

        run_with_collector_lock(
            RUN_LOCK_FILE,
            "  Skip collect-views: another collector run is in progress.",
            run_collect_views,
        )
        return

    if args.collect_audit_events:
        config = load_config()
        client = PowerBIClient(rate_limit_per_minute=get_rate_limit_per_minute(config))
        print("\nAuthenticating...")
        client.get_access_token()
        print("  OK\n")
        override_days = args.audit_lookback_days if args.audit_lookback_days > 0 else None
        override_filter = normalize_activity_filter(args.audit_activity_filter)
        run_with_collector_lock(
            RUN_LOCK_FILE,
            "  Skip collect-audit-events: another collector run is in progress.",
            lambda: collect_audit_events_once(
                client,
                config,
                override_lookback_days=override_days,
                override_activity_filter=override_filter,
            ),
        )
        return

    if args.repair_view_dates:
        def run_repair_view_dates():
            repaired = repair_activity_event_service_dates()
            print(f"  Repaired {repaired} service-date row(s).")
            export_view_events_to_parquet()

        run_with_collector_lock(
            RUN_LOCK_FILE,
            "  Skip repair-view-dates: another collector run is in progress.",
            run_repair_view_dates,
        )
        return

    if args.export:
        print("\nExporting refresh history to Parquet...")
        export_to_parquet()
        return

    if args.export_views:
        print("\nExporting page view events to Parquet...")
        export_view_events_to_parquet()
        return

    if args.schedule:
        run_scheduled(interval_hours=args.interval)
        return

    if is_collector_lock_active(SCHEDULE_LOCK_FILE):
        print("  Skip one-off run: background scheduler is already active.")
        return

    print(f"\n{'=' * 55}")
    print("  POWER BI REFRESH HISTORY COLLECTION")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 55}")

    config = load_config()
    client = PowerBIClient(rate_limit_per_minute=get_rate_limit_per_minute(config))

    print("\nAuthenticating...")
    client.get_access_token()
    print("  OK\n")

    run_with_collector_lock(
        RUN_LOCK_FILE,
        "  Skip one-off run: another collector run is in progress.",
        lambda: collect_once(client, config),
    )

    print("\nHints:")
    print("  --schedule    Run background schedule")
    print("  --export      Export DB to Parquet")
    print("  --stats       Show database stats")


if __name__ == "__main__":
    main()
