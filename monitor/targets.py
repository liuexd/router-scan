from typing import Dict, List, Optional

from . import config
from .logger import append_event_log
from .utils import warn


def match_device_config(dev: Dict[str, str], mac: str, name_contains: str) -> bool:
    mac = mac.strip().upper()
    name_contains = name_contains.strip().lower()
    if mac and dev.get("mac", "").upper() == mac:
        return True
    if name_contains and name_contains in dev.get("name", "").lower():
        return True
    return False


def match_primary(dev: Dict[str, str]) -> bool:
    return match_device_config(dev, config.TARGET_MAC, config.TARGET_NAME_CONTAINS)


def match_secondary(dev: Dict[str, str]) -> bool:
    return match_device_config(dev, config.SECOND_TARGET_MAC, config.SECOND_TARGET_NAME_CONTAINS)


def has_primary() -> bool:
    return any(value.strip() for value in (config.TARGET_MAC, config.TARGET_NAME_CONTAINS))


def has_secondary() -> bool:
    return any(value.strip() for value in (config.SECOND_TARGET_MAC, config.SECOND_TARGET_NAME_CONTAINS))


def check_target(
    now_list: List[Dict[str, str]],
    last_state: Optional[bool],
    matcher,
    label: str,
    *,
    online_override: Optional[bool] = None,
    target_info_override: Optional[Dict[str, str]] = None,
) -> Optional[bool]:
    if online_override is None:
        online = False
        target_info = None
        for device in now_list:
            if matcher(device):
                online = True
                target_info = device
                break
    else:
        online = bool(online_override)
        target_info = target_info_override

    if last_state is None:
        if online:
            rssi_info = f"，RSSI={target_info.get('rssi', '')}" if target_info else ""
            warn(f"[{label}] 初始在线{rssi_info}")
        else:
            print(f"[INFO] {label} 初始离线")
        return online

    if online != last_state:
        status = "上线" if online else "下线"
        rssi_info = f"，RSSI={target_info.get('rssi', '')}" if (online and target_info) else ""
        line = f"[{label}] {status}{rssi_info}"
        warn(line)
        append_event_log(line)
    return online


def render_target_status(now_list: List[Dict[str, str]], primary_status: Optional[Dict[str, object]] = None) -> None:
    rendered = False
    if has_primary():
        _render_single(now_list, match_primary, "目标设备", primary_status)
        rendered = True
    if has_secondary():
        _render_single(now_list, match_secondary, "第二目标设备", None)
        rendered = True
    if not rendered:
        print("\n[目标设备] 未配置")


def _render_single(
    now_list: List[Dict[str, str]],
    matcher,
    label: str,
    primary_status: Optional[Dict[str, object]],
) -> None:
    info = next((device for device in now_list if matcher(device)), None)
    if label == "目标设备" and primary_status is not None:
        status_text = "在线" if primary_status.get("online") else "离线"
        summary = str(primary_status.get("summary") or "")
        if info:
            print(
                f"\n[{label}] {status_text}  IP={info.get('ip', '')}  MAC={info.get('mac', '')}  "
                f"RSSI={info.get('rssi', '')}  判定={summary}"
            )
        else:
            print(f"\n[{label}] {status_text}  判定={summary}")
        return
    if info:
        print(f"\n[{label}] 在线  IP={info.get('ip', '')}  MAC={info.get('mac', '')}  RSSI={info.get('rssi', '')}")
    else:
        print(f"\n[{label}] 离线")
