# 配置说明

优先使用 `python main.py --setup`。也可以把 `deployment.example.json` 复制为 `deployment.json`，再创建独立管理凭据。文件都位于 `--config-dir` 指定目录，默认项目根目录，程序从任意工作目录启动均使用同一位置。

## deployment.json：部署方式

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `backend` | `zte` | `zte` 远端接口或 `openwrt` 本机无线 |
| `web_bind` | `127.0.0.1` | 网页监听地址；在路由器填其 LAN IP |
| `web_port` | `30707` | 网页端口 |
| `runtime_dir` | `runtime` | 相对路径基于配置目录；路由器建议 `/tmp/router-scan` |
| `tls_verify` | `true` | HTTPS 证书校验，必须为布尔值 |
| `http_timeout` | `6` | 单次 HTTP 和预检 TCP 超时，单位秒，范围 `(0,120]` |
| `history_max_entries` | `5000` | 最近和存储历史的条数上限 |

改动后重启程序或 `/etc/init.d/router-scan restart`。从 `openwrt` 切到 `zte` 时需配置远端凭据。切换不同数据来源时建议使用不同 `runtime_dir`，避免历史混在一起。

## monitor_settings.json：监控设置

初始化创建、网页保存的设置包括：

```json
{
  "router_base_url": "http://192.168.1.1:8090",
  "router_fetch_path": "/?_type=vueData&_tag=vue_topo_data&Action=GetALLClients",
  "target_mac": "",
  "target_name_contains": "",
  "push_notify_enabled": false,
  "suspect_notify_enabled": true,
  "device_absence_tolerance": 1,
  "no_traffic_sample_limit": 5
}
```

`router_base_url` 只填协议、主机和端口。不要写路径、`#/`、用户名密码或查询参数。接口路径单独放在 `router_fetch_path`。选择 `openwrt` 时远端地址和接口路径不参与本机采集。

目标 MAC 更稳定；设备可能使用随机 MAC，需要按实际列表选择。名称是包含匹配；如果目标未出现在列表中，其历史显示离线，不代表整个采集失败。默认不监控某个预设手机型号。

RSSI 阈值另存于 `rssi_threshold.json`。现有算法以信号绝对值判断，例如阈值 66 时 `-70 dBm` 会被判为低于所需信号质量；这是业务判断，设备仍可能关联在路由器上。

## zte_credentials.json：远端管理凭据

由初始化命令生成，示例结构：

```json
{
  "base_url": "http://192.168.1.1:8090",
  "username": "",
  "password": "填写管理密码"
}
```

用户名留空表示读取设备 `initial_info_json` 中的 `user` 字段，不是提交空用户名。密码必须是管理页面登录密码。凭据与完整目标地址绑定，包括协议及端口；网页改地址后必须用 `--setup` 更新相应凭据，否则拒绝把密码发送到新的地址。

会话保存为运行目录中的 `zte_session.json`，仅对相同地址复用，过期自动重新登录。登录失败退避从一分钟开始，最长十五分钟。修改密码后重启程序可清除进程内的退避计时。不要粘贴 SID 到日志或公开 issue。

## 环境变量与优先级

| 环境变量 | 作用 |
| --- | --- |
| `ROUTER_SCAN_CONFIG_DIR` | 配置目录；CLI 的 `--config-dir` 优先 |
| `ROUTER_SCAN_BACKEND` | 覆盖 `backend`；CLI 的 `--backend` 优先 |
| `ROUTER_SCAN_RUNTIME_DIR` | 覆盖运行目录 |
| `ROUTER_SCAN_WEB_BIND` | 覆盖监听地址 |
| `ROUTER_SCAN_WEB_PORT` | 覆盖网页端口 |

一般优先级为 CLI/环境变量 > `deployment.json` > 默认值。通用服务脚本不固定模式、IP 或目标端口，读取应用目录中的配置。

## 可选推送

默认关闭外部推送；空 token 或空 Webhook 地址不会发送请求。要启用，请安装 `requirements-optional.txt`，并按现有 `monitor/config.py` 的推送字段配置。当前推送凭据仍采用源码中的字段，不支持初始化向导管理；不要将真实推送字段提交到 GitHub。OpenWrt 可按固件软件源安装 `python3-requests`，HTTPS 推送还需要 SSL 标准库及证书。
