from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional

from . import config
from .logger import append_event_log
from .utils import parse_rssi_value, warn

Device = Dict[str, str]
Matcher = Callable[[Device], bool]


@dataclass(frozen=True)
class TargetSpec:
    """A configured device target.

    ``key`` is stable and is used for runtime state and API data.  Labels are kept
    here instead of being repeated throughout the monitoring loop.
    """

    key: str
    label: str
    mac: str
    name_contains: str

    @property
    def configured(self) -> bool:
        return bool(self.mac.strip() or self.name_contains.strip())

    def matches(self, device: Device) -> bool:
        return match_device_config(device, self.mac, self.name_contains)


def configured_targets() -> List[TargetSpec]:
    targets = [
        TargetSpec("primary", "设备一", config.TARGET_MAC, config.TARGET_NAME_CONTAINS),
        TargetSpec(
            "secondary",
            "设备二",
            config.SECOND_TARGET_MAC,
            config.SECOND_TARGET_NAME_CONTAINS,
        ),
    ]
    return [target for target in targets if target.configured]


def match_device_config(dev: Device, mac: str, name_contains: str) -> bool:
    expected_mac = mac.strip().upper()
    expected_name = name_contains.strip().casefold()
    actual_mac = str(dev.get("mac", "")).strip().upper()
    actual_name = str(dev.get("name", "")).casefold()
    return bool(
        (expected_mac and actual_mac == expected_mac)
        or (expected_name and expected_name in actual_name)
    )


def match_primary(dev: Device) -> bool:
    return match_device_config(dev, config.TARGET_MAC, config.TARGET_NAME_CONTAINS)


def match_secondary(dev: Device) -> bool:
    return match_device_config(
        dev, config.SECOND_TARGET_MAC, config.SECOND_TARGET_NAME_CONTAINS
    )


def has_primary() -> bool:
    return TargetSpec("primary", "设备一", config.TARGET_MAC, config.TARGET_NAME_CONTAINS).configured


def has_secondary() -> bool:
    return TargetSpec(
        "secondary", "设备二", config.SECOND_TARGET_MAC, config.SECOND_TARGET_NAME_CONTAINS
    ).configured


def find_target(devices: Iterable[Device], target: TargetSpec) -> Optional[Device]:
    return next((device for device in devices if target.matches(device)), None)


def check_target(
    now_list: List[Device],
    last_state: Optional[bool],
    matcher: Matcher,
    label: str,
) -> bool:
    target_info = next((device for device in now_list if matcher(device)), None)
    present = target_info is not None

    if last_state is None:
        if target_info:
            warn(f"[{label}] 初始在线，RSSI={target_info.get('rssi', '')}")
        else:
            print(f"[INFO] {label} 初始离线")
        return present

    if present != last_state:
        status = "上线" if present else "下线"
        rssi_info = f"，RSSI={target_info.get('rssi', '')}" if target_info else ""
        line = f"[{label}] {status}{rssi_info}"
        warn(line)
        append_event_log(line)
    return present


def is_device_online(device: Optional[Device]) -> bool:
    """Apply the configured RSSI rule to one discovered device."""

    if not device:
        return False
    rssi_value = parse_rssi_value(device.get("rssi", ""))
    return rssi_value is not None and rssi_value < config.RSSI_THRESHOLD


def is_target_online(devices: List[Device], matcher: Matcher = match_primary) -> bool:
    return is_device_online(next((device for device in devices if matcher(device)), None))


def render_target_status(
    now_list: List[Device], arping_results: Optional[Dict[str, Optional[bool]]] = None
) -> None:
    targets = configured_targets()
    if not targets:
        print("\n[目标设备] 未配置")
        return
    results = arping_results or {}
    for target in targets:
        _render_single(now_list, target.matches, target.label, results.get(target.key))


def _render_single(
    now_list: List[Device], matcher: Matcher, label: str, arping_ok: Optional[bool]
) -> None:
    info = next((device for device in now_list if matcher(device)), None)
    if not info:
        print(f"\n[{label}] 离线 ✖")
        return
    arping_hint = ""
    if arping_ok is True:
        arping_hint = " (ARP: up)"
    elif arping_ok is False:
        arping_hint = " (ARP: down)"
    print(
        f"\n[{label}] 在线 ✔{arping_hint}  IP={info.get('ip', '')}  "
        f"MAC={info.get('mac', '')}  RSSI={info.get('rssi', '')}"
    )
