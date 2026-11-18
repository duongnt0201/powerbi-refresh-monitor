"""
Export Raw Refresh History to CSV
Lấy tối đa 60 bản ghi mỗi dataset (giới hạn cứng của Power BI REST API)
KHÔNG lọc 30 ngày - lấy tất cả dữ liệu có thể.
"""

import csv
import json
import sys
import os
import yaml
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

# Thêm đường dẫn để import PowerBIClient
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from powerbi_client import PowerBIClient


def load_config():
    with open("pbi_config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_max_workers(config, total_ds: int) -> int:
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


def dataset_key(workspace_id: str, dataset_id: str) -> str:
    return f"{workspace_id}:{dataset_id}"


def get_ignored_dataset_keys(config) -> set:
    collector_cfg = {}
    if isinstance(config, dict):
        collector_cfg = config.get("collector", {}) or {}
    return set(collector_cfg.get("ignored_dataset_keys", []) or [])


def main():
    print("=" * 60)
    print("  EXPORT RAW REFRESH HISTORY -> CSV")
    print("  Lấy tối đa 60 bản ghi/dataset (Power BI API limit)")
    print("=" * 60)

    config = load_config()
    client = PowerBIClient(rate_limit_per_minute=get_rate_limit_per_minute(config))

    # Đảm bảo xác thực
    print("\n[1/3] Đang xác thực Power BI API...")
    client.get_access_token()
    print("  ✅ Xác thực thành công!")

    # Thu thập danh sách tasks
    workspaces = config["powerbi"].get("workspaces", [])
    ignored_keys = get_ignored_dataset_keys(config)
    MAX_TOP = 60  # Giới hạn cứng của Power BI REST API

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
    print(f"\n[2/3] Đang kéo dữ liệu từ {total_ds} datasets (top={MAX_TOP} mỗi dataset, workers={max_workers})...")
    if skipped_ignored > 0:
        print(f"  ⏭️ Bỏ qua {skipped_ignored} dataset trong ignore list")
    if total_ds == 0:
        print("  ⚠️ Không có dataset hợp lệ để export.")
        return

    all_rows = []
    errors = []

    def fetch_one(task_info):
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

                rows.append({
                    "request_id": h.get("request_id", ""),
                    "dataset_name": ds_name,
                    "workspace_name": ws_name,
                    "status": h.get("status", "Unknown"),
                    "refresh_type": h.get("refresh_type", ""),
                    "start_time": start.strftime("%Y-%m-%d %H:%M:%S") if start else "",
                    "end_time": end.strftime("%Y-%m-%d %H:%M:%S") if end else "",
                    "duration_seconds": duration if duration is not None else "",
                    "error": error_detail
                })
        except Exception as e:
            errors.append(f"  ❌ {ds_name}: {e}")
        return rows

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for batch in executor.map(fetch_one, tasks):
            all_rows.extend(batch)

    # Sắp xếp mới nhất lên đầu
    all_rows.sort(key=lambda r: r.get("start_time") or "", reverse=True)

    if errors:
        print(f"\n  ⚠️ Có {len(errors)} dataset gặp lỗi:")
        for e in errors:
            print(e)

    # Xuất CSV
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"raw_refresh_history_{timestamp}.csv"
    filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)

    headers = [
        "STT",
        "ID Log",
        "Tên Dataset",
        "Workspace",
        "Status Refresh",
        "Type Refresh",
        "Thời gian bắt đầu",
        "Thời gian kết thúc",
        "Duration (giây)",
        "Description"
    ]

    print(f"\n[3/3] Đang ghi {len(all_rows)} dòng ra file CSV...")

    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for i, row in enumerate(all_rows, 1):
            writer.writerow([
                i,
                row["request_id"],
                row["dataset_name"],
                row["workspace_name"],
                row["status"],
                row["refresh_type"],
                row["start_time"],
                row["end_time"],
                row["duration_seconds"],
                row["error"]
            ])

    print(f"\n{'=' * 60}")
    print(f"  ✅ HOÀN TẤT!")
    print(f"  📄 File: {filepath}")
    print(f"  📊 Tổng: {len(all_rows)} lượt refresh từ {total_ds} datasets")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
