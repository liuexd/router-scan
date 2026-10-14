# OpenWrt 部署工具

完整流程见 [OpenWrt 部署指南](../docs/openwrt.md)。

- `install-openwrt.sh`：检查 Python 3.9+ 与标准库，复制程序，保留已有配置，生成 procd 服务。默认不启动；`--start` 仅在预检成功后启用服务。
- `router-scan.init`：服务模板，`@APP_DIR@` 由安装脚本替换，不要直接复制模板到 `/etc/init.d`。
- `uninstall-openwrt.sh`：停止服务、取消自启、删除 init 文件，保留应用与凭据。

采集后端、远端地址、管理密码、网页监听 IP 都从配置读取，不固定到某台设备。运行目录推荐 `/tmp/router-scan`，重启清空历史与会话。
