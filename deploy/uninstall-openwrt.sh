#!/bin/sh
# Keeps application files, passwords and history for an explicit manual decision.
set -eu
[ "$(id -u)" = 0 ] || { echo "请使用 root 运行"; exit 1; }
if [ -x /etc/init.d/router-scan ]; then
    /etc/init.d/router-scan stop || true
    /etc/init.d/router-scan disable || true
    rm -f /etc/init.d/router-scan
fi
echo "服务已移除，应用目录和凭据仍保留。确认不再需要后按 docs/openwrt.md 删除专用应用目录。"
