from . import config
from .notifications import send_webhook_notification
from .utils import ts


def append_event_log(message: str) -> None:
    try:
        with config.EVENT_LOG.open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    except Exception:
        pass


def log_status(message: str) -> None:
    try:
        with config.STATUS_LOG.open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    except Exception as exc:
        print(f"[{ts()}] 写入状态日志失败：{exc}")


def log_program_exit(reason: str, notify: bool = True) -> None:
    message = f"[{ts()}] 程序退出：{reason}"
    log_status(message)
    if notify and config.EXIT_NOTIFY_ENABLED:
        send_webhook_notification(message)
