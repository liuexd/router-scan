# OpenWrt/ImmortalWrt 部署

支持 Python 3.9+、procd 与 ubus。已验证 ImmortalWrt 21.02 的 aarch64 平台和联发科专用驱动。软件源、CPU 架构和剩余存储因固件而异；脚本不会替换软件源或升级固件。

## 1. 登录、空间与依赖

使用有效 root SSH 会话。先检查：

```sh
cat /etc/openwrt_release
df -h /overlay
free -m
python3 --version
ubus list
```

Python 精简环境和应用需要一定闪存空间；本次安装 Python 使用约 10 MB，实际取决于版本与压缩。`/rom` 只读且通常显示 100%，应查看 `/overlay`，不是直接删除 `/rom` 文件。

opkg 固件的 HTTP 基础环境：

```sh
opkg update
opkg install python3-light python3-urllib python3-email python3-logging python3-codecs
python3 -c 'import http.server,urllib.request,json,subprocess'
```

HTTPS 额外需要 SSL 模块；按当前软件源安装 `python3-openssl` 和证书包。本机无线模式缺少 `iwinfo` ubus 服务时安装 `rpcd-mod-iwinfo`，然后检查 `ubus call iwinfo devices`。脚本不自动重启网络或 rpcd。

## 2. 上传代码

在电脑下载或克隆项目。为了节省路由器空间，上传运行必要文件：`main.py`、整个 `monitor/` 和整个 `deploy/`，不要上传 `.git/`、`.venv/`、`versions/`、缓存和真实凭据。

可以在本机打包：

```sh
tar --exclude=__pycache__ -czf router-scan-source.tar.gz main.py monitor deploy/router-scan.init deploy/install-openwrt.sh deploy/uninstall-openwrt.sh
scp router-scan-source.tar.gz root@路由器地址:/tmp/
```

在路由器解压：

```sh
mkdir -p /tmp/router-scan-source
tar -xzf /tmp/router-scan-source.tar.gz -C /tmp/router-scan-source
cd /tmp/router-scan-source
sh deploy/install-openwrt.sh
```

现代 scp 默认使用 SFTP；设备没有 SFTP 子系统时可按客户端支持使用 `scp -O`，或通过已有文件上传工具。上传失败与程序 token 无关。

缺少 Python 时，可在检查软件源后使用 `--install-python`。脚本会检查 root、OpenWrt、Python 版本/标准库和应用语法，再安装通用 procd 服务。已有 `deployment.json`、`monitor_settings.json`、`zte_credentials.json` 不会被覆盖。

## 3. 初始化并验证

```sh
python3 /opt/router-scan/main.py --setup
python3 /opt/router-scan/main.py --check
```

远端中兴选择 `zte`，输入浏览器的完整管理地址，包括可能的 `:8090`。监控本机无线选择 `openwrt`，无需中兴管理密码。

网页监听地址填写本机 LAN IP，运行目录建议 `/tmp/router-scan`。如果不知道 LAN IP：

```sh
ubus call network.interface.lan status
```

预检通过后启动：

```sh
/etc/init.d/router-scan enable
/etc/init.d/router-scan start
/etc/init.d/router-scan status
```

网页地址为 `http://LAN-IP:30707`。在有可靠证据前不要创建公网端口转发。安装脚本支持 `--start`，但仅在 `--check` 通过后启用和重启服务。

可以使用 `--prefix /mnt/router-scan` 将应用放到专用挂载目录；安装脚本按该目录生成服务路径。外置存储必须在开机启动服务前可用。默认目录是 `/opt/router-scan`。

## 4. 数据与资源

配置和凭据在应用目录持久保存。状态、历史、会话在 `runtime_dir`；默认内存目录重启清空，也会丢失重启前的曲线。要持久历史，使用可靠外置存储，例如 `/mnt/router-scan-data`，并配置适合内存的 `history_max_entries`。

默认一天/最多 5000 条历史，避免无限增长。状态/历史原子写入，网页不会读到写到一半的 JSON。procd 输出交给系统日志管理；网页请求不逐条记录访问日志。

## 5. 软件源不通或离线安装

不要因 HTTPS 失败随意混用不同固件、不同 CPU 架构的包。检查软件源地址、网络路由、TLS 和证书。

需要离线安装时，在联网电脑下载与固件版本、架构匹配的 Python 包及依赖，核对对应 `Packages` 清单中的 SHA256。上传到 `/tmp/` 后执行：

```sh
opkg install /tmp/router-scan-packages/*.ipk
python3 -c 'import http.server,urllib.request,json,subprocess'
```

确认安装完成后删除**本次上传的专用目录**。安装脚本不会关闭包签名验证、修改软件源，也不会自动使用第三方镜像。

如果 `opkg` 提示锁定，先检查是否有正在安装/更新的任务，不直接删除锁文件或终止无关进程。

## 6. 升级、回滚和卸载

升级前把应用目录备份到电脑，备份中包含凭据，应限制访问。将新版本的必要文件上传到独立临时目录，运行新版本安装脚本；它保留已有配置。然后：

```sh
python3 /opt/router-scan/main.py --check
/etc/init.d/router-scan restart
/etc/init.d/router-scan status
```

失败时恢复上一版程序文件及对应配置并重启，不需要恢复出厂或重启无线网络。

```sh
sh deploy/uninstall-openwrt.sh
```

卸载脚本停止服务、取消自启并移除 init 文件，保留应用和管理凭据供你决定。确认不再需要后删除**实际专用安装目录**。不要直接卸载 Python 公共依赖，其他服务可能正在使用。

## 7. 存储清理

先定位：

```sh
du -k /overlay/upper 2>/dev/null | sort -n | tail -20
df -h /overlay
```

可清理本次临时上传包、已确认不用的独立应用及其配置。`/tmp` 清理释放内存，不一定增加闪存可用空间；压缩闪存的实际释放量可能晚于文件删除。不要删除正在运行的代理/组网程序、网络配置、证书或包管理数据库。本文的部署脚本不会替用户卸载其他应用。
