import yaml
from pathlib import Path
from powerbi_client import PowerBIClient

def sync_workspaces():
    print("="*60)
    print(" CÔNG CỤ TỰ ĐỘNG ĐỒNG BỘ POWER BI DATASETS")
    print("="*60)
    
    print(" Đang kết nối Power BI API...")
    client = PowerBIClient()
    client.get_access_token()

    print(" Đang lấy danh sách Workspaces...")
    workspaces = client.get_workspaces()
    
    config_path = Path("pbi_config.yaml")
    if not config_path.exists():
        print("Lỗi: Không tìm thấy pbi_config.yaml!")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
        
    compiled_workspaces = []
    total_datasets = 0
    
    for ws in workspaces:
        print(f"  > Quét Workspace: {ws.get('name', 'Unknown')}...")
        datasets = client.get_datasets(ws["id"])
        
        refreshable_datasets = []
        for ds in datasets:
            if ds.get('isRefreshable', False):
                refreshable_datasets.append({
                    "id": ds["id"],
                    "name": ds.get("name", "Unknown")
                })
                
        if refreshable_datasets:
            compiled_workspaces.append({
                "id": ws["id"],
                "name": ws.get("name", "Unknown"),
                "datasets": refreshable_datasets
            })
            total_datasets += len(refreshable_datasets)
            
    # Cập nhật config
    if 'powerbi' not in config:
        config['powerbi'] = {}
    config['powerbi']['workspaces'] = compiled_workspaces
    
    print(f"\n Đã tìm thấy {len(compiled_workspaces)} Workspaces & {total_datasets} Datasets có thể Refresh!")
    print(" Đang ghi cấu hình vào pbi_config.yaml...")
    
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        
    print(" DONE! Hãy khởi động lại (Restart) Server để áp dụng thay đổi.")

if __name__ == "__main__":
    sync_workspaces()
