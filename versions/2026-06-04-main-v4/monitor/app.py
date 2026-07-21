import time
from typing import Dict, List, Optional

from . import config
from .fetcher import load_state, read_cookie_from_file, scan_once
from .history import record_history, start_server, stop_server
from .logger import log_program_exit
from .notifications import notify_arping_down, notify_status_change
from .targets import (
    check_target,
    has_primary,
    has_secondary,
    match_primary,
    match_secondary,
    render_target_status,
    is_target_online,
)
from .utils import print_table, ts


def _compute_dynamic_interval(
    current_online: Optional[bool],
    down_event: bool,
    down_events: List[float],
    now: float,
) -> float:
    min_interval = float(getattr(config, "DYNAMIC_INTERVAL_MIN_SECONDS", config.INTERVAL))
    max_interval = float(getattr(config, "DYNAMIC_INTERVAL_MAX_SECONDS", min_interval))
    if max_interval < min_interval:
        max_interval = min_interval
    if down_event:
        down_events.append(now)
    window_seconds = max(1, int(getattr(config, "DYNAMIC_INTERVAL_WINDOW_SECONDS", 60)))
    cutoff = now - window_seconds
    down_events[:] = [t for t in down_events if t >= cutoff]
    if current_online is None:
        return float(config.INTERVAL)
    if not current_online:
        return min_interval
    max_downs = max(1, int(getattr(config, "DYNAMIC_INTERVAL_MAX_DOWNS", 5)))
    ratio = min(len(down_events), max_downs) / max_downs
    interval = max_interval - (max_interval - min_interval) * ratio
    return max(min_interval, min(max_interval, interval))


def main() -> None:
    prev_state = load_state()
    cookie = read_cookie_from_file()
    target_last: Optional[bool] = None
    second_target_last: Optional[bool] = None
    target_online_last: Optional[bool] = None
    arping_down_streak = 0
    arping_alert_level = 0
    down_events: List[float] = []

    if config.ENABLE_HISTORY_WEB:
        start_server()

    try:
        if config.WATCH:
            print(
                f"[INFO] 持续扫描已开启，扫描间隔{config.DYNAMIC_INTERVAL_MIN_SECONDS}-"
                f"{config.DYNAMIC_INTERVAL_MAX_SECONDS}s 动态调整。按 Ctrl+C 停止。"
            )
            while True:
                loop_start = time.monotonic()
                (
                    cookie,
                    target_last,
                    second_target_last,
                    target_online_last,
                    arping_down_streak,
                    arping_alert_level,
                    current_online,
                    down_event,
                ) = _run_once(
                    prev_state,
                    cookie,
                    target_last,
                    second_target_last,
                    target_online_last,
                    arping_down_streak,
                    arping_alert_level,
                )
                now = time.monotonic()
                interval = _compute_dynamic_interval(current_online, down_event, down_events, now)
                sleep_for = max(0.0, interval - (now - loop_start))
                time.sleep(sleep_for)
        else:
            _run_once(
                prev_state,
                cookie,
                target_last,
                second_target_last,
                target_online_last,
                arping_down_streak,
                arping_alert_level,
                single_run=True,
            )
    except KeyboardInterrupt:
        print("\n[INFO] 已停止。")
        log_program_exit("用户终止", notify=True)
    except Exception as exc:
        log_program_exit(f"异常退出：{exc}", notify=True)
        raise
    finally:
        stop_server()


def _run_once(
    prev_state: Dict[str, Dict],
    cookie: str,
    target_last: Optional[bool],
    second_target_last: Optional[bool],
    target_online_last: Optional[bool],
    arping_down_streak: int,
    arping_alert_level: int,
    *,
    single_run: bool = False,
) -> tuple[str, Optional[bool], Optional[bool], Optional[bool], int, int, Optional[bool], bool]:
    now_list, new_list, gone_list, cookie, arping_ok = scan_once(prev_state, cookie)
    print(f"\n[{ts()}] 扫描结果：{config.URL}")
    print_table("当前在线设备", now_list)
    print_table("新上线", new_list)
    print_table("下线", gone_list)
    render_target_status(now_list, arping_ok)

    current_online: Optional[bool] = None
    down_event = False

    if has_primary():
        target_device = next((d for d in now_list if match_primary(d)), None)
        target_last = check_target(now_list, target_last, matcher=match_primary, label="目标设备")
        current_online = is_target_online(now_list)
        prev_online = target_online_last
        if prev_online is None:
            target_online_last = current_online
        elif current_online != prev_online:
            if prev_online and not current_online:
                down_event = True
            notify_status_change(current_online)
            target_online_last = current_online
        if current_online and arping_ok is False:
            arping_down_streak += 1
        else:
            arping_down_streak = 0
            arping_alert_level = 0
        if current_online and arping_ok is False:
            if arping_down_streak >= config.ARPING_EMERGENCY_THRESHOLD and arping_alert_level < config.ARPING_EMERGENCY_THRESHOLD:
                notify_arping_down("emergency", arping_down_streak)
                arping_alert_level = config.ARPING_EMERGENCY_THRESHOLD
            elif arping_down_streak >= config.ARPING_WARN_THRESHOLD and arping_alert_level < config.ARPING_WARN_THRESHOLD:
                notify_arping_down("warn", arping_down_streak)
                arping_alert_level = config.ARPING_WARN_THRESHOLD
        record_history("目标设备", current_online, target_device, arping_ok=arping_ok)

    if has_secondary():
        second_device = next((d for d in now_list if match_secondary(d)), None)
        second_target_last = check_target(
            now_list,
            second_target_last,
            matcher=match_secondary,
            label="第二目标设备",
        )
        record_history("第二目标设备", bool(second_target_last), second_device, arping_ok=arping_ok)

    if single_run:
        log_program_exit("单次扫描完成", notify=False)

    return (
        cookie,
        target_last,
        second_target_last,
        target_online_last,
        arping_down_streak,
        arping_alert_level,
        current_online,
        down_event,
    )
