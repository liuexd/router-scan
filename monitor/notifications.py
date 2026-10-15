from __future__ import annotations
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from . import config
from .utils import ts

WEBHOOK_STATUS = {"last_attempt": None, "last_success": None, "error": ""}


def _webhook_url():
    if config.NOTIFICATIONS_FILE.exists():
        data = json.loads(config.NOTIFICATIONS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("webhook_url", ""), str):
            raise ValueError("通知配置格式错误")
        return data.get("webhook_url", "").strip()
    return config.WEBHOOK_URL


def webhook_status():
    try:
        configured = bool(_webhook_url())
    except (ValueError, OSError):
        configured = False
    return dict(WEBHOOK_STATUS, enabled=config.PUSH_NOTIFY_ENABLED, configured=configured)


def send_pushplus_notification(content: str) -> None:
    if not config.PUSH_NOTIFY_ENABLED or not config.PUSHPLUS_TOKEN:
        return
    import requests
    params = {
        "token": config.PUSHPLUS_TOKEN,
        "title": config.PUSHPLUS_TITLE,
        "content": content,
        "topic": config.PUSHPLUS_TOPIC,
    }
    try:
        requests.get("http://www.pushplus.plus/send", params=params, timeout=10)
    except Exception as exc:
        print(f"[{ts()}] PushPlus 推送失败：{exc}")


def send_webhook_notification(text: str) -> bool:
    if not config.PUSH_NOTIFY_ENABLED:
        WEBHOOK_STATUS["error"] = "推送总开关关闭"
        return False
    WEBHOOK_STATUS["last_attempt"] = ts()
    try:
        url = _webhook_url()
        if not url:
            raise ValueError("未配置 Webhook 地址")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Webhook 地址格式错误")
        payload = json.dumps({"msgtype": "text", "text": {"content": text}}, ensure_ascii=False).encode("utf-8")
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json; charset=utf-8"})
        with opener.open(request, timeout=10) as response:
            result = json.loads(response.read())
        if not isinstance(result, dict) or result.get("errcode") != 0:
            code = result.get("errcode", "missing") if isinstance(result, dict) else "missing"
            raise ValueError(f"Webhook 平台拒绝消息，errcode={code}")
        WEBHOOK_STATUS.update(last_success=ts(), error="")
        print(f"[{ts()}] Webhook 平台已接受消息")
        return True
    except urllib.error.HTTPError as error:
        message = f"Webhook HTTP 错误 {error.code}"
    except (ImportError, ModuleNotFoundError):
        message = "Webhook 缺少 HTTPS/SSL 标准库，请安装 python3-openssl"
    except (socket.timeout, TimeoutError):
        message = "Webhook 请求超时"
    except urllib.error.URLError:
        message = "Webhook 网络连接或 TLS 验证失败"
    except ValueError as error:
        message = str(error) if not isinstance(error, json.JSONDecodeError) else "Webhook 返回了无效 JSON"
    except Exception:
        message = "Webhook 发送异常，请检查运行环境"
    WEBHOOK_STATUS["error"] = message
    print(f"[{ts()}] {message}")
    return False


def notify_status_change(is_online: bool) -> None:
    if not config.STATUS_NOTIFY_ENABLED:
        return
    content = "U" if is_online else "D"
    status_text = "UP" if is_online else "DOWN"
    send_pushplus_notification(content)
    send_webhook_notification(f"[{ts()}] {status_text}")


def notify_suspect_detected(summary: str, evidence: str = "") -> None:
    if not config.SUSPECT_NOTIFY_ENABLED:
        return
    lines = [f"[{ts()}] SUSPECT", summary]
    if evidence:
        lines.append(evidence)
    message = "\n".join(lines)
    send_pushplus_notification(f"SUSPECT\n{summary}")
    send_webhook_notification(message)


def notify_presence_change(is_present: bool) -> None:
    if not config.STATUS_NOTIFY_ENABLED:
        return
    status_text = "ONLINE" if is_present else "OFFLINE"
    send_webhook_notification(f"[{ts()}] {status_text}")
