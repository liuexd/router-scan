import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

from . import config
from .utils import ts

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _base_url() -> str:
    # Prefer explicit router_base_url; fall back to URL host
    base_candidate = getattr(config, "ROUTER_BASE_URL", "") or ""
    try:
        return config._normalize_base_url(base_candidate)  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        parsed = urlparse(getattr(config, "URL", ""))
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass
    return str(base_candidate or "http://192.168.1.1")


def _token_url() -> str:
    return f"{_base_url()}/?_type=loginsceneData&_tag=login_token_json"


def _login_url() -> str:
    return f"{_base_url()}/?_type=loginData&_tag=login_entry"


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def fetch_sid() -> tuple[str, dict]:
    session = requests.Session()

    token_resp = session.get(_token_url(), timeout=5, verify=False)
    token_resp.raise_for_status()
    token_body = token_resp.json()
    logintoken = token_body.get("logintoken")
    session_token = token_resp.headers.get("X_XSRF_TOKEN") or token_body.get("_sessionToken")
    if not logintoken or not session_token:
        raise RuntimeError(f"未拿到登录 token，响应: {token_body}")

    digest = sha256_hex(f"{config.ROUTER_PASSWORD}{logintoken}")
    payload = {
        "Username": config.ROUTER_USERNAME,
        "Password": digest,
        "action": "login",
        "Frm_Logintoken": "",
        "captchaCode": "",
        "_sessionTOKEN": session_token,
    }

    login_resp = session.post(_login_url(), data=payload, timeout=5, verify=False)
    login_resp.raise_for_status()
    login_body = login_resp.json()
    sid = session.cookies.get("SID") or login_resp.cookies.get("SID")
    if not sid:
        raise RuntimeError(f"登录成功但未拿到 SID，返回: {login_body}")

    return f"SID={sid}", login_body


def refresh_cookie_from_router() -> str:
    try:
        cookie, login_body = fetch_sid()
        config.COOKIE_FILE.write_text(cookie + "\n", encoding="utf-8")
        print(f"[{ts()}] 已刷新 Cookie: {cookie}")
        print(f"[DEBUG] login_need_refresh={login_body.get('login_need_refresh')}, sess_token={login_body.get('sess_token')}")
        return cookie
    except Exception as exc:  # noqa: BLE001
        print(f"[{ts()}] 刷新 Cookie 失败：{exc}")
        return ""


def read_cookie_from_file(path: Path = config.COOKIE_FILE) -> str:
    if not path.exists():
        raise FileNotFoundError(f"未找到 {path}，请先生成或手动创建")
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        return line if line.upper().startswith("SID=") else f"SID={line}"
    raise ValueError(f"{path} 内容为空或未包含可用的 SID 行")


def get_cookie() -> None:
    refresh_cookie_from_router()


def load_state() -> Dict[str, Dict]:
    if config.STATE_FILE.exists():
        try:
            return json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: Dict[str, Dict]) -> None:
    config.STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_url(url: str, cookie: Optional[str], extra_headers: Dict[str, str]) -> Tuple[str, Optional[str]]:
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    headers = {"User-Agent": "Mozilla/5.0"}
    if cookie:
        headers["Cookie"] = cookie
    if extra_headers:
        headers.update(extra_headers)
    resp = requests.get(url, headers=headers, timeout=10, verify=False)
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower() or None
    text = resp.text
    if config.SAVE_RAW:
        ext = "json" if (ctype == "application/json" or text.strip().startswith("{") or text.strip().startswith("[")) else "html"
        Path(f"router_raw.{ext}").write_text(text, encoding="utf-8", errors="ignore")
    return text, ctype


def parse_clients_auto(text: str, ctype: Optional[str]) -> List[Dict[str, str]]:
    if ctype == "application/json":
        try:
            data = json.loads(text)
            clients = extract_clients_from_json(data)
            if clients:
                return clients
        except Exception:
            pass
    try:
        data = json.loads(text)
        clients = extract_clients_from_json(data)
        if clients:
            return clients
    except Exception:
        pass
    return parse_clients_from_html(text)


def extract_clients_from_json(data: Any) -> List[Dict[str, str]]:
    found: List[Dict[str, str]] = []

    def norm_mac(s: str) -> str:
        return s.strip().upper().replace("-", ":")

    RSSI_KEYS = {"rssi", "signal", "signal_strength", "sig", "rx_signal", "wifi_signal", "ap_rssi", "sta_rssi"}
    SNR_KEYS = {"snr"}
    TX_RATE_KEYS = ["txratebps", "tx_rate_bps", "txratekbps", "tx_rate_kbps", "txratembps", "tx_rate_mbps"]
    RX_RATE_KEYS = ["rxratebps", "rx_rate_bps", "rxratekbps", "rx_rate_kbps", "rxratembps", "rx_rate_mbps"]

    def visit(node: Any):
        if isinstance(node, dict):
            mac_key = next((k for k in node.keys() if k.lower() in ["mac", "macaddr", "mac_addr", "macaddress"]), None)
            ip_key = next((k for k in node.keys() if k.lower() in ["ip", "ipv4", "ipaddr", "ip_addr", "ipaddress"]), None)
            name_key = next((k for k in node.keys() if k.lower() in ["name", "hostname", "devname", "device_name", "dev_name"]), None)
            rssi_key = next((k for k in node.keys() if k.lower() in RSSI_KEYS), None)
            snr_key = next((k for k in node.keys() if k.lower() in SNR_KEYS), None)
            if mac_key and ip_key:
                name = str(node.get(name_key, "")) if name_key else ""
                mac = norm_mac(str(node.get(mac_key, "")))
                ip = str(node.get(ip_key, ""))
                rssi = str(node.get(rssi_key, "")) if rssi_key else ""
                snr = str(node.get(snr_key, "")) if snr_key else ""
                tx_rate_bps = _extract_rate_bps(node, TX_RATE_KEYS, "tx")
                rx_rate_bps = _extract_rate_bps(node, RX_RATE_KEYS, "rx")
                if ip and mac:
                    found.append(
                        {
                            "name": name,
                            "ip": ip,
                            "mac": mac,
                            "rssi": rssi,
                            "snr": snr,
                            "tx_rate_bps": tx_rate_bps,
                            "rx_rate_bps": rx_rate_bps,
                        }
                    )
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(data)
    uniq = {}
    for d in found:
        uniq.setdefault(d["mac"], d)
    return list(uniq.values())


def parse_clients_from_html(html: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    candidate_tables = []
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        header_join = "|".join(headers)
        if any(k in header_join for k in ["All Clients", "设备名称"]) and ("IP" in header_join) and ("MAC" in header_join or "Mac" in header_join or "MAC 地址" in header_join):
            candidate_tables.append(table)
    if not candidate_tables:
        label = soup.find(lambda tag: tag.name in ["h2", "h3", "div", "span"] and "All Clients" in tag.get_text())
        if label:
            next_table = label.find_next("table")
            if next_table:
                candidate_tables.append(next_table)
    if not candidate_tables:
        best = None
        best_cols = 0
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True) for th in table.find_all("th")]
            if "IP" in "|".join(headers):
                cols = len(headers)
                if cols >= best_cols:
                    best, best_cols = table, cols
        if best:
            candidate_tables.append(best)
    if not candidate_tables:
        raise RuntimeError("未找到包含客户端列表的表格（该页可能是 SPA 动态渲染，需要抓取 JSON 的 XHR 接口）。")
    results: List[Dict[str, str]] = []
    for table in candidate_tables:
        for row in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(cells) < 4 or cells[0] == "设备名称":
                continue
            name, ip, mac, rssi = cells[:4]
            results.append({"name": name, "ip": ip, "mac": mac.upper(), "rssi": rssi, "snr": "", "tx_rate_bps": "", "rx_rate_bps": ""})
    return results


def diff(prev: Dict[str, Dict], curr: List[Dict[str, str]]):
    curr_by_mac = {d["mac"]: d for d in curr}
    prev_online = {m for m, v in prev.items() if v.get("online", False)}
    curr_macs = set(curr_by_mac.keys())
    new_list = [curr_by_mac[m] for m in curr_macs - prev_online]
    gone_macs = sorted(prev_online - curr_macs)
    gone_list = [
        {
            "name": prev[m].get("name", ""),
            "ip": prev[m].get("ip", ""),
            "mac": m,
            "rssi": prev[m].get("rssi", ""),
            "snr": prev[m].get("snr", ""),
            "tx_rate_bps": prev[m].get("tx_rate_bps", ""),
            "rx_rate_bps": prev[m].get("rx_rate_bps", ""),
        }
        for m in gone_macs
    ]
    now_list = [curr_by_mac[m] for m in sorted(curr_macs)]
    for m, d in curr_by_mac.items():
        prev.setdefault(m, {}).update(
            {
                "name": d.get("name", ""),
                "ip": d.get("ip", ""),
                "rssi": d.get("rssi", ""),
                "snr": d.get("snr", ""),
                "tx_rate_bps": d.get("tx_rate_bps", ""),
                "rx_rate_bps": d.get("rx_rate_bps", ""),
                "online": True,
            }
        )
    for m in gone_macs:
        if m in prev:
            prev[m]["online"] = False
    return now_list, new_list, gone_list


def _extract_rate_bps(node: Dict[str, Any], candidate_keys: List[str], direction: str) -> str:
    lowered = {str(key).lower(): key for key in node.keys()}
    values: List[float] = []
    for key in candidate_keys:
        original_key = lowered.get(key)
        if original_key is None:
            continue
        value = _normalize_rate_value(node.get(original_key), key)
        if value is not None:
            values.append(value)
    fallback_bps = _normalize_rate_value(node.get(f"{direction.upper()}RateBps"), f"{direction}ratebps")
    if fallback_bps is not None:
        values.append(fallback_bps)
    fallback_kbps = _normalize_rate_value(node.get(f"{direction.upper()}RateKbps"), f"{direction}ratekbps")
    if fallback_kbps is not None:
        values.append(fallback_kbps)
    fallback_mbps = _normalize_rate_value(node.get(f"{direction.upper()}RateMbps"), f"{direction}ratembps")
    if fallback_mbps is not None:
        values.append(fallback_mbps)
    return f"{max(values)}" if values else ""


def _normalize_rate_value(value: Any, key_name: str) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except Exception:
        return None
    lowered = key_name.lower()
    if "mbps" in lowered:
        return max(0.0, number * 1_000_000)
    if "kbps" in lowered:
        return max(0.0, number * 1_000)
    return max(0.0, number)


def scan_once(
    prev_state: Dict[str, Dict],
    cookie: str,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], List[Dict[str, str]], str]:
    def _fetch(active_cookie: str) -> List[Dict[str, str]]:
        text, content_type = fetch_url(config.URL, active_cookie, config.EXTRA_HEADERS)
        return parse_clients_auto(text, content_type)

    try:
        current = _fetch(cookie)
    except requests.exceptions.HTTPError as error:
        status_code = getattr(error.response, "status_code", None)
        if status_code in {401, 403}:
            print(f"[{ts()}] HTTP {status_code}：Cookie 可能失效，正在刷新并重抓")
            cookie = refresh_cookie_from_router()
            if not cookie:
                return [], [], [], cookie
            current = _fetch(cookie)
        else:
            print(f"[{ts()}] HTTP 错误：{error}")
            return [], [], [], cookie
    except RuntimeError as error:
        if "未找到包含客户端列表的表格" in str(error):
            print(f"[{ts()}] 解析失败（可能跳回登录页），正在刷新 Cookie 并重抓")
            cookie = refresh_cookie_from_router()
            if not cookie:
                return [], [], [], cookie
            current = _fetch(cookie)
        else:
            print(f"[{ts()}] 解析异常：{error}")
            return [], [], [], cookie
    except Exception as error:
        print(f"[{ts()}] 抓取/解析失败：{error}")
        return [], [], [], cookie

    current_devices, new_devices, gone_devices = diff(prev_state, current)
    save_state(prev_state)
    return current_devices, new_devices, gone_devices, cookie
