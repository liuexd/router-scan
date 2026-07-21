import time
from typing import Dict, Optional

from . import config
from .activity_probe import ProbeState, evaluate_primary_status
from .fetcher import load_state, read_cookie_from_file, scan_once
from .history import record_history, start_server, stop_server
from .logger import log_program_exit
from .notifications import notify_status_change
from .targets import (
    check_target,
    has_primary,
    has_secondary,
    match_primary,
    match_secondary,
    render_target_status,
)
from .utils import print_table, ts


FAST_SCAN_REASONS = {"traffic_idle", "traffic_missing"}


def _compute_dynamic_interval(primary_reason: Optional[str]) -> float:
    fast_interval = float(getattr(config, "DYNAMIC_INTERVAL_MIN_SECONDS", 2))
    slow_interval = float(getattr(config, "DYNAMIC_INTERVAL_MAX_SECONDS", 5))
    if slow_interval < fast_interval:
        slow_interval = fast_interval
    if primary_reason in FAST_SCAN_REASONS:
        return fast_interval
    return slow_interval


def main() -> None:
    prev_state = load_state()
    cookie = read_cookie_from_file()
    target_last: Optional[bool] = None
    second_target_last: Optional[bool] = None
    target_online_last: Optional[bool] = None
    primary_probe_state = ProbeState()

    if config.ENABLE_HISTORY_WEB:
        start_server()

    try:
        if config.WATCH:
            print(
                f"[INFO] 持续扫描已开启，正常状态 {config.DYNAMIC_INTERVAL_MAX_SECONDS}s，"
                f"疑似在线 {config.DYNAMIC_INTERVAL_MIN_SECONDS}s。按 Ctrl+C 停止。"
            )
            while True:
                loop_start = time.monotonic()
                (
                    cookie,
                    target_last,
                    second_target_last,
                    target_online_last,
                    current_online,
                    primary_reason,
                ) = _run_once(
                    prev_state,
                    cookie,
                    target_last,
                    second_target_last,
                    target_online_last,
                    primary_probe_state,
                )
                now = time.monotonic()
                interval = _compute_dynamic_interval(primary_reason)
                sleep_for = max(0.0, interval - (now - loop_start))
                time.sleep(sleep_for)
        else:
            _run_once(
                prev_state,
                cookie,
                target_last,
                second_target_last,
                target_online_last,
                primary_probe_state,
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
    primary_probe_state: ProbeState,
    *,
    single_run: bool = False,
) -> tuple[str, Optional[bool], Optional[bool], Optional[bool], Optional[bool], Optional[str]]:
    now_list, new_list, gone_list, cookie = scan_once(prev_state, cookie)
    print(f"\n[{ts()}] 扫描结果：{config.URL}")
    print_table("当前在线设备", now_list)
    print_table("新上线", new_list)
    print_table("下线", gone_list)

    current_online: Optional[bool] = None
    primary_reason: Optional[str] = None
    primary_status_display: Optional[dict] = None

    if has_primary():
        target_device = next((d for d in now_list if match_primary(d)), None)
        target_last = check_target(now_list, target_last, matcher=match_primary, label="目标设备")
        primary_status = evaluate_primary_status(target_device, primary_probe_state)
        current_online = primary_status.online
        primary_reason = primary_status.reason
        primary_status_display = {"online": current_online, "summary": primary_status.summary}
        prev_online = target_online_last
        if prev_online is None:
            target_online_last = current_online
        elif current_online != prev_online:
            notify_status_change(current_online)
            target_online_last = current_online
        record_history("目标设备", current_online, target_device, review=primary_status.to_history_fields())

    if has_secondary():
        second_device = next((d for d in now_list if match_secondary(d)), None)
        second_target_last = check_target(
            now_list,
            second_target_last,
            matcher=match_secondary,
            label="第二目标设备",
        )
        record_history("第二目标设备", bool(second_target_last), second_device)

    render_target_status(now_list, primary_status_display)

    if single_run:
        log_program_exit("单次扫描完成", notify=False)

    return (
        cookie,
        target_last,
        second_target_last,
        target_online_last,
        current_online,
        primary_reason,
    )
