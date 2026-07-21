import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

from . import config
from .targets import configured_targets, find_target, is_device_online
from .utils import ts

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
_NEXT_AUTH_REFRESH_AT = 0.0


class RouterAuthenticationError(RuntimeError):
    """The router returned its login page instead of device data."""


class ClientDataParseError(RuntimeError):
    """The response was neither supported JSON nor a client table."""


def _looks_like_login_page(text: str, content_type: Optional[str] = None) -> bool:
    sample = (text or "").lstrip()
    if not sample.startswith("<"):
        return False
    lowered = sample[:12000].casefold()
    markers = (
        "login_token_json",
        "login_entry",
        "autologin_entry",
        "login_need_refresh",
        "sessiontimeout",
        "g_sessiontmptoken",
        "loginwrapper",
        "id=\"app\"",
    )
    return "<html" in lowered and (
        any(marker in lowered for marker in markers)
        or ("/jquery/static/js/app." in lowered and "<title" in lowered)
    )


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
        login_error = str(login_body.get("loginErrType") or "").strip()
        if login_error == "e_exceed_max_user_preempt":
            raise RouterAuthenticationError(
                "路由器已有其他管理员会话，已达到登录上限。请先退出路由器管理页面，"
                "再等待程序自动重试。"
            )
        if login_error and login_error.lower() not in {"succ", "success"}:
            raise RouterAuthenticationError(f"路由器登录失败：{login_error}")
        config.COOKIE_FILE.write_text(cookie + "\n", encoding="utf-8")
        print(f"[{ts()}] 登录 Cookie 已刷新")
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
    content_prefix = resp.content[:2048].lower()
    if b'charset="utf-8"' in content_prefix or b"charset='utf-8'" in content_prefix or b"charset=utf-8" in content_prefix:
        resp.encoding = "utf-8"
    text = resp.text
    if config.SAVE_RAW:
        ext = "json" if (ctype == "application/json" or text.strip().startswith("{") or text.strip().startswith("[")) else "html"
        Path(f"router_raw.{ext}").write_text(text, encoding="utf-8", errors="ignore")
    return text, ctype


def parse_clients_auto(text: str, ctype: Optional[str]) -> List[Dict[str, str]]:
    if _looks_like_login_page(text, ctype):
        raise RouterAuthenticationError("路由器登录已失效，接口返回了登录页面")

    if ctype == "application/json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ClientDataParseError(f"接口声明为 JSON，但内容无法解析：{exc}") from exc
        return extract_clients_from_json(data)
    try:
        data = json.loads(text)
        return extract_clients_from_json(data)
    except json.JSONDecodeError:
        pass
    return parse_clients_from_html(text)


def extract_clients_from_json(data: Any) -> List[Dict[str, str]]:
    found: List[Dict[str, str]] = []

    def norm_mac(s: str) -> str:
        return s.strip().upper().replace("-", ":")

    RSSI_KEYS = {"rssi", "signal", "signal_strength", "sig", "rx_signal", "wifi_signal", "ap_rssi", "sta_rssi"}

    def visit(node: Any):
        if isinstance(node, dict):
            mac_key = next((k for k in node.keys() if k.lower() in ["mac", "macaddr", "mac_addr", "macaddress"]), None)
            ip_key = next((k for k in node.keys() if k.lower() in ["ip", "ipv4", "ipaddr", "ip_addr", "ipaddress"]), None)
            name_key = next((k for k in node.keys() if k.lower() in ["name", "hostname", "devname", "device_name", "dev_name"]), None)
            rssi_key = next((k for k in node.keys() if k.lower() in RSSI_KEYS), None)
            if mac_key and ip_key:
                name = str(node.get(name_key, "")) if name_key else ""
                mac = norm_mac(str(node.get(mac_key, "")))
                ip = str(node.get(ip_key, ""))
                rssi = str(node.get(rssi_key, "")) if rssi_key else ""
                if ip and mac:
                    found.append({"name": name, "ip": ip, "mac": mac, "rssi": rssi})
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
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        raise ClientDataParseError(
            "响应中没有客户端 JSON 或表格"
            + (f"（页面标题：{title}）" if title else "")
        )
    results: List[Dict[str, str]] = []
    for table in candidate_tables:
        for row in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(cells) < 4 or cells[0] == "设备名称":
                continue
            name, ip, mac, rssi = cells[:4]
            results.append({"name": name, "ip": ip, "mac": mac.upper(), "rssi": rssi})
    return results


def diff(prev: Dict[str, Dict], curr: List[Dict[str, str]]):
    curr_by_mac = {d["mac"]: d for d in curr}
    prev_online = {m for m, v in prev.items() if v.get("online", False)}
    curr_macs = set(curr_by_mac.keys())
    new_list = [curr_by_mac[m] for m in curr_macs - prev_online]
    gone_macs = sorted(prev_online - curr_macs)
    gone_list = [{"name": prev[m].get("name", ""), "ip": prev[m].get("ip", ""), "mac": m, "rssi": prev[m].get("rssi", "")} for m in gone_macs]
    now_list = [curr_by_mac[m] for m in sorted(curr_macs)]
    for m, d in curr_by_mac.items():
        prev.setdefault(m, {}).update({"name": d.get("name", ""), "ip": d.get("ip", ""), "rssi": d.get("rssi", ""), "online": True})
    for m in gone_macs:
        if m in prev:
            prev[m]["online"] = False
    return now_list, new_list, gone_list


def _arping_target(target_ip: str) -> Optional[bool]:
    if not target_ip or not config.ARPING_BEFORE_FETCH:
        return None

    try:
        import nmap  # type: ignore

        scanner = nmap.PortScanner()
        scanner.scan(hosts=target_ip, arguments="-sn -PR --max-retries 2 --host-timeout 1500ms")
        if target_ip in scanner.all_hosts():
            return scanner[target_ip].state() == "up"
        return False
    except ImportError:
        print(f"[{ts()}] python-nmap 未安装，跳过 ARPing 预热")
        return None
    except Exception as exc:
        print(f"[{ts()}] python-nmap 预热失败：{exc}")
        return False


def _probe_targets(devices: List[Dict[str, str]]) -> Dict[str, Optional[bool]]:
    """Probe both configured devices concurrently after one router snapshot."""

    targets = configured_targets()
    results: Dict[str, Optional[bool]] = {target.key: None for target in targets}
    if not config.ARPING_BEFORE_FETCH:
        return results

    jobs: Dict[str, str] = {}
    for target in targets:
        device = find_target(devices, target)
        if not is_device_online(device):
            continue
        target_ip = str((device or {}).get("ip", "")).strip()
        # Keep the existing manual override for device one for compatibility.
        if target.key == "primary" and config.ARPING_MODE == "manual":
            target_ip = config.ARPING_MANUAL_IP.strip() or target_ip
        if target_ip:
            jobs[target.key] = target_ip

    if not jobs:
        return results
    with ThreadPoolExecutor(max_workers=min(2, len(jobs)), thread_name_prefix="device-probe") as pool:
        futures = {pool.submit(_arping_target, ip): key for key, ip in jobs.items()}
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as exc:
                print(f"[{ts()}] {key} ARP 检测失败：{exc}")
                results[key] = False
    return results


def scan_once(
    prev_state: Dict[str, Dict],
    cookie: str,
) -> Tuple[
    List[Dict[str, str]],
    List[Dict[str, str]],
    List[Dict[str, str]],
    str,
    Dict[str, Optional[bool]],
]:
    global _NEXT_AUTH_REFRESH_AT
    def _fetch(active_cookie: str) -> List[Dict[str, str]]:
        text, content_type = fetch_url(config.URL, active_cookie, config.EXTRA_HEADERS)
        return parse_clients_auto(text, content_type)

    def _last_known_devices() -> List[Dict[str, str]]:
        devices = [
            {
                "name": str(info.get("name", "")),
                "ip": str(info.get("ip", "")),
                "mac": str(mac),
                "rssi": str(info.get("rssi", "")),
            }
            for mac, info in prev_state.items()
            if info.get("online", False)
        ]
        return sorted(devices, key=lambda device: device["mac"])

    arping_results: Dict[str, Optional[bool]] = {
        target.key: None for target in configured_targets()
    }
    current: Optional[List[Dict[str, str]]] = None
    last_error: Optional[Exception] = None
    for attempt in range(2):
        try:
            current = _fetch(cookie)
            break
        except requests.exceptions.HTTPError as error:
            last_error = error
            status_code = getattr(error.response, "status_code", None)
            needs_login = status_code in {401, 403}
        except RouterAuthenticationError as error:
            last_error = error
            needs_login = True
        except (ClientDataParseError, requests.RequestException) as error:
            last_error = error
            needs_login = False
        except Exception as error:
            last_error = error
            needs_login = False

        if attempt == 0 and needs_login:
            now = time.monotonic()
            if now >= _NEXT_AUTH_REFRESH_AT:
                print(f"[{ts()}] 登录状态已失效，正在刷新 Cookie 后重试")
                refreshed_cookie = refresh_cookie_from_router()
                if refreshed_cookie:
                    _NEXT_AUTH_REFRESH_AT = 0.0
                    cookie = refreshed_cookie
                    continue
                _NEXT_AUTH_REFRESH_AT = now + max(
                    5, int(getattr(config, "AUTH_RETRY_INTERVAL_SECONDS", 30))
                )
        break

    if current is None:
        print(f"[{ts()}] 本轮扫描失败，保留上次设备状态：{last_error}")
        return _last_known_devices(), [], [], cookie, arping_results

    current_devices, new_devices, gone_devices = diff(prev_state, current)
    arping_results = _probe_targets(current_devices)
    save_state(prev_state)
    return current_devices, new_devices, gone_devices, cookie, arping_results
