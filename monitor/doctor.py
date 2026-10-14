"""Diagnose from the same host and routing table that will run the monitor."""
import socket
from urllib.parse import urlsplit
from . import config


def check():
    result = {"backend": config.ROUTER_BACKEND, "url": config.URL, "ok": False}
    try:
        if config.ROUTER_BACKEND == "zte":
            target = urlsplit(config.ROUTER_BASE_URL)
            port = target.port or (443 if target.scheme == "https" else 80)
            result["port"] = port
            result["stage"] = "tcp"
            with socket.create_connection((target.hostname, port), timeout=config.HTTP_TIMEOUT):
                result["tcp_connected"] = True
            from .zte import _request, fetch_clients
            result["stage"] = "token"
            token = _request(config.ROUTER_BASE_URL + "/?_type=loginsceneData&_tag=login_token_json", {})
            result["token_available"] = bool(token.get("logintoken") and token.get("_sessionToken"))
            if not result["token_available"]:
                raise RuntimeError("token 字段缺失，请检查管理端口和设备型号")
            result["stage"] = "clients"
        else:
            from .openwrt import fetch_clients
            result["stage"] = "local_wireless"
        clients = fetch_clients()
        from .targets import has_primary, match_primary
        result.update(ok=True, device_count=len(clients), target_configured=has_primary(),
                      target_found=any(match_primary(client) for client in clients) if has_primary() else None)
    except Exception as error:
        result["error"] = str(error)
        if result.get("stage") == "tcp":
            result["hint"] = "ping 成功不代表管理端口开放；填写浏览器实际使用的协议和端口，并从运行主机检查路由"
        elif result.get("stage") == "clients":
            result["hint"] = "HTTP 200 也可能是登录页；检查凭据、SID 和接口路径"
    return result
