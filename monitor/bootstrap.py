"""Interactive setup; no optional dependencies or package import side effects."""
import getpass
import json
from pathlib import Path
from urllib.parse import urlsplit
from .storage import write_private_json


def setup(config_dir, backend=None):
    directory = Path(config_dir).resolve()
    backend = backend or input("采集模式 zte（远端中兴）/openwrt（本机）[zte]：").strip() or "zte"
    if backend not in {"zte", "openwrt"}:
        raise ValueError("采集模式必须为 zte 或 openwrt")
    deployment_path = directory / "deployment.json"
    settings_path = directory / "monitor_settings.json"
    deployment = json.loads(deployment_path.read_text(encoding="utf-8")) if deployment_path.exists() else {}
    settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    if backend == "zte":
        url = input("完整管理地址（例如 http://192.168.1.1:8090，不含 #/）：").strip().rstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("地址格式错误：只填写协议、主机和端口")
        parsed.port
        username = input("管理用户名（只显示密码的页面可留空，程序从设备读取）：").strip()
        password = getpass.getpass("管理密码（不会显示）：")
        if not password:
            raise ValueError("密码不能为空")
        write_private_json(directory / "zte_credentials.json", {"base_url": url, "username": username, "password": password})
        settings["router_base_url"] = url
    bind = input("网页监听地址 [127.0.0.1；路由器部署填写 LAN IP]：").strip() or deployment.get("web_bind", "127.0.0.1")
    runtime = input("运行数据目录 [runtime；路由器建议 /tmp/router-scan]：").strip() or deployment.get("runtime_dir", "runtime")
    target = input("目标设备 MAC（留空先查看设备列表）：").strip()
    deployment.update(backend=backend, web_bind=bind, runtime_dir=runtime)
    deployment.setdefault("tls_verify", True)
    deployment.setdefault("web_port", 30707)
    settings.update(target_mac=target, target_name_contains="", push_notify_enabled=False)
    write_private_json(deployment_path, deployment)
    write_private_json(settings_path, settings)
    print(f"配置已保存到 {directory}；请先运行 python main.py --check")
