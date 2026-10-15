from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import config
from .utils import parse_rssi_value


@dataclass
class PrimaryStatus:
    online: bool
    present: bool
    reason: str
    summary: str
    evidence: str = ""
    rssi_value: Optional[int] = None
    snr_value: Optional[int] = None
    rssi_stable: bool = False
    snr_stable: bool = False
    traffic_active: bool = False
    traffic_window_seconds: int = 0
    tx_peak_bps: Optional[float] = None
    rx_peak_bps: Optional[float] = None
    no_traffic_samples: int = 0
    no_traffic_limit: int = 0
    missing_samples: int = 0
    missing_tolerance: int = 0
    same_rssi_samples: int = 0

    def to_history_fields(self) -> Dict[str, object]:
        return {
            "decision": self.reason,
            "decision_summary": self.summary,
            "decision_evidence": self.evidence,
            "rssi_stable": self.rssi_stable,
            "snr_stable": self.snr_stable,
            "traffic_active": self.traffic_active,
            "traffic_window_seconds": self.traffic_window_seconds,
            "tx_peak_bps": round(self.tx_peak_bps, 2) if self.tx_peak_bps is not None else None,
            "rx_peak_bps": round(self.rx_peak_bps, 2) if self.rx_peak_bps is not None else None,
            "no_traffic_samples": self.no_traffic_samples,
            "no_traffic_limit": self.no_traffic_limit,
            "missing_samples": self.missing_samples,
            "missing_tolerance": self.missing_tolerance,
            "same_rssi_samples": self.same_rssi_samples,
        }


@dataclass
class ProbeState:
    traffic_samples: List[tuple[float, Optional[float], Optional[float]]] = field(default_factory=list)
    last_status: Optional[PrimaryStatus] = None
    last_device: Optional[Dict[str, str]] = None
    no_traffic_samples: int = 0
    missing_samples: int = 0
    last_rssi_value: Optional[int] = None
    same_rssi_samples: int = 0


def evaluate_primary_status(device: Optional[Dict[str, str]], probe_state: ProbeState) -> PrimaryStatus:
    if not device:
        return _handle_missing_device(probe_state)

    probe_state.missing_samples = 0
    probe_state.last_device = dict(device)

    rssi_value = parse_rssi_value(device.get("rssi", ""))
    snr_value = _parse_snr_value(device.get("snr", ""))
    if rssi_value is None:
        _clear_signal_tracking(probe_state)
        return _remember_status(
            probe_state,
            PrimaryStatus(
                online=False,
                present=True,
                reason="rssi_missing",
                summary="RSSI 无法解析，按离线处理",
                evidence="RSSI 无法解析",
                snr_value=snr_value,
            ),
        )

    if rssi_value >= config.RSSI_THRESHOLD:
        _clear_signal_tracking(probe_state)
        return _remember_status(
            probe_state,
            PrimaryStatus(
                online=False,
                present=True,
                reason="rssi_threshold",
                summary=f"RSSI={rssi_value} dBm，未低于阈值 {config.RSSI_THRESHOLD}",
                evidence=f"RSSI={rssi_value} dBm 未低于阈值 {config.RSSI_THRESHOLD}",
                rssi_value=rssi_value,
                snr_value=snr_value,
            ),
        )

    same_rssi_samples = _update_rssi_tracking(probe_state, rssi_value)
    tx_bps = _parse_rate_bps(device.get("tx_rate_bps", ""))
    rx_bps = _parse_rate_bps(device.get("rx_rate_bps", ""))
    if config.ROUTER_BACKEND == "openwrt" and tx_bps is None and rx_bps is None:
        probe_state.no_traffic_samples = 0
        return _remember_status(
            probe_state,
            PrimaryStatus(
                online=True, present=True, reason="associated",
                summary="设备已关联；驱动未提供实际流量统计",
                evidence=f"RSSI={rssi_value} dBm；依据无线关联和信号判断",
                rssi_value=rssi_value, snr_value=snr_value,
                same_rssi_samples=same_rssi_samples,
            ),
        )
    has_traffic = _has_traffic_activity(tx_bps, rx_bps)
    no_traffic_limit = config.NO_TRAFFIC_SAMPLE_LIMIT
    missing_tolerance = config.DEVICE_ABSENCE_TOLERANCE

    if has_traffic:
        probe_state.no_traffic_samples = 0
        _reset_traffic_samples(probe_state, tx_bps, rx_bps)
        return _remember_status(
            probe_state,
            PrimaryStatus(
                online=True,
                present=True,
                reason="traffic_active",
                summary="检测到数据传输，设备在线",
                evidence=f"RSSI={rssi_value} dBm，Tx={_format_rate(tx_bps)}，Rx={_format_rate(rx_bps)}",
                rssi_value=rssi_value,
                snr_value=snr_value,
                rssi_stable=same_rssi_samples >= 2,
                traffic_active=True,
                tx_peak_bps=tx_bps,
                rx_peak_bps=rx_bps,
                no_traffic_limit=no_traffic_limit,
                missing_tolerance=missing_tolerance,
                same_rssi_samples=same_rssi_samples,
            ),
        )

    _append_traffic_sample(probe_state, tx_bps, rx_bps)
    tx_peak_bps, rx_peak_bps = _traffic_peaks(probe_state)
    traffic_desc = (
        "本次未提供上传/下载速率"
        if tx_bps is None and rx_bps is None
        else f"Tx={_format_rate(tx_bps)}，Rx={_format_rate(rx_bps)}"
    )

    if same_rssi_samples < 2:
        probe_state.no_traffic_samples = 0
        return _remember_status(
            probe_state,
            PrimaryStatus(
                online=True,
                present=True,
                reason="traffic_waiting_rssi_change",
                summary="RSSI 仍在变化，暂不开始疑似下线计数",
                evidence=f"RSSI={rssi_value} dBm；{traffic_desc}；需要 RSSI 连续不变后才开始计数",
                rssi_value=rssi_value,
                snr_value=snr_value,
                rssi_stable=False,
                traffic_active=False,
                tx_peak_bps=tx_peak_bps,
                rx_peak_bps=rx_peak_bps,
                no_traffic_limit=no_traffic_limit,
                missing_tolerance=missing_tolerance,
                same_rssi_samples=same_rssi_samples,
            ),
        )

    probe_state.no_traffic_samples += 1

    if probe_state.no_traffic_samples >= no_traffic_limit:
        return _remember_status(
            probe_state,
            PrimaryStatus(
                online=False,
                present=True,
                reason="traffic_idle",
                summary=f"RSSI 连续不变且连续 {probe_state.no_traffic_samples} 次未检测到数据传输，判定为疑似下线",
                evidence=(
                    f"RSSI={rssi_value} dBm 连续不变 {same_rssi_samples} 次；"
                    f"{traffic_desc}；连续 {probe_state.no_traffic_samples} 次未检测到数据传输"
                ),
                rssi_value=rssi_value,
                snr_value=snr_value,
                rssi_stable=True,
                traffic_active=False,
                tx_peak_bps=tx_peak_bps,
                rx_peak_bps=rx_peak_bps,
                no_traffic_samples=probe_state.no_traffic_samples,
                no_traffic_limit=no_traffic_limit,
                missing_tolerance=missing_tolerance,
                same_rssi_samples=same_rssi_samples,
            ),
        )

    return _remember_status(
        probe_state,
        PrimaryStatus(
            online=True,
            present=True,
            reason="traffic_waiting",
            summary=(
                f"RSSI 连续不变，疑似下线计数 {probe_state.no_traffic_samples}/{no_traffic_limit}"
            ),
            evidence=(
                f"RSSI={rssi_value} dBm 连续不变 {same_rssi_samples} 次；"
                f"{traffic_desc}；达到 {no_traffic_limit} 次后提醒疑似下线"
            ),
            rssi_value=rssi_value,
            snr_value=snr_value,
            rssi_stable=True,
            traffic_active=False,
            tx_peak_bps=tx_peak_bps,
            rx_peak_bps=rx_peak_bps,
            no_traffic_samples=probe_state.no_traffic_samples,
            no_traffic_limit=no_traffic_limit,
            missing_tolerance=missing_tolerance,
            same_rssi_samples=same_rssi_samples,
        ),
    )


def _handle_missing_device(probe_state: ProbeState) -> PrimaryStatus:
    probe_state.missing_samples += 1
    _clear_signal_tracking(probe_state)
    missing_tolerance = config.DEVICE_ABSENCE_TOLERANCE
    offline_limit = missing_tolerance + 1
    last_status = probe_state.last_status

    if last_status is not None and probe_state.missing_samples <= missing_tolerance:
        return PrimaryStatus(
            online=last_status.online,
            present=last_status.present,
            reason="not_present_tolerated",
            summary=(
                f"本次未出现在设备列表，已过滤第 {probe_state.missing_samples}/{missing_tolerance} 次漏采样"
            ),
            evidence=f"连续未在设备列表中出现 {probe_state.missing_samples} 次，未达到离线阈值 {offline_limit} 次",
            rssi_value=last_status.rssi_value,
            snr_value=last_status.snr_value,
            rssi_stable=last_status.rssi_stable,
            traffic_active=last_status.traffic_active,
            tx_peak_bps=last_status.tx_peak_bps,
            rx_peak_bps=last_status.rx_peak_bps,
            no_traffic_samples=last_status.no_traffic_samples,
            no_traffic_limit=last_status.no_traffic_limit or config.NO_TRAFFIC_SAMPLE_LIMIT,
            missing_samples=probe_state.missing_samples,
            missing_tolerance=missing_tolerance,
            same_rssi_samples=last_status.same_rssi_samples,
        )

    return _remember_status(
        probe_state,
        PrimaryStatus(
            online=False,
            present=False,
            reason="not_present",
            summary=f"连续 {probe_state.missing_samples} 次未出现在设备列表，判定离线",
            evidence=f"设备连续 {probe_state.missing_samples} 次未出现在路由器设备列表中",
            missing_samples=probe_state.missing_samples,
            missing_tolerance=missing_tolerance,
        ),
    )


def _remember_status(probe_state: ProbeState, status: PrimaryStatus) -> PrimaryStatus:
    probe_state.last_status = status
    return status


def _clear_signal_tracking(probe_state: ProbeState) -> None:
    probe_state.traffic_samples.clear()
    probe_state.no_traffic_samples = 0
    probe_state.last_rssi_value = None
    probe_state.same_rssi_samples = 0


def _update_rssi_tracking(probe_state: ProbeState, rssi_value: int) -> int:
    if probe_state.last_rssi_value == rssi_value:
        probe_state.same_rssi_samples += 1
    else:
        probe_state.same_rssi_samples = 1
        probe_state.no_traffic_samples = 0
    probe_state.last_rssi_value = rssi_value
    return probe_state.same_rssi_samples


def _reset_traffic_samples(
    probe_state: ProbeState,
    tx_bps: Optional[float],
    rx_bps: Optional[float],
) -> None:
    probe_state.traffic_samples[:] = [(time.monotonic(), tx_bps, rx_bps)]


def _append_traffic_sample(
    probe_state: ProbeState,
    tx_bps: Optional[float],
    rx_bps: Optional[float],
) -> None:
    probe_state.traffic_samples.append((time.monotonic(), tx_bps, rx_bps))
    keep = max(1, int(config.NO_TRAFFIC_SAMPLE_LIMIT))
    probe_state.traffic_samples[:] = probe_state.traffic_samples[-keep:]


def _traffic_peaks(probe_state: ProbeState) -> tuple[Optional[float], Optional[float]]:
    if not probe_state.traffic_samples:
        return None, None
    tx_values = [(tx_bps or 0.0) for _, tx_bps, _ in probe_state.traffic_samples]
    rx_values = [(rx_bps or 0.0) for _, _, rx_bps in probe_state.traffic_samples]
    return max(tx_values) if tx_values else None, max(rx_values) if rx_values else None


def _parse_rate_bps(value: str) -> Optional[float]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except Exception:
        return None


def _parse_snr_value(value: str) -> Optional[int]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except Exception:
        return None


def _has_traffic_activity(tx_bps: Optional[float], rx_bps: Optional[float]) -> bool:
    threshold = float(config.TRAFFIC_ACTIVE_MIN_BPS)
    return (tx_bps or 0.0) > threshold or (rx_bps or 0.0) > threshold


def _format_rate(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f} MB/s"
    if value >= 1_000:
        return f"{value / 1_000:.2f} KB/s"
    return f"{value:.0f} B/s"
