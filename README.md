# router-scan

扫描路由器客户端，记录目标设备的信号与活动状态，提供设备列表、曲线和历史网页。

支持两种明确的采集模式：

| 模式 | 程序运行位置 | 数据来源 |
| --- | --- | --- |
| `zte` | Windows/Linux 电脑或 OpenWrt 路由器 | 远端中兴智能路由器的管理 HTTP/HTTPS 接口 |
| `openwrt` | 被监控的 OpenWrt/ImmortalWrt 本机 | `hostapd` 或 `iwinfo` 的关联无线客户端 |

核心采集只需要 **Python 3.9+ 标准库**。网页监听服务默认只绑定 `127.0.0.1:30707`；路由器安装脚本使用本机 LAN 地址。中兴设备必须支持本文列出的接口，不保证任意型号通用。

## 电脑上快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe main.py --setup
.\.venv\Scripts\python.exe main.py --check
.\.venv\Scripts\python.exe main.py
```

Linux 上把解释器路径换成 `.venv/bin/python`。核心无需 `pip install`。需要外部推送或旧版 HTML 解析辅助函数时才安装 `requirements-optional.txt`。

初始化时选择采集模式，填写**完整管理地址**、管理凭据、网页监听地址及数据目录。例如浏览器访问 `http://192.168.1.1:8090/#/`，程序应填写 `http://192.168.1.1:8090`。`#/` 是浏览器路由，不属于请求地址；遗漏 `:8090` 会转为请求 80 端口。

页面只显示密码时，用户名可以留空，程序尝试从设备初始化接口读取内置用户名；读取不到时要求手动提供。密码通过隐藏输入保存到独立凭据文件，不修改源码。

`--check` 必须在**实际运行程序的主机**上执行。它依次检查 TCP 端口、登录 token、管理会话和客户端 JSON，失败返回非零退出码；不会把 HTTP 200 的登录页当作设备列表。

通过检查后访问 [本机监控页面](http://127.0.0.1:30707)。先打开设备列表选择目标；默认没有指定目标，曲线暂时为空属于预期行为。`start.bat` 自动使用同目录虚拟环境，也可运行 `start.bat --check`。

## 安装到 OpenWrt/ImmortalWrt

把当前项目上传到路由器的临时目录，使用 root 登录并执行：

```sh
cd /tmp/router-scan-source
sh deploy/install-openwrt.sh
python3 /opt/router-scan/main.py --setup
python3 /opt/router-scan/main.py --check
/etc/init.d/router-scan enable
/etc/init.d/router-scan start
```

路由器没有 Python 时，检查可用空间及软件源后执行 `sh deploy/install-openwrt.sh --install-python`。默认只安装程序和服务文件，不立即启动。完整上传、依赖、离线安装、升级、卸载说明见 [OpenWrt 部署指南](docs/openwrt.md)。

部署在 OpenWrt 上也可以选择 `zte`，监控另一台中兴设备。运行位置和采集模式是两件事；修改网页里的路由器地址不会自动改变模式。

## 配置与维护

- [配置文件和环境变量](docs/configuration.md)：采集模式、地址与端口、凭据、监听地址、历史上限。
- [故障排查](docs/troubleshooting.md)：ping 通但端口超时、token 与 SID、设备为空、目标不在列表、网页不可访问。
- [OpenWrt 部署指南](docs/openwrt.md)：依赖检查、服务、自启、存储和升级。

```sh
python main.py --help
python main.py --check
python main.py --once
python main.py --config-dir /path/to/config
```

网页顶部显示最近采集状态；`/diagnostics` 返回后端、请求地址、最近成功时间和错误；`/devices` 返回设备与 `stale` 标志。采集失败不会生成假离线历史。错误登录有退避，服务不会每几秒重复提交错误密码。

## 数据与限制

运行状态、历史与会话放在 `runtime_dir`；默认保留一天，最多 5000 条历史。按每五秒一条计算，单目标约覆盖最近七小时；增加上限会增加内存和磁盘使用。OpenWrt 默认放在 `/tmp/router-scan`，重启清空，配置和管理凭据仍留在应用目录。

本机无线采集根据实际关联状态识别客户端，DHCP 租约只补全名称和 IP，不作为在线证明。联发科驱动可能不给有效流量计数，这时流量显示不可用，不能把 PHY 连接速率当作实际流量。中兴接口提供有效计数时保留流量信息。RSSI 阈值和流量停滞判断属于监控规则，“疑似下线”不等同于实测断开。

管理凭据以 JSON 明文保存在配置目录，POSIX 系统使用 `0600` 权限；Windows 请保护该目录的用户访问权限。凭据、Cookie、运行状态已加入 `.gitignore`，不要手动提交。HTTPS 默认校验证书，自签名设备见故障排查说明。网页没有账号认证，仅在可信本机或 LAN 使用，不创建公网端口转发。

`versions/` 是历史快照；部署和维护以项目根目录为准。

## 验证修改

```sh
python -m unittest discover -s tests -v
```
