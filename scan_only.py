import yaml
from powerbi_client import PowerBIClient

def scan_only():
    print("="*60)
    print(" QUÉT DỮ LIỆU POWER BI (KHÔNG GHI VÀO CONFIG)")
    print("="*60)
    
    client = PowerBIClient()
    client.get_access_token()

    print(" Đang lấy danh sách Workspaces...")
    workspaces = client.get_workspaces()
    
    results = {}
    
    for ws in workspaces:
        ws_name = ws.get('name', 'Unknown')
        print(f"  > Quét Workspace: {ws_name}...")
        datasets = client.get_datasets(ws["id"])
        
        refreshable = []
        for ds in datasets:
            if ds.get('isRefreshable', False):
                refreshable.append({
                    "id": ds["id"],
                    "name": ds.get("name", "Unknown")
                })
        
        if refreshable:
            results[ws_name] = refreshable

    print("\n" + "="*60)
    print(f" KẾT QUẢ QUÉT: {len(results)} Workspaces có Datasets")
    print("="*60)
    
    # In ra định dạng YAML để User copy nếu cần
    print(yaml.dump({"powerbi": {"workspaces_scanned": results}}, allow_unicode=True, sort_keys=False, default_flow_style=False))

if __name__ == "__main__":
    scan_only()
