from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import config
from .fetcher import load_state, read_cookie_from_file, scan_once
from .history import record_history, start_server, stop_server
from .logger import log_program_exit
from .notifications import notify_arping_down, notify_status_change
from .targets import TargetSpec, check_target, configured_targets, find_target, is_device_online, render_target_status
from .utils import print_table, ts


@dataclass
class TargetRuntime:
    last_presence: Optional[bool] = None
    last_online: Optional[bool] = None
    arping_down_streak: int = 0
    arping_alert_level: int = 0


def _compute_dynamic_interval(
    current_online: Optional[bool],
    down_event: bool,
    down_events: List[float],
    now: float,
) -> float:
    min_interval = float(getattr(config, "DYNAMIC_INTERVAL_MIN_SECONDS", config.INTERVAL))
    max_interval = max(
        min_interval,
        float(getattr(config, "DYNAMIC_INTERVAL_MAX_SECONDS", min_interval)),
    )
    if down_event:
        down_events.append(now)
    cutoff = now - max(1, int(getattr(config, "DYNAMIC_INTERVAL_WINDOW_SECONDS", 60)))
    down_events[:] = [event_time for event_time in down_events if event_time >= cutoff]
    if current_online is None:
        return float(config.INTERVAL)
    if not current_online:
        return min_interval
    max_downs = max(1, int(getattr(config, "DYNAMIC_INTERVAL_MAX_DOWNS", 5)))
    ratio = min(len(down_events), max_downs) / max_downs
    return max_interval - (max_interval - min_interval) * ratio


def _target_hint(target: TargetSpec) -> str:
    return target.name_contains or target.mac or target.label


def _update_target(
    devices: List[Dict[str, str]],
    target: TargetSpec,
    runtime: TargetRuntime,
    arping_ok: Optional[bool],
) -> Tuple[bool, bool]:
    """Update one target and return ``(online, went_down)``."""

    device = find_target(devices, target)
    runtime.last_presence = check_target(
        devices,
        runtime.last_presence,
        matcher=target.matches,
        label=target.label,
    )
    online = is_device_online(device)
    went_down = runtime.last_online is True and not online
    if runtime.last_online is not None and online != runtime.last_online:
        notify_status_change(online, _target_hint(target))
    runtime.last_online = online

    if online and arping_ok is False:
        runtime.arping_down_streak += 1
    else:
        runtime.arping_down_streak = 0
        runtime.arping_alert_level = 0

    if runtime.arping_down_streak >= config.ARPING_EMERGENCY_THRESHOLD:
        threshold = config.ARPING_EMERGENCY_THRESHOLD
        if runtime.arping_alert_level < threshold:
            notify_arping_down("emergency", runtime.arping_down_streak, _target_hint(target))
            runtime.arping_alert_level = threshold
    elif runtime.arping_down_streak >= config.ARPING_WARN_THRESHOLD:
        threshold = config.ARPING_WARN_THRESHOLD
        if runtime.arping_alert_level < threshold:
            notify_arping_down("warn", runtime.arping_down_streak, _target_hint(target))
            runtime.arping_alert_level = threshold

    record_history(target.label, online, device, arping_ok=arping_ok)
    return online, went_down


def _run_once(
    prev_state: Dict[str, Dict],
    cookie: str,
    runtimes: Dict[str, TargetRuntime],
    *,
    single_run: bool = False,
) -> Tuple[str, Optional[bool], bool]:
    devices, new_devices, gone_devices, cookie, arping_results = scan_once(prev_state, cookie)
    print(f"\n[{ts()}] 扫描结果：{config.URL}")
    print_table("当前在线设备", devices)
    print_table("新上线", new_devices)
    print_table("下线", gone_devices)
    render_target_status(devices, arping_results)

    statuses: List[bool] = []
    down_event = False
    for target in configured_targets():
        runtime = runtimes.setdefault(target.key, TargetRuntime())
        online, went_down = _update_target(
            devices, target, runtime, arping_results.get(target.key)
        )
        statuses.append(online)
        down_event = down_event or went_down

    if single_run:
        log_program_exit("单次扫描完成", notify=False)
    return cookie, (all(statuses) if statuses else None), down_event


def main() -> None:
    previous_state = load_state()
    cookie = read_cookie_from_file()
    runtimes: Dict[str, TargetRuntime] = {}
    down_events: List[float] = []

    if config.ENABLE_HISTORY_WEB:
        start_server()

    try:
        if not config.WATCH:
            _run_once(previous_state, cookie, runtimes, single_run=True)
            return

        print(
            f"[INFO] 持续扫描已开启，扫描间隔 "
            f"{config.DYNAMIC_INTERVAL_MIN_SECONDS}-{config.DYNAMIC_INTERVAL_MAX_SECONDS}s 动态调整。"
            "按 Ctrl+C 停止。"
        )
        while True:
            loop_start = time.monotonic()
            cookie, aggregate_online, down_event = _run_once(
                previous_state, cookie, runtimes
            )
            now = time.monotonic()
            interval = _compute_dynamic_interval(
                aggregate_online, down_event, down_events, now
            )
            time.sleep(max(0.0, interval - (now - loop_start)))
    except KeyboardInterrupt:
        print("\n[INFO] 已停止。")
        log_program_exit("用户终止", notify=True)
    except Exception as exc:
        log_program_exit(f"异常退出：{exc}", notify=True)
        raise
    finally:
        stop_server()
