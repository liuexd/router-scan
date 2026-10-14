#!/bin/sh
# Run on the OpenWrt host. Never modifies package feeds or network settings.
set -eu
APP_DIR=/opt/router-scan
INSTALL_PYTHON=0
START_SERVICE=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --prefix) APP_DIR="$2"; shift 2 ;;
        --install-python) INSTALL_PYTHON=1; shift ;;
        --start) START_SERVICE=1; shift ;;
        *) echo "Usage: sh deploy/install-openwrt.sh [--prefix /opt/router-scan] [--install-python] [--start]"; exit 2 ;;
    esac
done
case "$APP_DIR" in
    /*) ;;
    *) echo "安装目录必须是绝对路径"; exit 2 ;;
esac
case "$APP_DIR" in
    /|/etc|/usr|/bin|/sbin|/tmp|/var|*..*|*[!a-zA-Z0-9_/-]*) echo "请使用专用安装目录，例如 /opt/router-scan"; exit 2 ;;
esac
[ "$(id -u)" = 0 ] || { echo "请使用 root 运行"; exit 1; }
[ -f /etc/openwrt_release ] || { echo "此脚本仅适用于 OpenWrt/ImmortalWrt"; exit 1; }
SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
command -v ubus >/dev/null || { echo "缺少 ubus"; exit 1; }
if ! command -v python3 >/dev/null; then
    if [ "$INSTALL_PYTHON" = 0 ]; then
        echo "缺少 Python。检查软件源后，使用 --install-python 安装，或参阅 docs/openwrt.md 离线安装。"
        exit 1
    fi
    if command -v opkg >/dev/null; then
        opkg update
        opkg install python3-light python3-urllib python3-email python3-logging python3-codecs
    else
        echo "当前包管理器不属于已验证的 opkg 流程，请手动安装 Python 3.9+ 后重试。"
        exit 1
    fi
fi
python3 -c 'import sys,http.server,urllib.request,json,subprocess; assert sys.version_info >= (3,9), "需要 Python 3.9+"' || {
    echo "Python 版本过低或标准库不完整，参阅 docs/openwrt.md"; exit 1;
}
mkdir -p "$APP_DIR"
APP_DIR=$(CDPATH= cd -- "$APP_DIR" && pwd)
case "$APP_DIR" in
    /|/etc|/usr|/bin|/sbin|/tmp|/var) echo "安装目录解析到系统目录，停止安装"; exit 2 ;;
esac
SOURCE_DIR=$(CDPATH= cd -- "$SOURCE_DIR" && pwd)
if [ "$SOURCE_DIR" != "$APP_DIR" ]; then
    mkdir -p "$APP_DIR/monitor"
    cp "$SOURCE_DIR/main.py" "$APP_DIR/main.py"
    cp "$SOURCE_DIR"/monitor/*.py "$APP_DIR/monitor/"
fi
# Syntax check without writing pycache to flash.
python3 - "$APP_DIR" <<'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
for path in [root / "main.py", *sorted((root / "monitor").glob("*.py"))]:
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
PY
if [ ! -f "$APP_DIR/deployment.json" ]; then
    LAN_IP=$(ubus call network.interface.lan status | jsonfilter -e '@["ipv4-address"][0].address')
    LAN_IP=${LAN_IP:-127.0.0.1}
    python3 - "$APP_DIR" "$LAN_IP" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1]) / "deployment.json"
path.write_text(json.dumps({"backend": "zte", "web_bind": sys.argv[2], "web_port": 30707,
                           "runtime_dir": "/tmp/router-scan", "tls_verify": True}, indent=2))
path.chmod(0o600)
PY
fi
python3 - "$SOURCE_DIR/deploy/router-scan.init" "$APP_DIR" <<'PY'
import pathlib, sys
source, app = sys.argv[1:]
path = pathlib.Path("/etc/init.d/router-scan")
path.write_text(pathlib.Path(source).read_text().replace("@APP_DIR@", app))
path.chmod(0o755)
PY
echo "安装完成：$APP_DIR"
if [ "$START_SERVICE" = 1 ]; then
    python3 "$APP_DIR/main.py" --check || {
        echo "预检未通过，服务未启用；请先运行 --setup 并根据诊断结果修正配置。"; exit 1;
    }
    /etc/init.d/router-scan enable
    /etc/init.d/router-scan restart
else
    echo "下一步：python3 $APP_DIR/main.py --setup"
    echo "然后：python3 $APP_DIR/main.py --check"
    echo "通过后：/etc/init.d/router-scan enable && /etc/init.d/router-scan start"
fi
