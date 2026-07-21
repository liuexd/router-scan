from typing import Dict, List, Optional

from . import config
from .logger import append_event_log
from .utils import parse_rssi_value, warn


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
) -> Optional[bool]:
    online = None
    target_info = None
    for d in now_list:
        if matcher(d):
            online = True
            target_info = d
            break
    if online is None:
        online = False

    if last_state is None:
        if online and target_info:
            warn(f"[{label}] 初始在线，RSSI={target_info.get('rssi', '')}")
        else:
            print(f"[INFO] {label} 初始离线")
        return online

    if online != last_state:
        status = "上线" if online else "下线"
        rssi_info = f"，RSSI={target_info.get('rssi', '')}" if (online and target_info) else ""
        line = f"[{label}] {status}{rssi_info}"
        warn(line)
        append_event_log(f"[{label}] {status}{rssi_info}")
    return online


def is_target_online(devices: List[Dict[str, str]]) -> bool:
    target = next((device for device in devices if match_primary(device)), None)
    if not target:
        return False
    rssi_value = parse_rssi_value(target.get("rssi", ""))
    if rssi_value is None:
        return False
    return rssi_value < config.RSSI_THRESHOLD


def render_target_status(now_list: List[Dict[str, str]], arping_ok: Optional[bool] = None) -> None:
    rendered = False
    if has_primary():
        _render_single(now_list, match_primary, "目标设备", arping_ok)
        rendered = True
    if has_secondary():
        _render_single(now_list, match_secondary, "第二目标设备", arping_ok)
        rendered = True
    if not rendered:
        print("\n[目标设备] 未配置")


def _render_single(now_list: List[Dict[str, str]], matcher, label: str, arping_ok: Optional[bool]) -> None:
    info = next((d for d in now_list if matcher(d)), None)
    if info:
        arping_hint = ""
        if arping_ok is True:
            arping_hint = " (arping: up)"
        elif arping_ok is False:
            arping_hint = " (arping: down)"
        print(f"\n[{label}] 在线 ✓{arping_hint}  IP={info.get('ip', '')}  MAC={info.get('mac', '')}  RSSI={info.get('rssi', '')}")
    else:
        print(f"\n[{label}] 离线 ✗")
