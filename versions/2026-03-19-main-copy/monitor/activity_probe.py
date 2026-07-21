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
        }


@dataclass
class ProbeState:
    rssi_samples: List[tuple[float, int]] = field(default_factory=list)
    snr_samples: List[tuple[float, int]] = field(default_factory=list)
    traffic_samples: List[tuple[float, Optional[float], Optional[float]]] = field(default_factory=list)
    last_status: Optional[PrimaryStatus] = None


def evaluate_primary_status(device: Optional[Dict[str, str]], probe_state: ProbeState) -> PrimaryStatus:
    now = time.monotonic()
    if not device:
        _reset_probe_state(probe_state)
        return PrimaryStatus(
            online=False,
            present=False,
            reason="not_present",
            summary="未出现在路由器设备列表中",
            evidence="未出现在路由器设备列表中",
        )

    rssi_value = parse_rssi_value(device.get("rssi", ""))
    snr_value = _parse_snr_value(device.get("snr", ""))
    if rssi_value is None:
        _reset_probe_state(probe_state)
        return PrimaryStatus(
            online=False,
            present=True,
            reason="rssi_missing",
            summary="RSSI 无法解析，按离线处理",
            evidence="RSSI 无法解析",
            snr_value=snr_value,
        )

    if rssi_value >= config.RSSI_THRESHOLD:
        _reset_probe_state(probe_state)
        return PrimaryStatus(
            online=False,
            present=True,
            reason="rssi_threshold",
            summary=f"RSSI={rssi_value} dBm，未低于阈值 {config.RSSI_THRESHOLD}",
            evidence=f"RSSI={rssi_value} dBm 未低于阈值 {config.RSSI_THRESHOLD}",
            rssi_value=rssi_value,
            snr_value=snr_value,
        )

    _append_rssi_sample(probe_state, now, rssi_value)
    if snr_value is not None:
        _append_snr_sample(probe_state, now, snr_value)
    tx_bps = _parse_rate_bps(device.get("tx_rate_bps", ""))
    rx_bps = _parse_rate_bps(device.get("rx_rate_bps", ""))
    _append_traffic_sample(probe_state, now, tx_bps, rx_bps)

    rssi_stable = _is_rssi_stable(probe_state, now)
    snr_stable = _is_snr_stable(probe_state, now)

    if not rssi_stable or (snr_value is not None and not snr_stable):
        evidence_parts = [f"RSSI={rssi_value} dBm 低于阈值 {config.RSSI_THRESHOLD}"]
        if not rssi_stable:
            evidence_parts.append("RSSI 存在自然波动")
        if snr_value is not None:
            evidence_parts.append(f"SNR={snr_value}")
            evidence_parts.append("SNR 存在自然波动" if not snr_stable else "SNR 长时间不变")
        status = PrimaryStatus(
            online=True,
            present=True,
            reason="signal_fluctuating",
            summary=f"RSSI={rssi_value} dBm，存在自然波动或 SNR 提供在线佐证",
            evidence="；".join(evidence_parts),
            rssi_value=rssi_value,
            snr_value=snr_value,
            rssi_stable=rssi_stable,
            snr_stable=snr_stable,
        )
        probe_state.last_status = status
        return status

    traffic_stats = _traffic_stats(probe_state, now)
    if traffic_stats["has_activity"]:
        evidence = (
            f"RSSI={rssi_value} dBm 与 SNR={snr_value if snr_value is not None else 'n/a'} 长时间不变；"
            f"近{config.TRAFFIC_IDLE_WINDOW_SECONDS}秒检测到上传/下载活动"
        )
        status = PrimaryStatus(
            online=True,
            present=True,
            reason="traffic_active",
            summary=(
                f"RSSI/SNR 长时间不变，但近{config.TRAFFIC_IDLE_WINDOW_SECONDS}秒检测到"
                f"上传/下载活动 (Tx峰值={_format_rate(traffic_stats['tx_peak_bps'])}, "
                f"Rx峰值={_format_rate(traffic_stats['rx_peak_bps'])})"
            ),
            evidence=(
                f"{evidence}；Tx峰值={_format_rate(traffic_stats['tx_peak_bps'])}；"
                f"Rx峰值={_format_rate(traffic_stats['rx_peak_bps'])}"
            ),
            rssi_value=rssi_value,
            snr_value=snr_value,
            rssi_stable=True,
            snr_stable=snr_stable,
            traffic_active=True,
            traffic_window_seconds=config.TRAFFIC_IDLE_WINDOW_SECONDS,
            tx_peak_bps=traffic_stats["tx_peak_bps"],
            rx_peak_bps=traffic_stats["rx_peak_bps"],
        )
        probe_state.last_status = status
        return status

    if traffic_stats["has_observation"]:
        status = PrimaryStatus(
            online=False,
            present=True,
            reason="traffic_idle",
            summary=(
                f"RSSI/SNR 长时间不变，且近{config.TRAFFIC_IDLE_WINDOW_SECONDS}秒"
                "无上传/下载活动，判定为疑似在线"
            ),
            evidence=(
                f"RSSI={rssi_value} dBm 与 SNR={snr_value if snr_value is not None else 'n/a'} 长时间不变；"
                f"近{config.TRAFFIC_IDLE_WINDOW_SECONDS}秒无上传/下载活动"
            ),
            rssi_value=rssi_value,
            snr_value=snr_value,
            rssi_stable=True,
            snr_stable=snr_stable,
            traffic_active=False,
            traffic_window_seconds=config.TRAFFIC_IDLE_WINDOW_SECONDS,
            tx_peak_bps=traffic_stats["tx_peak_bps"],
            rx_peak_bps=traffic_stats["rx_peak_bps"],
        )
        probe_state.last_status = status
        return status

    status = PrimaryStatus(
        online=False,
        present=True,
        reason="traffic_missing",
        summary=(
            f"RSSI/SNR 长时间不变，但近{config.TRAFFIC_IDLE_WINDOW_SECONDS}秒"
            "缺少上传/下载速率数据，判定为疑似在线"
        ),
        evidence=(
            f"RSSI={rssi_value} dBm 与 SNR={snr_value if snr_value is not None else 'n/a'} 长时间不变；"
            "缺少上传/下载速率数据"
        ),
        rssi_value=rssi_value,
        snr_value=snr_value,
        rssi_stable=True,
        snr_stable=snr_stable,
        traffic_active=False,
        traffic_window_seconds=config.TRAFFIC_IDLE_WINDOW_SECONDS,
    )
    probe_state.last_status = status
    return status


def _reset_probe_state(probe_state: ProbeState) -> None:
    probe_state.rssi_samples.clear()
    probe_state.snr_samples.clear()
    probe_state.traffic_samples.clear()
    probe_state.last_status = None


def _append_rssi_sample(probe_state: ProbeState, now: float, rssi_value: int) -> None:
    probe_state.rssi_samples.append((now, rssi_value))
    cutoff = now - config.RSSI_STABLE_WINDOW_SECONDS
    probe_state.rssi_samples[:] = [(sample_ts, value) for sample_ts, value in probe_state.rssi_samples if sample_ts >= cutoff]


def _append_snr_sample(probe_state: ProbeState, now: float, snr_value: int) -> None:
    probe_state.snr_samples.append((now, snr_value))
    cutoff = now - config.RSSI_STABLE_WINDOW_SECONDS
    probe_state.snr_samples[:] = [(sample_ts, value) for sample_ts, value in probe_state.snr_samples if sample_ts >= cutoff]


def _append_traffic_sample(
    probe_state: ProbeState,
    now: float,
    tx_bps: Optional[float],
    rx_bps: Optional[float],
) -> None:
    probe_state.traffic_samples.append((now, tx_bps, rx_bps))
    cutoff = now - config.TRAFFIC_IDLE_WINDOW_SECONDS
    probe_state.traffic_samples[:] = [
        (sample_ts, tx_value, rx_value)
        for sample_ts, tx_value, rx_value in probe_state.traffic_samples
        if sample_ts >= cutoff
    ]


def _is_rssi_stable(probe_state: ProbeState, now: float) -> bool:
    samples = probe_state.rssi_samples
    if len(samples) < config.RSSI_STABLE_MIN_SAMPLES:
        return False
    if now - samples[0][0] < config.RSSI_STABLE_WINDOW_SECONDS:
        return False
    return len({value for _, value in samples}) == 1


def _is_snr_stable(probe_state: ProbeState, now: float) -> bool:
    samples = probe_state.snr_samples
    if not samples:
        return True
    if len(samples) < config.RSSI_STABLE_MIN_SAMPLES:
        return False
    if now - samples[0][0] < config.RSSI_STABLE_WINDOW_SECONDS:
        return False
    return len({value for _, value in samples}) == 1


def _traffic_stats(probe_state: ProbeState, now: float) -> Dict[str, object]:
    cutoff = now - config.TRAFFIC_IDLE_WINDOW_SECONDS
    samples = [(tx_bps, rx_bps) for sample_ts, tx_bps, rx_bps in probe_state.traffic_samples if sample_ts >= cutoff]
    observations = [(tx_bps, rx_bps) for tx_bps, rx_bps in samples if tx_bps is not None or rx_bps is not None]
    tx_peak = max((tx_bps or 0.0) for tx_bps, _ in observations) if observations else None
    rx_peak = max((rx_bps or 0.0) for _, rx_bps in observations) if observations else None
    active_threshold = float(config.TRAFFIC_ACTIVE_MIN_BPS)
    has_activity = any((tx_bps or 0.0) > active_threshold or (rx_bps or 0.0) > active_threshold for tx_bps, rx_bps in observations)
    return {
        "has_observation": bool(observations),
        "has_activity": has_activity,
        "tx_peak_bps": tx_peak,
        "rx_peak_bps": rx_peak,
    }


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


def _format_rate(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f} MB/s"
    if value >= 1_000:
        return f"{value / 1_000:.2f} KB/s"
    return f"{value:.0f} B/s"
