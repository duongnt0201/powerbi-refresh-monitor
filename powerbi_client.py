"""
Power BI REST API Client
Handles authentication via MSAL using Device Code Flow.
Supports MFA and caches tokens for background execution.
"""

import os
import sys
import atexit
import time
import json
import threading
import requests
from urllib.parse import quote
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from msal import PublicClientApplication, SerializableTokenCache


def _ensure_utf8_console_output():
    """Force UTF-8 console output to avoid UnicodeEncodeError on Windows code pages."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_ensure_utf8_console_output()


class PowerBIClient:
    """Client for Power BI REST API using MSAL Device Code Flow."""
    
    # Sử dụng Azure CLI client ID hoặc Microsoft Power BI Client ID
    PUBLIC_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46" # Azure CLI (rất thân thiện với mọi loại tenant)
    AUTHORITY_URL = "https://login.microsoftonline.com/common"
    API_BASE_URL = "https://api.powerbi.com/v1.0/myorg"
    CACHE_FILE = "powerbi_token_cache.bin"
    SCOPES = ["https://analysis.windows.net/powerbi/api/.default"]
    API_TIMEOUT_SECONDS = 30
    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
    MAX_RETRIES = 3
    BASE_BACKOFF_SECONDS = 1.5
    MAX_BACKOFF_SECONDS = 30
    DEFAULT_RATE_LIMIT_PER_MINUTE = 180
    DEFAULT_API_ERROR_LOG_DIR = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "logs",
        "api_errors"
    )

    def __init__(self, rate_limit_per_minute: Optional[int] = None):
        """Khởi tạo client và tải Cache nếu có."""
        self.cache = SerializableTokenCache()
        env_rpm = os.getenv("PBI_RATE_LIMIT_PER_MINUTE")
        if rate_limit_per_minute is not None:
            self.rate_limit_per_minute = max(0, int(rate_limit_per_minute))
        elif env_rpm and env_rpm.isdigit():
            self.rate_limit_per_minute = max(0, int(env_rpm))
        else:
            self.rate_limit_per_minute = self.DEFAULT_RATE_LIMIT_PER_MINUTE
        self._throttle_lock = threading.Lock()
        self._next_request_time = 0.0
        self._log_lock = threading.Lock()
        self.api_error_log_dir = os.getenv(
            "PBI_API_ERROR_LOG_DIR",
            self.DEFAULT_API_ERROR_LOG_DIR
        )

        # Load token từ cache file nếu tồn tại
        if os.path.exists(self.CACHE_FILE):
            try:
                with open(self.CACHE_FILE, "r") as f:
                    self.cache.deserialize(f.read())
            except Exception:
                pass
                
        # Đảm bảo cache được lưu khi script thoát
        atexit.register(self._save_cache)
                
        self.app = PublicClientApplication(
            self.PUBLIC_CLIENT_ID,
            authority=self.AUTHORITY_URL,
            token_cache=self.cache
        )
        self._access_token = None

    def _save_cache(self):
        """Lưu state cache ra file nếu có thay đổi."""
        if self.cache.has_state_changed:
            with open(self.CACHE_FILE, "w") as f:
                f.write(self.cache.serialize())

    def get_access_token(self) -> str:
        """
        Lấy Access Token.
        1. Thử lấy ngầm từ Cache (dùng Refresh Token).
        2. Nếu thất bại, yêu cầu đăng nhập qua Device Code Flow.
        """
        accounts = self.app.get_accounts()
        result = None
        
        # Bước 1: Thử lấy token ngầm (Silent Flow)
        if accounts:
            result = self.app.acquire_token_silent(self.SCOPES, account=accounts[0])
            
        # Bước 2: Không lấy được ngầm (lần đầu hoặc token hết hạn hoàn toàn) => Dùng Device Code
        if not result or "access_token" not in result:
            print("\n" + "="*60)
            print(" YEU CAU XAC THUC POWER BI (HO TRO MFA)")
            print("="*60)
            
            flow = self.app.initiate_device_flow(scopes=self.SCOPES)
            if "user_code" not in flow:
                raise Exception(f"Khong the khoi tao dang nhap: {flow.get('error')}")
            
            # In ra Terminal huong dan nguoi dung dang nhap
            print(flow.get('message'))
            print(" Script dang cho ban dang nhap tren trinh duyet...")
            print("="*60 + "\n")
            
            # Block cho den khi dang nhap xong
            result = self.app.acquire_token_by_device_flow(flow)
            
            if "access_token" in result:
                print(" Dang nhap Power BI thanh cong!")
                print(" Da luu session vao cache de script chay ngam sau nay.")
            else:
                error = result.get('error')
                error_desc = result.get('error_description')
                raise Exception(f" Dang nhap that bai: {error} - {error_desc}")
                
        self._access_token = result['access_token']
        return self._access_token

    def _throttle_requests(self):
        """Throttle outbound API calls to reduce 429 responses."""
        rpm = self.rate_limit_per_minute
        if rpm <= 0:
            return

        min_interval = 60.0 / float(rpm)
        with self._throttle_lock:
            now = time.monotonic()
            wait_seconds = self._next_request_time - now
            if wait_seconds > 0:
                time.sleep(wait_seconds)
                now = time.monotonic()
            self._next_request_time = max(self._next_request_time, now) + min_interval

    def _log_api_error(
        self,
        endpoint: str,
        method: str,
        message: str,
        status_code: Optional[int] = None,
        attempt: Optional[int] = None,
        will_retry: bool = False
    ):
        """Write API errors to daily JSONL file for audit and shift handover."""
        try:
            os.makedirs(self.api_error_log_dir, exist_ok=True)
            now = datetime.now()
            log_file = os.path.join(
                self.api_error_log_dir,
                f"api_errors_{now.strftime('%Y-%m-%d')}.jsonl"
            )
            payload = {
                "timestamp": now.isoformat(timespec="seconds"),
                "service": "powerbi",
                "endpoint": endpoint,
                "method": method,
                "status_code": status_code,
                "attempt": attempt,
                "will_retry": will_retry,
                "message": message,
            }
            with self._log_lock:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            # Logging must never break the data pipeline.
            pass
    
    def _compute_retry_delay(self, attempt: int, response: Optional[requests.Response] = None) -> float:
        """Compute retry delay with Retry-After support and bounded backoff."""
        retry_after = None
        if response is not None:
            retry_after = response.headers.get("Retry-After")

        if retry_after:
            try:
                return min(float(retry_after), self.MAX_BACKOFF_SECONDS)
            except ValueError:
                pass

        delay = self.BASE_BACKOFF_SECONDS * (2 ** attempt)
        return min(delay, self.MAX_BACKOFF_SECONDS)

    def _api_request(self, endpoint: str, method: str = "GET") -> Dict[str, Any]:
        """Tạo JWT API Request."""
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            url = endpoint
        else:
            url = f"{self.API_BASE_URL}/{endpoint}"
        last_error: Optional[Exception] = None

        for attempt in range(self.MAX_RETRIES + 1):
            token = self.get_access_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }

            try:
                self._throttle_requests()
                response = requests.request(
                    method,
                    url,
                    headers=headers,
                    timeout=self.API_TIMEOUT_SECONDS
                )
            except requests.exceptions.RequestException as e:
                last_error = e
                should_retry = attempt < self.MAX_RETRIES
                self._log_api_error(
                    endpoint=endpoint,
                    method=method,
                    message=str(e),
                    status_code=getattr(getattr(e, "response", None), "status_code", None),
                    attempt=attempt + 1,
                    will_retry=should_retry
                )
                if attempt < self.MAX_RETRIES:
                    delay = self._compute_retry_delay(attempt)
                    print(f"[PowerBI] API network error, retry in {delay:.1f}s ({attempt + 1}/{self.MAX_RETRIES})")
                    time.sleep(delay)
                    continue
                raise Exception(f"Power BI API request failed: {e}")

            status = response.status_code

            if 200 <= status < 300:
                if not response.content:
                    return {}
                try:
                    return response.json()
                except ValueError:
                    return {}

            if status == 401 and attempt < self.MAX_RETRIES:
                self._log_api_error(
                    endpoint=endpoint,
                    method=method,
                    message="Unauthorized (401)",
                    status_code=status,
                    attempt=attempt + 1,
                    will_retry=True
                )
                self._access_token = None
                delay = self._compute_retry_delay(attempt, response)
                print(f"[PowerBI] Unauthorized (401), retry in {delay:.1f}s ({attempt + 1}/{self.MAX_RETRIES})")
                time.sleep(delay)
                continue

            if status in self.RETRYABLE_STATUS_CODES and attempt < self.MAX_RETRIES:
                self._log_api_error(
                    endpoint=endpoint,
                    method=method,
                    message=response.text[:300].replace("\n", " "),
                    status_code=status,
                    attempt=attempt + 1,
                    will_retry=True
                )
                delay = self._compute_retry_delay(attempt, response)
                print(f"[PowerBI] API returned {status}, retry in {delay:.1f}s ({attempt + 1}/{self.MAX_RETRIES})")
                time.sleep(delay)
                continue

            if status in [401, 403]:
                print("Lỗi quyền truy cập API. Có thể account không có quyền hoặc token hết hạn.")

            body = response.text[:500].replace("\n", " ")
            self._log_api_error(
                endpoint=endpoint,
                method=method,
                message=body,
                status_code=status,
                attempt=attempt + 1,
                will_retry=False
            )
            raise Exception(f"Power BI API request failed ({status}): {body}")

        if last_error is not None:
            raise Exception(f"Power BI API request failed: {last_error}")
        raise Exception("Power BI API request failed: unknown error")

    def get_activity_events(
        self,
        start_datetime_utc: str,
        end_datetime_utc: str,
        activity_filter: Optional[str] = "ViewReport",
    ) -> List[Dict[str, Any]]:
        """
        Pull audit activity events from Power BI Admin API for a single UTC day window.
        start_datetime_utc/end_datetime_utc should be ISO8601 UTC strings.
        """
        filter_query = ""
        if activity_filter:
            filter_expr = f"Activity eq '{activity_filter}'"
            filter_query = f"&$filter={quote(filter_expr)}"

        endpoint = (
            "admin/activityevents"
            f"?startDateTime='{start_datetime_utc}'"
            f"&endDateTime='{end_datetime_utc}'"
            f"{filter_query}"
        )

        events: List[Dict[str, Any]] = []
        next_endpoint = endpoint
        max_pages = 1000
        pages = 0

        while next_endpoint and pages < max_pages:
            data = self._api_request(next_endpoint)
            events.extend(data.get("activityEventEntities", []) or [])
            next_endpoint = data.get("continuationUri")
            pages += 1

        return events
    
    def get_refresh_history(
        self, workspace_id: str, dataset_id: str, top: int = 10, suppress_errors: bool = True
    ) -> List[Dict[str, Any]]:
        """Lấy lịch sử refresh của 1 Dataset."""
        endpoint = f"groups/{workspace_id}/datasets/{dataset_id}/refreshes?$top={top}"
        
        try:
            data = self._api_request(endpoint)
            refreshes = data.get("value", [])
            
            formatted = []
            for refresh in refreshes:
                record = {
                    "request_id": refresh.get("requestId"),
                    "status": refresh.get("status", "Unknown"),
                    "start_time": self._parse_datetime(refresh.get("startTime")),
                    "end_time": self._parse_datetime(refresh.get("endTime")),
                    "refresh_type": refresh.get("refreshType"),
                    "service_exception_json": refresh.get("serviceExceptionJson")
                }
                formatted.append(record)
            
            return formatted
        except Exception as e:
            if not suppress_errors:
                raise
            print(f"[PowerBI] Failed to get refresh history for DS {dataset_id}: {e}")
            return []
    
    def get_latest_refresh(
        self, workspace_id: str, dataset_id: str
    ) -> Optional[Dict[str, Any]]:
        history = self.get_refresh_history(workspace_id, dataset_id, top=1)
        return history[0] if history else None
    
    def get_datasets(self, workspace_id: str) -> List[Dict[str, Any]]:
        endpoint = f"groups/{workspace_id}/datasets"
        try:
            data = self._api_request(endpoint)
            return data.get("value", [])
        except Exception as e:
            print(f"[PowerBI] Failed to get datasets: {e}")
            return []
    
    def get_workspaces(self) -> List[Dict[str, Any]]:
        endpoint = "groups"
        try:
            data = self._api_request(endpoint)
            return data.get("value", [])
        except Exception as e:
            print(f"[PowerBI] Failed to get workspaces: {e}")
            return []
    
    @staticmethod
    def _parse_datetime(dt_string: Optional[str]) -> Optional[datetime]:
        if not dt_string:
            return None
        try:
            dt_string = dt_string.replace("Z", "+00:00")
            return datetime.fromisoformat(dt_string)
        except ValueError:
            return None
