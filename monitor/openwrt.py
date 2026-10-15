from __future__ import annotations
"""Read associated Wi-Fi stations locally; leases alone do not imply online."""
import json
import subprocess
import time
from pathlib import Path

_previous = {}


def _run(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=5, check=True)
    return result.stdout


def _leases():
    clients = {}
    path = Path("/tmp/dhcp.leases")
    if path.exists():
        for line in path.read_text().splitlines():
            fields = line.split()
            if len(fields) >= 4:
                clients[fields[1].upper()] = (fields[2], "" if fields[3] == "*" else fields[3])
    try:
        for line in _run("ip", "-4", "neigh", "show").splitlines():
            fields = line.split()
            if "lladdr" in fields:
                mac = fields[fields.index("lladdr") + 1].upper()
                clients.setdefault(mac, (fields[0], ""))
    except (OSError, subprocess.SubprocessError):
        pass
    return clients


def fetch_clients():
    try:
        objects = _run("ubus", "list", "hostapd.*").splitlines()
    except subprocess.CalledProcessError as error:
        if error.returncode != 4:
            raise
        objects = []
    if not objects:
        return _fetch_iwinfo()
    leases = _leases()
    now = time.monotonic()
    current = {}
    for obj in objects:
        data = json.loads(_run("ubus", "call", obj, "get_clients"))
        for mac, station in data.get("clients", {}).items():
            if station.get("assoc") is False:
                continue
            mac = mac.upper()
            ip, name = leases.get(mac, ("", ""))
            rates = []
            for direction in ("rx", "tx"):
                counter = station.get("bytes", {}).get(direction)
                key = (obj, mac, direction)
                old = _previous.get(key)
                rate = ""
                if isinstance(counter, (int, float)):
                    if old and counter >= old[1] and now > old[0]:
                        rate = str((counter - old[1]) * 8 / (now - old[0]))
                    _previous[key] = (now, counter)
                rates.append(rate)
            current[mac] = {
                "mac": mac, "ip": ip, "name": name,
                "rssi": str(station.get("signal", "")), "snr": "",
                # Router RX is client upload; use byte deltas rather than PHY link rates.
                "tx_rate_bps": rates[0], "rx_rate_bps": rates[1],
            }
    for key in list(_previous):
        if key[1] not in current:
            del _previous[key]
    return list(current.values())


def _fetch_iwinfo():
    devices = json.loads(_run("ubus", "call", "iwinfo", "devices"))["devices"]
    leases = _leases()
    clients = {}
    ap_count = 0
    for interface in devices:
        argument = json.dumps({"device": interface})
        info = json.loads(_run("ubus", "call", "iwinfo", "info", argument))
        if info.get("mode") != "Master":
            continue
        ap_count += 1
        data = json.loads(_run("ubus", "call", "iwinfo", "assoclist", argument))
        for station in data.get("results", []):
            mac = station["mac"].upper()
            ip, name = leases.get(mac, ("", ""))
            clients[mac] = {
                "mac": mac, "ip": ip, "name": name,
                "rssi": str(station.get("signal", "")), "snr": "",
                # This vendor driver returns zero byte counters even during traffic.
                # PHY connection rates cannot substitute for actual throughput.
                "tx_rate_bps": "", "rx_rate_bps": "",
            }
    if not ap_count:
        raise RuntimeError("No active AP interfaces found through iwinfo")
    return list(clients.values())
