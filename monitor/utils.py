from __future__ import annotations
import sys
from datetime import datetime
from typing import Any, Dict, List, Tuple

from . import config


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def warn(msg: str) -> None:
    flag = "*** " + msg + " ***"
    print("\n" + flag)
    if config.BEEP_ON_EVENT:
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass


def print_table(
    title: str,
    rows: List[Dict[str, str]],
    cols: Tuple[str, str, str, str] = ("name", "ip", "mac", "rssi"),
    headers: Tuple[str, str, str, str] = ("设备名称", "IP", "MAC", "RSSI"),
) -> None:
    print(f"\n== {title} ({len(rows)}) ==")
    if not rows:
        print("(空)")
        return

    widths = [24, 16, 18, 8]
    line_fmt = "{:<{w0}}  {:<{w1}}  {:<{w2}}  {:<{w3}}"
    print(line_fmt.format(headers[0], headers[1], headers[2], headers[3], w0=widths[0], w1=widths[1], w2=widths[2], w3=widths[3]))
    print("-" * (sum(widths) + 6))
    for r in rows:
        print(
            line_fmt.format(
                r.get(cols[0], ""),
                r.get(cols[1], ""),
                r.get(cols[2], ""),
                r.get(cols[3], ""),
                w0=widths[0],
                w1=widths[1],
                w2=widths[2],
                w3=widths[3],
            )
        )


def parse_rssi_value(rssi_text: str) -> int | None:
    import re

    if not rssi_text:
        return None
    match = re.search(r"(-?\d+)", str(rssi_text))
    if not match:
        return None
    try:
        return abs(int(match.group(1)))
    except Exception:
        return None
