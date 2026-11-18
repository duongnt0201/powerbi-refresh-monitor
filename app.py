"""
Power BI Refresh Dashboard - Flask Backend
Serves dashboard UI and provides API endpoints for refresh status data.
"""

import json
import sys
import random
import time
import sqlite3
import yaml
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, jsonify, request
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

from powerbi_client import PowerBIClient

app = Flask(__name__)
DB_FILE = Path(__file__).with_name("powerbi_dashboard.db")

# Global state
config = {}
powerbi_client = None
demo_mode = False
app_initialized = False
app_init_error = None
init_lock = Lock()
status_cache_lock = Lock()
status_cache_payload = None
status_cache_expire_at = 0.0


def get_latest_data_volume_signals(limit: int = 5) -> List[Dict[str, Any]]:
    if not DB_FILE.exists():
        return []

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

    return [
        {
            "collected_at": row["collected_at"],
            "file_path": row["file_path"],
            "file_size_bytes": row["file_size_bytes"],
            "previous_size_bytes": row["previous_size_bytes"],
            "delta_bytes": row["delta_bytes"],
            "delta_ratio": row["delta_ratio"],
            "signal": row["signal"],
            "diagnosis": row["diagnosis"],
        }
        for row in rows
    ]


def load_config(config_path: str = "pbi_config.yaml") -> dict:
    """Load configuration from YAML file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            "Please create pbi_config.yaml with your Power BI credentials."
        )
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def is_demo_mode() -> bool:
    """Check if demo mode should be activated."""
    return "--demo" in sys.argv


def dataset_key(workspace_id: str, dataset_id: str) -> str:
    return f"{workspace_id}:{dataset_id}"


def get_ignored_dataset_keys(current_config: dict) -> set:
    collector_cfg = current_config.get("collector", {}) or {}
    return set(collector_cfg.get("ignored_dataset_keys", []) or [])


def get_rate_limit_per_minute(current_config: dict) -> Optional[int]:
    collector_cfg = current_config.get("collector", {}) or {}
    value = collector_cfg.get("rate_limit_per_minute")
    if isinstance(value, int) and value >= 0:
        return value
    return None


def get_dashboard_rate_limit_per_minute(current_config: dict) -> Optional[int]:
    dashboard_cfg = current_config.get("dashboard", {}) or {}
    value = dashboard_cfg.get("rate_limit_per_minute")
    if isinstance(value, int) and value >= 0:
        return value
    return get_rate_limit_per_minute(current_config)


def get_status_cache_seconds(current_config: dict) -> int:
    dashboard_cfg = current_config.get("dashboard", {}) or {}
    value = dashboard_cfg.get("status_cache_seconds")
    if isinstance(value, int) and value >= 0:
        return value
    return 45


def get_dashboard_workers(current_config: dict, total_datasets: int) -> int:
    dashboard_cfg = current_config.get("dashboard", {}) or {}
    configured = dashboard_cfg.get("max_workers")
    if isinstance(configured, int) and configured > 0:
        return min(max(1, configured), max(1, total_datasets))

    if total_datasets <= 1:
        return 1
    return min(12, total_datasets)


def get_status_data_source(current_config: dict) -> str:
    dashboard_cfg = current_config.get("dashboard", {}) or {}
    value = str(dashboard_cfg.get("status_data_source", "hybrid")).strip().lower()
    if value in {"db", "api", "hybrid", "hybrid_today_api"}:
        return value
    return "hybrid"


def get_api_fallback_workers(current_config: dict, total_datasets: int) -> int:
    dashboard_cfg = current_config.get("dashboard", {}) or {}
    configured = dashboard_cfg.get("api_fallback_workers")
    if isinstance(configured, int) and configured > 0:
        return min(max(1, configured), max(1, total_datasets))
    if total_datasets <= 1:
        return 1
    return min(4, total_datasets)


def get_hybrid_db_lookback_days(current_config: dict) -> int:
    dashboard_cfg = current_config.get("dashboard", {}) or {}
    value = dashboard_cfg.get("hybrid_db_lookback_days")
    if isinstance(value, int) and value > 0:
        return value
    return 30


def get_hybrid_today_api_top(current_config: dict) -> int:
    dashboard_cfg = current_config.get("dashboard", {}) or {}
    value = dashboard_cfg.get("hybrid_today_api_top")
    if isinstance(value, int) and value > 0:
        return value
    return 5


def get_hybrid_today_api_scope(current_config: dict) -> str:
    dashboard_cfg = current_config.get("dashboard", {}) or {}
    value = str(dashboard_cfg.get("hybrid_today_api_scope", "active_today_only")).strip().lower()
    if value in {"all", "active_today_only"}:
        return value
    return "active_today_only"


def generate_demo_data():
    """Generate realistic demo data for dashboard preview."""
    demo_datasets = [
        ("Sales Report Daily", "Production Reports"),
        ("HR Dashboard", "Production Reports"),
        ("Finance KPIs", "Production Reports"),
        ("Marketing Analytics", "Marketing"),
        ("Customer Churn Model", "Data Science"),
        ("Inventory Tracker", "Operations"),
        ("CRM Lead Pipeline", "Sales Team"),
        ("Employee Satisfaction", "HR Workspace"),
        ("Revenue Forecast", "Finance"),
        ("Support Tickets", "Customer Service"),
        ("Website Analytics", "Marketing"),
        ("Supply Chain Monitor", "Operations"),
    ]
    
    now = datetime.now()
    results = []
    
    for i, (ds_name, ws_name) in enumerate(demo_datasets):
        # Generate varied refresh history
        history = []
        random.seed(i + int(now.timestamp() / 3600))  # Stable per hour
        
        num_records = random.randint(8, 20)
        base_duration = random.uniform(60, 600)  # 1-10 minutes
        
        for j in range(num_records):
            hours_ago = j * random.uniform(6, 12)
            start = now - timedelta(hours=hours_ago)
            duration = base_duration + random.gauss(0, base_duration * 0.2)
            duration = max(30, duration)
            end = start + timedelta(seconds=duration)
            
            # Most succeed, some fail
            if j == 0 and i in [1, 4]:  # HR and Churn model currently failed
                status = "Failed"
                error = "The credentials provided for the data source are invalid." if i == 1 else "Query timeout expired. The execution ran for too long."
            elif random.random() < 0.08:
                status = "Failed"
                error = random.choice([
                    "The credentials provided for the data source are invalid.",
                    "Data source connection error.",
                    "Query timeout expired.",
                    "Memory allocation error during processing."
                ])
            else:
                status = "Completed"
                error = None
            
            refresh_type = random.choice(["Scheduled", "Scheduled", "Scheduled", "OnDemand"])
            
            history.append({
                "request_id": f"demo-{i}-{j}",
                "status": status,
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "duration_seconds": round(duration, 1),
                "refresh_type": refresh_type,
                "error": error
            })
        
        total = len(history)
        success_count = sum(1 for h in history if h["status"] == "Completed")
        failed_count = sum(1 for h in history if h["status"] == "Failed")
        durations = [h["duration_seconds"] for h in history if h["status"] == "Completed" and h.get("duration_seconds")]
        avg_dur = round(sum(durations) / len(durations), 1) if durations else None
        
        results.append({
            "workspace_id": f"demo-ws-{i}",
            "workspace_name": ws_name,
            "dataset_id": f"demo-ds-{i}",
            "dataset_name": ds_name,
            "latest": history[0] if history else None,
            "history": history,
            "stats": {
                "total": total,
                "success": success_count,
                "failed": failed_count,
                "success_rate": round(success_count / total * 100, 1) if total > 0 else 0,
                "avg_duration_seconds": avg_dur
            }
        })
    
    return results


def get_pbi_client():
    """Lazy initialize the Power BI client to survive multiprocess environments."""
    global powerbi_client
    if powerbi_client is None and not demo_mode:
        powerbi_client = PowerBIClient(
            rate_limit_per_minute=get_dashboard_rate_limit_per_minute(config)
        )
    return powerbi_client


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def build_dataset_result(
    ws_id: str,
    ws_name: str,
    ds_id: str,
    ds_name: str,
    history: List[Dict[str, Any]],
    error: Optional[str] = None,
) -> Dict[str, Any]:
    total = len(history)
    success_count = sum(1 for h in history if h["status"] == "Completed")
    failed_count = sum(1 for h in history if h["status"] == "Failed")
    durations = [h["duration_seconds"] for h in history if h.get("duration_seconds") is not None]
    avg_duration = sum(durations) / len(durations) if durations else None

    payload = {
        "workspace_id": ws_id,
        "workspace_name": ws_name,
        "dataset_id": ds_id,
        "dataset_name": ds_name,
        "latest": history[0] if history else None,
        "history": history,
        "stats": {
            "total": total,
            "success": success_count,
            "failed": failed_count,
            "success_rate": round(success_count / total * 100, 1) if total > 0 else 0,
            "avg_duration_seconds": round(avg_duration, 1) if avg_duration else None,
        },
    }
    if error:
        payload["error"] = error
    return payload


def query_history_from_db(tasks: List[tuple], history_count: int) -> Dict[str, List[Dict[str, Any]]]:
    if history_count <= 0 or not tasks or not DB_FILE.exists():
        return {}

    key_to_rows = {}
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    try:
        query = """
        SELECT request_id, dataset_id, dataset_name, workspace_id, workspace_name,
               status, refresh_type, start_time, end_time, duration_seconds, error
        FROM refresh_history
        WHERE workspace_id = ? AND dataset_id = ?
        ORDER BY start_time DESC, collected_at DESC
        LIMIT ?
        """
        for ws_id, _, ds_id, _ in tasks:
            rows = conn.execute(query, (ws_id, ds_id, history_count)).fetchall()
            formatted = []
            for row in rows:
                start = parse_dt(row["start_time"])
                end = parse_dt(row["end_time"])
                duration = row["duration_seconds"]
                if duration is None and start and end:
                    duration = (end - start).total_seconds()

                formatted.append({
                    "request_id": row["request_id"],
                    "status": row["status"] or "Unknown",
                    "start_time": start.isoformat() if start else None,
                    "end_time": end.isoformat() if end else None,
                    "duration_seconds": float(duration) if duration is not None else None,
                    "refresh_type": row["refresh_type"],
                    "error": row["error"] or None,
                })
            key_to_rows[dataset_key(ws_id, ds_id)] = formatted
    finally:
        conn.close()

    return key_to_rows


def fetch_dataset_history_api(ws_id: str, ds_id: str, history_count: int) -> List[Dict[str, Any]]:
    history = get_pbi_client().get_refresh_history(ws_id, ds_id, top=history_count)
    formatted_history = []
    for h in history:
        start = h.get("start_time")
        end = h.get("end_time")
        duration = None
        if start and end:
            duration = (end - start).total_seconds()

        error_detail = None
        if h.get("service_exception_json"):
            try:
                err_json = json.loads(h["service_exception_json"])
                error_detail = err_json.get("errorDescription") or str(err_json)
            except (json.JSONDecodeError, TypeError):
                error_detail = str(h["service_exception_json"])

        formatted_history.append({
            "request_id": h.get("request_id"),
            "status": h.get("status", "Unknown"),
            "start_time": start.isoformat() if start else None,
            "end_time": end.isoformat() if end else None,
            "duration_seconds": duration,
            "refresh_type": h.get("refresh_type"),
            "error": error_detail
        })
    return formatted_history


def query_history_from_db_before_today(
    tasks: List[tuple],
    history_count: int,
    lookback_days: int
) -> Dict[str, List[Dict[str, Any]]]:
    if history_count <= 0 or not tasks or not DB_FILE.exists():
        return {}

    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    window_start = today_start - timedelta(days=lookback_days)
    key_to_rows = {}

    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    try:
        query = """
        SELECT request_id, dataset_id, dataset_name, workspace_id, workspace_name,
               status, refresh_type, start_time, end_time, duration_seconds, error
        FROM refresh_history
        WHERE workspace_id = ? AND dataset_id = ?
          AND start_time >= ? AND start_time < ?
        ORDER BY start_time DESC, collected_at DESC
        LIMIT ?
        """
        for ws_id, _, ds_id, _ in tasks:
            rows = conn.execute(
                query,
                (ws_id, ds_id, window_start.strftime("%Y-%m-%d %H:%M:%S"), today_start.strftime("%Y-%m-%d %H:%M:%S"), history_count)
            ).fetchall()
            formatted = []
            for row in rows:
                start = parse_dt(row["start_time"])
                end = parse_dt(row["end_time"])
                duration = row["duration_seconds"]
                if duration is None and start and end:
                    duration = (end - start).total_seconds()

                formatted.append({
                    "request_id": row["request_id"],
                    "status": row["status"] or "Unknown",
                    "start_time": start.isoformat() if start else None,
                    "end_time": end.isoformat() if end else None,
                    "duration_seconds": float(duration) if duration is not None else None,
                    "refresh_type": row["refresh_type"],
                    "error": row["error"] or None,
                })
            key_to_rows[dataset_key(ws_id, ds_id)] = formatted
    finally:
        conn.close()

    return key_to_rows


def get_today_active_dataset_keys(tasks: List[tuple]) -> set:
    if not tasks or not DB_FILE.exists():
        return set()

    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    active = set()

    conn = sqlite3.connect(str(DB_FILE))
    try:
        query = """
        SELECT 1
        FROM refresh_history
        WHERE workspace_id = ? AND dataset_id = ? AND start_time >= ?
        LIMIT 1
        """
        today_str = today_start.strftime("%Y-%m-%d %H:%M:%S")
        for ws_id, _, ds_id, _ in tasks:
            row = conn.execute(query, (ws_id, ds_id, today_str)).fetchone()
            if row:
                active.add(dataset_key(ws_id, ds_id))
    finally:
        conn.close()

    return active


def filter_today_rows(history_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    today = datetime.now().date()
    result = []
    for row in history_rows:
        start = parse_dt(row.get("start_time"))
        if start and start.date() == today:
            result.append(row)
    return result


def merge_history_rows(today_rows: List[Dict[str, Any]], older_rows: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    seen = set()
    merged = []
    for row in (today_rows or []) + (older_rows or []):
        req_id = row.get("request_id")
        if req_id:
            key = ("id", req_id)
        else:
            key = ("fallback", row.get("start_time"), row.get("status"), row.get("refresh_type"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)

    def sort_key(row: Dict[str, Any]) -> float:
        dt = parse_dt(row.get("start_time"))
        if dt is None:
            return 0.0
        try:
            return dt.timestamp()
        except Exception:
            return 0.0

    merged.sort(key=sort_key, reverse=True)
    if limit > 0:
        return merged[:limit]
    return merged

def init_app():
    """Initialize app configuration."""
    global config, demo_mode
    config = load_config()
    demo_mode = is_demo_mode()
    
    if not demo_mode:
        data_source = get_status_data_source(config)
        if data_source in {"api", "hybrid_today_api"}:
            # Pre-warm token only when dashboard reads live API directly.
            try:
                print("\n[Dashboard] Kiem tra xac thuc Power BI API...")
                get_pbi_client().get_access_token()
            except Exception as exc:
                # Keep server alive; /api/status can still return DB-backed data.
                print(f"[Dashboard] Warning: API pre-auth failed, continue with degraded mode: {exc}")
        else:
            print(f"\n[Dashboard] Status data source: {data_source} (DB-first)")
    else:
        print("[Dashboard] DEMO MODE - Running with fake data")
        print("[Dashboard] To use real data, remove 'your-email' in pbi_config.yaml")


def ensure_initialized():
    """Initialize app once in a thread-safe way."""
    global app_initialized, app_init_error

    if app_initialized:
        return
    if app_init_error is not None:
        raise RuntimeError(app_init_error)

    with init_lock:
        if app_initialized:
            return
        if app_init_error is not None:
            raise RuntimeError(app_init_error)

        try:
            init_app()
            app_initialized = True
        except Exception as exc:
            app_init_error = str(exc)
            raise


@app.before_request
def ensure_app_ready():
    """Ensure app config/auth is ready before handling any request."""
    try:
        ensure_initialized()
    except Exception as exc:
        if request.path.startswith("/api/"):
            return jsonify({"error": str(exc)}), 500
        return f"Dashboard initialization failed: {exc}", 500


@app.route("/")
def dashboard():
    """Serve the main dashboard page."""
    dashboard_config = config.get("dashboard", {})
    return render_template(
        "index.html",
        refresh_interval=dashboard_config.get("refresh_interval_seconds", 60),
        page_title=dashboard_config.get("title", "Power BI Refresh Monitor")
    )


@app.route("/history")
def history_page():
    """Serve the raw data history page."""
    dashboard_config = config.get("dashboard", {})
    return render_template(
        "history.html",
        page_title=dashboard_config.get("title", "Power BI Refresh Monitor")
    )


@app.route("/api/status")
def get_status():
    """Get current refresh status for all configured datasets."""
    global status_cache_payload, status_cache_expire_at

    # Demo mode: return generated fake data
    if demo_mode:
        results = generate_demo_data()
        total_datasets = len(results)
        total_success = sum(1 for r in results if r.get("latest") and r["latest"]["status"] == "Completed")
        total_failed = sum(1 for r in results if r.get("latest") and r["latest"]["status"] == "Failed")
        all_durations = [r["stats"]["avg_duration_seconds"] for r in results if r["stats"].get("avg_duration_seconds")]
        overall_avg = round(sum(all_durations) / len(all_durations), 1) if all_durations else None
        
        return jsonify({
            "timestamp": datetime.now().isoformat(),
            "demo_mode": True,
            "summary": {
                "total_datasets": total_datasets,
                "success": total_success,
                "failed": total_failed,
                "in_progress": 0,
                "overall_success_rate": round(total_success / total_datasets * 100, 1) if total_datasets > 0 else 0,
                "avg_duration_seconds": overall_avg,
                "data_volume_signal": {
                    "signal": "normal_increase_or_stable",
                    "diagnosis": "Demo mode placeholder."
                }
            },
            "datasets": results
        })

    cache_seconds = get_status_cache_seconds(config)
    now_mono = time.monotonic()
    if cache_seconds > 0:
        with status_cache_lock:
            if status_cache_payload is not None and now_mono < status_cache_expire_at:
                return jsonify(status_cache_payload)

    data_source = get_status_data_source(config)
    results_map = {}
    workspaces = config["powerbi"].get("workspaces", [])
    history_count = config.get("dashboard", {}).get("history_count", 20)
    ignored_keys = get_ignored_dataset_keys(config)
    ignored_count = 0
    tasks = []

    for workspace in workspaces:
        ws_id = workspace["id"]
        ws_name = workspace.get("name", ws_id)
        for dataset in workspace.get("datasets") or []:
            ds_id = dataset["id"]
            ds_name = dataset.get("name", ds_id)
            if dataset_key(ws_id, ds_id) in ignored_keys:
                ignored_count += 1
                continue
            tasks.append((ws_id, ws_name, ds_id, ds_name))

    if data_source in {"db", "hybrid"}:
        db_history = query_history_from_db(tasks, history_count)
        for ws_id, ws_name, ds_id, ds_name in tasks:
            key = dataset_key(ws_id, ds_id)
            if key in db_history:
                rows = db_history[key]
                if rows:
                    results_map[key] = build_dataset_result(ws_id, ws_name, ds_id, ds_name, rows)
    elif data_source == "hybrid_today_api":
        lookback_days = get_hybrid_db_lookback_days(config)
        db_history = query_history_from_db_before_today(tasks, history_count, lookback_days)
        for ws_id, ws_name, ds_id, ds_name in tasks:
            key = dataset_key(ws_id, ds_id)
            rows = db_history.get(key) or []
            if rows:
                results_map[key] = build_dataset_result(ws_id, ws_name, ds_id, ds_name, rows)

    def fetch_one_api(task_info):
        ws_id, ws_name, ds_id, ds_name = task_info
        try:
            api_top = history_count
            if data_source == "hybrid_today_api":
                api_top = get_hybrid_today_api_top(config)
            history = fetch_dataset_history_api(ws_id, ds_id, api_top)
            if data_source == "hybrid_today_api":
                history = filter_today_rows(history)
            return dataset_key(ws_id, ds_id), build_dataset_result(ws_id, ws_name, ds_id, ds_name, history)
        except Exception as e:
            return dataset_key(ws_id, ds_id), build_dataset_result(
                ws_id, ws_name, ds_id, ds_name, [], error=str(e)
            )

    if data_source == "api":
        api_tasks = tasks
        max_workers = get_dashboard_workers(config, len(api_tasks))
    elif data_source == "hybrid":
        api_tasks = [
            item for item in tasks
            if dataset_key(item[0], item[2]) not in results_map
        ]
        max_workers = get_api_fallback_workers(config, len(api_tasks))
    elif data_source == "hybrid_today_api":
        scope = get_hybrid_today_api_scope(config)
        if scope == "active_today_only":
            active_keys = get_today_active_dataset_keys(tasks)
            api_tasks = [
                item for item in tasks
                if dataset_key(item[0], item[2]) in active_keys
            ]
        else:
            api_tasks = tasks
        max_workers = get_api_fallback_workers(config, len(api_tasks))
    else:
        api_tasks = []
        max_workers = 0

    if api_tasks:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for key, item in executor.map(fetch_one_api, api_tasks):
                if data_source == "hybrid_today_api":
                    old_rows = []
                    if key in results_map:
                        old_rows = results_map[key].get("history", [])
                    merged_rows = merge_history_rows(
                        item.get("history", []),
                        old_rows,
                        history_count
                    )
                    results_map[key] = build_dataset_result(
                        item.get("workspace_id"),
                        item.get("workspace_name"),
                        item.get("dataset_id"),
                        item.get("dataset_name"),
                        merged_rows,
                        error=item.get("error"),
                    )
                else:
                    results_map[key] = item

    results = []
    for ws_id, ws_name, ds_id, ds_name in tasks:
        key = dataset_key(ws_id, ds_id)
        if key not in results_map:
            results_map[key] = build_dataset_result(ws_id, ws_name, ds_id, ds_name, [])
        results.append(results_map[key])

    # Calculate overall summary
    total_datasets = len(results)
    total_success = sum(
        1 for r in results 
        if r.get("latest") and r["latest"]["status"] == "Completed"
    )
    total_failed = sum(
        1 for r in results 
        if r.get("latest") and r["latest"]["status"] == "Failed"
    )
    total_in_progress = sum(
        1 for r in results 
        if r.get("latest") and r["latest"]["status"] not in ["Completed", "Failed", "Cancelled"]
    )
    
    # Total refresh counts across all datasets (success/total)
    total_refresh_success = sum(r["stats"]["success"] for r in results)
    total_refresh_count = sum(r["stats"]["total"] for r in results)
    
    # Overall average duration
    all_durations = []
    for r in results:
        avg = r["stats"].get("avg_duration_seconds")
        if avg:
            all_durations.append(avg)
    overall_avg_duration = (
        sum(all_durations) / len(all_durations) if all_durations else None
    )
    size_signals = get_latest_data_volume_signals(limit=5)
    latest_size_signal = size_signals[0] if size_signals else None
    
    payload = {
        "timestamp": datetime.now().isoformat(),
        "status_data_source": data_source,
        "summary": {
            "total_datasets": total_datasets,
            "success": total_success,
            "failed": total_failed,
            "in_progress": total_in_progress,
            "ignored_datasets": ignored_count,
            "overall_success_rate": round(
                total_success / total_datasets * 100, 1
            ) if total_datasets > 0 else 0,
            "avg_duration_seconds": round(overall_avg_duration, 1) if overall_avg_duration else None,
            "total_refresh_success": total_refresh_success,
            "total_refresh_count": total_refresh_count,
            "data_volume_signal": latest_size_signal,
        },
        "datasets": results,
        "data_volume_signals": size_signals,
    }

    if cache_seconds > 0:
        with status_cache_lock:
            status_cache_payload = payload
            status_cache_expire_at = time.monotonic() + cache_seconds

    return jsonify(payload)


@app.route("/api/discover")
def discover_workspaces():
    """Discover all accessible workspaces and their datasets (for setup)."""
    try:
        workspaces = get_pbi_client().get_workspaces()
        result = []
        for ws in workspaces:
            datasets = get_pbi_client().get_datasets(ws["id"])
            result.append({
                "id": ws["id"],
                "name": ws.get("name", "Unknown"),
                "type": ws.get("type", "Unknown"),
                "datasets": [
                    {
                        "id": ds["id"],
                        "name": ds.get("name", "Unknown"),
                        "configured_by": ds.get("configuredBy", ""),
                        "is_refreshable": ds.get("isRefreshable", False)
                    }
                    for ds in datasets
                ]
            })
        return jsonify({"workspaces": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    ensure_initialized()
    dashboard_config = config.get("dashboard", {})
    port = dashboard_config.get("port", 5555)
    
    mode_label = " [DEMO]" if demo_mode else ""
    if not demo_mode:
        ignored_keys = get_ignored_dataset_keys(config)
        ds_count = sum(
            1
            for ws in config["powerbi"].get("workspaces", [])
            for ds in (ws.get("datasets") or [])
            if dataset_key(ws.get("id"), ds.get("id")) not in ignored_keys
        )
    else:
        ds_count = 12
    
    print(f"\n{'='*55}")
    print(f"  Power BI Refresh Dashboard{mode_label}")
    print(f"  http://localhost:{port}")
    print(f"  Monitoring {ds_count} dataset(s)")
    print(f"{'='*55}\n")
    
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
