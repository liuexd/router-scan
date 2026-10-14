"""Start, configure, or diagnose router-scan using Python 3.9+."""
import argparse
import json
import os
import sys
from pathlib import Path


def cli():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="路由器客户端监控：先 --setup，再 --check，最后运行")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--setup", action="store_true", help="交互生成配置和管理凭据")
    mode.add_argument("--check", action="store_true", help="检查 TCP、token、登录和设备列表；输出 JSON")
    mode.add_argument("--once", action="store_true", help="采集一次，不启动网页")
    parser.add_argument("--config-dir", default=os.environ.get("ROUTER_SCAN_CONFIG_DIR", str(Path(__file__).resolve().parent)), help="配置目录")
    parser.add_argument("--backend", choices=["zte", "openwrt"], help="覆盖配置中的采集模式")
    args = parser.parse_args()
    os.environ["ROUTER_SCAN_CONFIG_DIR"] = str(Path(args.config_dir).resolve())
    if args.backend:
        os.environ["ROUTER_SCAN_BACKEND"] = args.backend
    try:
        if args.setup:
            from monitor.bootstrap import setup
            setup(args.config_dir, args.backend)
            return 0
        from monitor import config
        if args.check:
            from monitor.doctor import check
            result = check()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1
        if args.once:
            config.WATCH = False
            config.ENABLE_HISTORY_WEB = False
        from monitor import main
        main()
        if args.once:
            from monitor.fetcher import SCAN_STATUS
            return 1 if SCAN_STATUS.get("error") else 0
        return 0
    except (ValueError, OSError, ImportError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
