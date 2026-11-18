import yaml
from pathlib import Path
from powerbi_client import PowerBIClient

def auto_update_config():
    print("="*60)
    print(" CÔNG CỤ TỰ ĐỘNG CẬP NHẬT CONFIG (THÊM MỚI)")
    print("="*60)
    
    config_path = Path("pbi_config.yaml")
    if not config_path.exists():
        print("Lỗi: Không tìm thấy pbi_config.yaml!")
        return

    # 1. Đọc config hiện tại
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    # Chuyển đổi config hiện tại sang dictionary để tra cứu nhanh: {workspace_id: {dataset_ids: set}}
    existing_data = {}
    for ws in config.get('powerbi', {}).get('workspaces', []):
        existing_data[ws['id']] = {ds['id'] for ds in ws.get('datasets', [])}

    # 2. Kết nối Power BI API
    client = PowerBIClient()
    client.get_access_token()

    print(" Đang quét dữ liệu mới nhất từ Power BI...")
    workspaces = client.get_workspaces()
    
    new_workspaces_count = 0
    new_datasets_count = 0
    
    # 3. Quét và so sánh
    current_workspaces = config.setdefault('powerbi', {}).setdefault('workspaces', [])
    
    for ws in workspaces:
        ws_id = ws['id']
        ws_name = ws.get('name', 'Unknown')
        
        # Lấy danh sách dataset của workspace này
        pbi_datasets = client.get_datasets(ws_id)
        refreshable_pbi = [ds for ds in pbi_datasets if ds.get('isRefreshable', False)]
        
        if not refreshable_pbi:
            continue

        # Nếu Workspace chưa có trong config
        if ws_id not in existing_data:
            print(f" [+] Thêm mới Workspace: {ws_name}")
            current_workspaces.append({
                "id": ws_id,
                "name": ws_name,
                "datasets": [{"id": ds["id"], "name": ds.get("name", "Unknown")} for ds in refreshable_pbi]
            })
            new_workspaces_count += 1
            new_datasets_count += len(refreshable_pbi)
        else:
            # Nếu Workspace đã có, kiểm tra từng dataset
            existing_ds_ids = existing_data[ws_id]
            
            # Tìm workspace object trong list hiện tại để append dataset
            ws_entry = next(item for item in current_workspaces if item["id"] == ws_id)
            
            for ds in refreshable_pbi:
                if ds["id"] not in existing_ds_ids:
                    print(f"  [+] Thêm dataset mới vào '{ws_name}': {ds.get('name', 'Unknown')}")
                    ws_entry.setdefault("datasets", []).append({
                        "id": ds["id"],
                        "name": ds.get("name", "Unknown")
                    })
                    new_datasets_count += 1

    # 4. Ghi lại vào file nếu có thay đổi
    if new_datasets_count > 0 or new_workspaces_count > 0:
        print(f"\n >>> Tìm thấy {new_workspaces_count} Workspace mới và {new_datasets_count} Dataset mới.")
        print(" Đang cập nhật pbi_config.yaml...")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        print(" DONE! Đã cập nhật xong.")
    else:
        print("\n Không tìm thấy dữ liệu mới nào để cập nhật.")

if __name__ == "__main__":
    auto_update_config()
