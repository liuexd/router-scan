import requests

from . import config
from .utils import ts


def send_pushplus_notification(content: str) -> None:
    if not config.PUSH_NOTIFY_ENABLED:
        return
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


def send_webhook_notification(text: str) -> None:
    if not config.PUSH_NOTIFY_ENABLED:
        return
    payload = {"msgtype": "text", "text": {"content": text}}
    try:
        requests.post(config.WEBHOOK_URL, json=payload, timeout=10)
    except Exception as exc:
        print(f"[{ts()}] Webhook 推送失败：{exc}")


def notify_status_change(is_online: bool) -> None:
    if not config.STATUS_NOTIFY_ENABLED:
        return
    content = "U" if is_online else "D"
    status_text = "UP" if is_online else "DOWN"
    send_pushplus_notification(content)
    send_webhook_notification(f"[{ts()}]+{status_text}")


def notify_presence_change(is_present: bool) -> None:
    if not config.STATUS_NOTIFY_ENABLED:
        return
    status_text = "ONLINE" if is_present else "OFFLINE"
    send_webhook_notification(f"[{ts()}]+{status_text}")
