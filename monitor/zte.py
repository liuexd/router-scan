"""Native HTTP client for ZTE; credentials and cookies stay outside source."""
from __future__ import annotations
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from . import config
from .storage import write_private_json

_next_login = 0.0
_login_failures = 0


def _session_path():
    return config.STATE_FILE.parent / "zte_session.json"


def _cookies(base):
    try:
        data = json.loads(_session_path().read_text(encoding="utf-8"))
        if data.get("base_url") == base:
            return data.get("cookies", {})
    except (OSError, ValueError):
        pass
    return {}


def _request(url, cookies, payload=None):
    headers = {"User-Agent": "Mozilla/5.0"}
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    data = None if payload is None else urllib.parse.urlencode(payload).encode()
    handlers = [urllib.request.ProxyHandler({})]
    if urllib.parse.urlsplit(url).scheme == "https":
        import ssl
        context = ssl.create_default_context() if config.TLS_VERIFY else ssl._create_unverified_context()
        handlers.append(urllib.request.HTTPSHandler(context=context))
    class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, new_url):
            old = urllib.parse.urlsplit(request.full_url)
            new = urllib.parse.urlsplit(new_url)
            if (old.scheme, old.netloc) != (new.scheme, new.netloc):
                raise PermissionError("管理接口重定向到了其他地址，已停止发送会话")
            return super().redirect_request(request, fp, code, message, headers, new_url)
    handlers.append(SameOriginRedirect())
    opener = urllib.request.build_opener(*handlers)
    with opener.open(urllib.request.Request(url, headers=headers, data=data), timeout=config.HTTP_TIMEOUT) as response:
        from http.cookies import SimpleCookie
        for line in response.headers.get_all("Set-Cookie", []):
            parsed = SimpleCookie(); parsed.load(line)
            cookies.update({k: v.value for k, v in parsed.items()})
        body = response.read()
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise PermissionError("设备接口返回登录页，SID 会话未认证或已失效")


def _login(base, cookies):
    global _next_login, _login_failures
    if time.monotonic() < _next_login:
        raise PermissionError("登录暂处于退避期，请检查管理凭据")
    try:
        try:
            credentials = json.loads(config.CREDENTIALS_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise PermissionError("缺少管理凭据，请运行 python main.py --setup") from None
        if credentials.get("base_url", "").rstrip("/") != base:
            raise PermissionError("当前目标地址没有匹配的管理凭据")
        password = credentials["password"]
        username = credentials.get("username", "")
        if not username:
            initial = _request(base + "/?_type=hiddenScene&_tag=initial_info_json", cookies)
            def find_user(node):
                if isinstance(node, dict):
                    if isinstance(node.get("user"), str) and node["user"]:
                        return node["user"]
                    for value in node.values():
                        found = find_user(value)
                        if found:
                            return found
                elif isinstance(node, list):
                    for value in node:
                        found = find_user(value)
                        if found:
                            return found
                return ""
            username = find_user(initial)
            if not username:
                raise PermissionError("设备未提供内置用户名，请在凭据文件中填写 username")
        token = _request(base + "/?_type=loginsceneData&_tag=login_token_json", cookies)
        digest = hashlib.sha256((password + token["logintoken"]).encode()).hexdigest()
        result = _request(base + "/?_type=loginData&_tag=login_entry", cookies, {
            "Username": username, "Password": digest, "action": "login",
            "Frm_Logintoken": "", "captchaCode": "", "_sessionTOKEN": token["_sessionToken"],
        })
        if result.get("loginErrType") or not (cookies.get("SID") or cookies.get("SID_HTTPS_")):
            raise PermissionError("管理登录被拒绝，请核对凭据或设备锁定状态")
    except Exception:
        _login_failures += 1
        _next_login = time.monotonic() + min(900, 60 * 2 ** min(_login_failures - 1, 4))
        raise
    _login_failures = 0
    _next_login = time.monotonic() + 60
    write_private_json(_session_path(), {"base_url": base, "cookies": cookies})


def fetch_clients():
    from .fetcher import extract_clients_from_json
    base = config.ROUTER_BASE_URL.rstrip("/")
    cookies = _cookies(base)
    try:
        data = _request(config.URL, cookies)
    except (PermissionError, urllib.error.HTTPError) as error:
        if isinstance(error, urllib.error.HTTPError) and error.code not in (401, 403):
            raise
        _login(base, cookies)
        data = _request(config.URL, cookies)
    if not isinstance(data, dict) or "clientsData" not in data:
        raise RuntimeError("接口 JSON 缺少 clientsData，请核对路径或管理权限")
    clients = extract_clients_from_json(data)
    reported = data["clientsData"]
    if isinstance(reported, dict) and int(reported.get("MGET_INST_NUM", 0)) > 0 and not clients:
        raise RuntimeError("设备报告非空列表，但字段无法解析，请检查固件接口格式")
    return clients
