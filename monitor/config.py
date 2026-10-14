from __future__ import annotations
import json
import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("ROUTER_SCAN_CONFIG_DIR", PROJECT_DIR)).resolve()
DEPLOYMENT_FILE = CONFIG_DIR / "deployment.json"
_deployment = json.loads(DEPLOYMENT_FILE.read_text(encoding="utf-8")) if DEPLOYMENT_FILE.exists() else {}
if not isinstance(_deployment, dict):
    raise ValueError("deployment.json 必须是 JSON 对象")
ROUTER_BACKEND = os.environ.get("ROUTER_SCAN_BACKEND", _deployment.get("backend", "zte"))
if ROUTER_BACKEND not in {"zte", "openwrt"}:
    raise ValueError("采集模式必须为 zte 或 openwrt")
WEB_BIND = os.environ.get("ROUTER_SCAN_WEB_BIND", _deployment.get("web_bind", "127.0.0.1"))
TLS_VERIFY = _deployment.get("tls_verify", True)
if not isinstance(TLS_VERIFY, bool):
    raise ValueError("tls_verify 必须是布尔值")
HTTP_TIMEOUT = float(_deployment.get("http_timeout", 6))
if not 0 < HTTP_TIMEOUT <= 120:
    raise ValueError("http_timeout 必须大于 0 且不超过 120 秒")
CREDENTIALS_FILE = CONFIG_DIR / "zte_credentials.json"

# Push settings
PUSHPLUS_TOKEN = ""
PUSHPLUS_TOPIC = "1"
PUSHPLUS_TITLE = "Warming"
WEBHOOK_URL = ""

# RSSI threshold
DEFAULT_RSSI_THRESHOLD = 66
RSSI_THRESHOLD_FILE = CONFIG_DIR / "rssi_threshold.json"
RSSI_THRESHOLD: int = DEFAULT_RSSI_THRESHOLD


def _normalize_threshold(value: int) -> int:
    value_int = int(value)
    return max(1, value_int)


def _load_rssi_threshold() -> int:
    try:
        if RSSI_THRESHOLD_FILE.exists():
            raw = json.loads(RSSI_THRESHOLD_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                raw = raw.get("rssi_threshold", raw.get("threshold", raw))
            if isinstance(raw, (int, float, str)):
                return _normalize_threshold(raw)
    except Exception as exc:
        print(f"[WARN] 加载 RSSI 阈值失败：{exc}")
    return DEFAULT_RSSI_THRESHOLD


def set_rssi_threshold(value: int) -> int:
    global RSSI_THRESHOLD
    normalized = _normalize_threshold(value)
    RSSI_THRESHOLD = normalized
    try:
        RSSI_THRESHOLD_FILE.write_text(
            json.dumps({"rssi_threshold": normalized}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"[WARN] 无法保存 RSSI 阈值：{exc}")
    return RSSI_THRESHOLD


RSSI_THRESHOLD = _load_rssi_threshold()

# Monitor/web settings
SETTINGS_FILE = CONFIG_DIR / "monitor_settings.json"
DEFAULT_STATUS_NOTIFY_ENABLED = True
DEFAULT_SUSPECT_NOTIFY_ENABLED = True
DEFAULT_EXIT_NOTIFY_ENABLED = True
DEFAULT_PUSH_NOTIFY_ENABLED = False
DEFAULT_BEEP_ON_EVENT = False
DEFAULT_CHART_MAX_POINTS = 60
DEFAULT_CHART_Y_RANGE_DB = 90
DEFAULT_CHART_Y_MIN_DB = 0
DEFAULT_NO_TRAFFIC_SAMPLE_LIMIT = 5
DEFAULT_DEVICE_ABSENCE_TOLERANCE = 1
DEFAULT_ROUTER_BASE_URL = "http://192.168.1.1"
DEFAULT_ROUTER_FETCH_PATH = "/?_type=vueData&_tag=vue_topo_data&Action=GetALLClients"

STATUS_NOTIFY_ENABLED = DEFAULT_STATUS_NOTIFY_ENABLED
SUSPECT_NOTIFY_ENABLED = DEFAULT_SUSPECT_NOTIFY_ENABLED
EXIT_NOTIFY_ENABLED = DEFAULT_EXIT_NOTIFY_ENABLED
PUSH_NOTIFY_ENABLED = DEFAULT_PUSH_NOTIFY_ENABLED
BEEP_ON_EVENT = DEFAULT_BEEP_ON_EVENT
CHART_MAX_POINTS = DEFAULT_CHART_MAX_POINTS
CHART_Y_RANGE_DB = DEFAULT_CHART_Y_RANGE_DB
CHART_Y_MIN_DB = DEFAULT_CHART_Y_MIN_DB
NO_TRAFFIC_SAMPLE_LIMIT = DEFAULT_NO_TRAFFIC_SAMPLE_LIMIT
DEVICE_ABSENCE_TOLERANCE = DEFAULT_DEVICE_ABSENCE_TOLERANCE
ROUTER_BASE_URL = DEFAULT_ROUTER_BASE_URL
ROUTER_FETCH_PATH = DEFAULT_ROUTER_FETCH_PATH


def _load_monitor_settings() -> dict:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception as exc:
        print(f"[WARN] 加载监控设置失败：{exc}")
        return {}


def _normalize_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _normalize_positive_int(value, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(value))
    except Exception:
        return default


def _normalize_base_url(url: str | None) -> str:
    from urllib.parse import urlsplit
    raw = (url or "").strip()
    if not raw:
        raw = DEFAULT_ROUTER_BASE_URL
    if not raw.startswith("http"):
        raw = f"http://{raw}"
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("路由器地址必须包含 http/https 和主机")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("路由器地址只填写协议、主机和端口，不要包含账号、路径或 #/")
    parsed.port  # validate malformed port numbers
    return raw.rstrip("/")


def _normalize_fetch_path(path: str | None) -> str:
    raw = (path or "").strip() or DEFAULT_ROUTER_FETCH_PATH
    return raw if raw.startswith("/") else f"/{raw}"


def _build_fetch_url(base_url: str, fetch_path: str) -> str:
    return f"{_normalize_base_url(base_url)}{_normalize_fetch_path(fetch_path)}"


def _apply_router_settings(*, base_url: str | None = None, fetch_path: str | None = None) -> None:
    global ROUTER_BASE_URL, ROUTER_FETCH_PATH, URL
    if base_url is not None:
        ROUTER_BASE_URL = _normalize_base_url(base_url)
    if fetch_path is not None:
        ROUTER_FETCH_PATH = _normalize_fetch_path(fetch_path)
    URL = _build_fetch_url(ROUTER_BASE_URL, ROUTER_FETCH_PATH)


URL = _build_fetch_url(ROUTER_BASE_URL, ROUTER_FETCH_PATH)


def _apply_monitor_settings(data: dict) -> None:
    global STATUS_NOTIFY_ENABLED, SUSPECT_NOTIFY_ENABLED, EXIT_NOTIFY_ENABLED, PUSH_NOTIFY_ENABLED, BEEP_ON_EVENT
    global CHART_MAX_POINTS, CHART_Y_RANGE_DB, CHART_Y_MIN_DB
    global NO_TRAFFIC_SAMPLE_LIMIT, DEVICE_ABSENCE_TOLERANCE
    global TARGET_MAC, TARGET_NAME_CONTAINS
    STATUS_NOTIFY_ENABLED = _normalize_bool(data.get("status_notify_enabled"), STATUS_NOTIFY_ENABLED)
    SUSPECT_NOTIFY_ENABLED = _normalize_bool(data.get("suspect_notify_enabled"), SUSPECT_NOTIFY_ENABLED)
    EXIT_NOTIFY_ENABLED = _normalize_bool(data.get("exit_notify_enabled"), EXIT_NOTIFY_ENABLED)
    PUSH_NOTIFY_ENABLED = _normalize_bool(data.get("push_notify_enabled"), PUSH_NOTIFY_ENABLED)
    BEEP_ON_EVENT = _normalize_bool(data.get("beep_on_event"), BEEP_ON_EVENT)
    CHART_MAX_POINTS = _normalize_positive_int(data.get("chart_max_points", CHART_MAX_POINTS), DEFAULT_CHART_MAX_POINTS)
    CHART_Y_RANGE_DB = _normalize_positive_int(data.get("chart_y_range_db", CHART_Y_RANGE_DB), DEFAULT_CHART_Y_RANGE_DB)
    CHART_Y_MIN_DB = _normalize_positive_int(data.get("chart_y_min_db", CHART_Y_MIN_DB), DEFAULT_CHART_Y_MIN_DB, minimum=0)
    NO_TRAFFIC_SAMPLE_LIMIT = _normalize_positive_int(
        data.get("no_traffic_sample_limit", NO_TRAFFIC_SAMPLE_LIMIT),
        DEFAULT_NO_TRAFFIC_SAMPLE_LIMIT,
    )
    DEVICE_ABSENCE_TOLERANCE = _normalize_positive_int(
        data.get("device_absence_tolerance", DEVICE_ABSENCE_TOLERANCE),
        DEFAULT_DEVICE_ABSENCE_TOLERANCE,
        minimum=0,
    )
    if "router_base_url" in data or "router_fetch_path" in data:
        _apply_router_settings(
            base_url=data.get("router_base_url") if "router_base_url" in data else None,
            fetch_path=data.get("router_fetch_path") if "router_fetch_path" in data else None,
        )
    if "target_mac" in data:
        TARGET_MAC = str(data.get("target_mac") or "").strip()
    if "target_name_contains" in data:
        TARGET_NAME_CONTAINS = str(data.get("target_name_contains") or "").strip()


def save_monitor_settings() -> None:
    data = {
        "status_notify_enabled": STATUS_NOTIFY_ENABLED,
        "suspect_notify_enabled": SUSPECT_NOTIFY_ENABLED,
        "exit_notify_enabled": EXIT_NOTIFY_ENABLED,
        "push_notify_enabled": PUSH_NOTIFY_ENABLED,
        "beep_on_event": BEEP_ON_EVENT,
        "chart_max_points": CHART_MAX_POINTS,
        "chart_y_range_db": CHART_Y_RANGE_DB,
        "chart_y_min_db": CHART_Y_MIN_DB,
        "no_traffic_sample_limit": NO_TRAFFIC_SAMPLE_LIMIT,
        "device_absence_tolerance": DEVICE_ABSENCE_TOLERANCE,
        "target_mac": TARGET_MAC,
        "target_name_contains": TARGET_NAME_CONTAINS,
        "router_base_url": ROUTER_BASE_URL,
        "router_fetch_path": ROUTER_FETCH_PATH,
    }
    try:
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[WARN] 无法保存监控设置：{exc}")


def set_monitor_settings(
    *,
    status_notify_enabled: bool | None = None,
    suspect_notify_enabled: bool | None = None,
    exit_notify_enabled: bool | None = None,
    push_notify_enabled: bool | None = None,
    beep_on_event: bool | None = None,
    chart_max_points: int | None = None,
    chart_y_range_db: int | None = None,
    chart_y_min_db: int | None = None,
    no_traffic_sample_limit: int | None = None,
    device_absence_tolerance: int | None = None,
    router_base_url: str | None = None,
    router_fetch_path: str | None = None,
    target_mac: str | None = None,
    target_name_contains: str | None = None,
) -> dict:
    if status_notify_enabled is not None:
        globals()["STATUS_NOTIFY_ENABLED"] = _normalize_bool(status_notify_enabled, STATUS_NOTIFY_ENABLED)
    if suspect_notify_enabled is not None:
        globals()["SUSPECT_NOTIFY_ENABLED"] = _normalize_bool(suspect_notify_enabled, SUSPECT_NOTIFY_ENABLED)
    if exit_notify_enabled is not None:
        globals()["EXIT_NOTIFY_ENABLED"] = _normalize_bool(exit_notify_enabled, EXIT_NOTIFY_ENABLED)
    if push_notify_enabled is not None:
        globals()["PUSH_NOTIFY_ENABLED"] = _normalize_bool(push_notify_enabled, PUSH_NOTIFY_ENABLED)
    if beep_on_event is not None:
        globals()["BEEP_ON_EVENT"] = _normalize_bool(beep_on_event, BEEP_ON_EVENT)
    if chart_max_points is not None:
        globals()["CHART_MAX_POINTS"] = _normalize_positive_int(chart_max_points, DEFAULT_CHART_MAX_POINTS)
    if chart_y_range_db is not None:
        globals()["CHART_Y_RANGE_DB"] = _normalize_positive_int(chart_y_range_db, DEFAULT_CHART_Y_RANGE_DB)
    if chart_y_min_db is not None:
        globals()["CHART_Y_MIN_DB"] = _normalize_positive_int(chart_y_min_db, DEFAULT_CHART_Y_MIN_DB, minimum=0)
    if no_traffic_sample_limit is not None:
        globals()["NO_TRAFFIC_SAMPLE_LIMIT"] = _normalize_positive_int(
            no_traffic_sample_limit,
            DEFAULT_NO_TRAFFIC_SAMPLE_LIMIT,
        )
    if device_absence_tolerance is not None:
        globals()["DEVICE_ABSENCE_TOLERANCE"] = _normalize_positive_int(
            device_absence_tolerance,
            DEFAULT_DEVICE_ABSENCE_TOLERANCE,
            minimum=0,
        )
    if router_base_url is not None or router_fetch_path is not None:
        _apply_router_settings(base_url=router_base_url, fetch_path=router_fetch_path)
    if target_mac is not None:
        globals()["TARGET_MAC"] = str(target_mac).strip()
    if target_name_contains is not None:
        globals()["TARGET_NAME_CONTAINS"] = str(target_name_contains).strip()
    save_monitor_settings()
    return get_monitor_settings()


def get_monitor_settings() -> dict:
    return {
        "status_notify_enabled": STATUS_NOTIFY_ENABLED,
        "suspect_notify_enabled": SUSPECT_NOTIFY_ENABLED,
        "exit_notify_enabled": EXIT_NOTIFY_ENABLED,
        "push_notify_enabled": PUSH_NOTIFY_ENABLED,
        "beep_on_event": BEEP_ON_EVENT,
        "chart_max_points": CHART_MAX_POINTS,
        "chart_y_range_db": CHART_Y_RANGE_DB,
        "chart_y_min_db": CHART_Y_MIN_DB,
        "no_traffic_sample_limit": NO_TRAFFIC_SAMPLE_LIMIT,
        "device_absence_tolerance": DEVICE_ABSENCE_TOLERANCE,
        "target_mac": TARGET_MAC,
        "target_name_contains": TARGET_NAME_CONTAINS,
        "router_base_url": ROUTER_BASE_URL,
        "router_fetch_path": ROUTER_FETCH_PATH,
        "chart_max_points_default": DEFAULT_CHART_MAX_POINTS,
        "chart_y_range_db_default": DEFAULT_CHART_Y_RANGE_DB,
        "chart_y_min_db_default": DEFAULT_CHART_Y_MIN_DB,
        "no_traffic_sample_limit_default": DEFAULT_NO_TRAFFIC_SAMPLE_LIMIT,
        "device_absence_tolerance_default": DEFAULT_DEVICE_ABSENCE_TOLERANCE,
        "target_mac_default": DEFAULT_TARGET_MAC,
        "target_name_contains_default": DEFAULT_TARGET_NAME_CONTAINS,
        "router_base_url_default": DEFAULT_ROUTER_BASE_URL,
        "router_fetch_path_default": DEFAULT_ROUTER_FETCH_PATH,
        "rssi_stable_window_seconds": RSSI_STABLE_WINDOW_SECONDS,
        "rssi_stable_min_samples": RSSI_STABLE_MIN_SAMPLES,
        "traffic_idle_window_seconds": TRAFFIC_IDLE_WINDOW_SECONDS,
        "traffic_active_min_bps": TRAFFIC_ACTIVE_MIN_BPS,
    }


# File paths
COOKIE_FILE = Path("cookie.txt")
STATE_FILE = Path("zte_clients_state.json")
EVENT_LOG = Path("target_events.log")
TARGET_HISTORY_FILE = Path("target_history.json")
STATUS_LOG = Path("target_status.log")
_runtime_dir = os.environ.get("ROUTER_SCAN_RUNTIME_DIR", _deployment.get("runtime_dir", "runtime"))
_runtime_path = Path(_runtime_dir)
if not _runtime_path.is_absolute():
    _runtime_path = CONFIG_DIR / _runtime_path
_runtime_path.mkdir(parents=True, exist_ok=True)
STATE_FILE = _runtime_path / STATE_FILE.name
EVENT_LOG = _runtime_path / EVENT_LOG.name
TARGET_HISTORY_FILE = _runtime_path / TARGET_HISTORY_FILE.name
STATUS_LOG = _runtime_path / STATUS_LOG.name
COOKIE_FILE = CONFIG_DIR / COOKIE_FILE.name

# Router login
ROUTER_USERNAME = ""
ROUTER_PASSWORD = ""

# Router fetch config
EXTRA_HEADERS = {}
SAVE_RAW = False

# Scanning behavior
WATCH = True
INTERVAL = 5
DYNAMIC_INTERVAL_MIN_SECONDS = 2
DYNAMIC_INTERVAL_MAX_SECONDS = 5

# Targets
DEFAULT_TARGET_MAC = ""
DEFAULT_TARGET_IP = ""
DEFAULT_TARGET_NAME_CONTAINS = ""

TARGET_MAC = DEFAULT_TARGET_MAC
TARGET_IP = DEFAULT_TARGET_IP
TARGET_NAME_CONTAINS = DEFAULT_TARGET_NAME_CONTAINS
SECOND_TARGET_MAC = ""
SECOND_TARGET_IP = ""
SECOND_TARGET_NAME_CONTAINS = ""

# RSSI stale / traffic review
RSSI_STABLE_WINDOW_SECONDS = 30
RSSI_STABLE_MIN_SAMPLES = 5
TRAFFIC_IDLE_WINDOW_SECONDS = 30
TRAFFIC_ACTIVE_MIN_BPS = 0

# History/web
TARGET_HISTORY_RETENTION_DAYS = 30
if ROUTER_BACKEND == "openwrt" or _runtime_dir:
    TARGET_HISTORY_RETENTION_DAYS = 1
HISTORY_RECENT_DAYS = 1
HISTORY_MAX_SNAPSHOT = 0
HISTORY_SAVE_INTERVAL_SECONDS = 10
ENABLE_HISTORY_WEB = True
HISTORY_WEB_PORT = int(os.environ.get("ROUTER_SCAN_WEB_PORT", _deployment.get("web_port", 30707)))
if not 1 <= HISTORY_WEB_PORT <= 65535:
    raise ValueError("网页端口必须在 1 到 65535 之间")
HISTORY_MAX_ENTRIES = int(_deployment.get("history_max_entries", 5000))
if HISTORY_MAX_ENTRIES < 1:
    raise ValueError("history_max_entries 必须为正整数")

_apply_monitor_settings(_load_monitor_settings())
