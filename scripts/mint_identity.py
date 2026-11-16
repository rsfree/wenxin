#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""铸造匿名身份（纯 HTTP，免登录、零生成请求），产物写入 `var/identity.json`。

实现在 `app/identity.py` —— **CLI 与面板共用同一份**，避免两处逻辑漂移。
本文件只做三件事：解析参数、调 `mint()`、打印人能读的结论。

用法::

    python scripts/mint_identity.py                 # 铸一枚，写 var/identity.json
    python scripts/mint_identity.py --print-cookie  # 只打印 cookie（管道用）
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.identity import IDENTITY_FIELDS, IDENTITY_PATH, mint_blocking, save_identity  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="铸造文心匿名身份（免登录、零生成请求）")
    ap.add_argument("--print-cookie", action="store_true", help="只打印 cookie，不落盘")
    args = ap.parse_args()

    try:
        cookie, fields, elapsed = mint_blocking()
    except RuntimeError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    if args.print_cookie:
        print(cookie)
        return 0

    save_identity(cookie, fields, elapsed)
    missing = [f for f in IDENTITY_FIELDS if f not in fields]
    print(f"✓ 已铸造身份（{elapsed:.3f}s，{len(fields)} 项：{'/'.join(fields)}）")
    if missing:
        print(f"  ⚠️ 未取到：{'/'.join(missing)}（仍可用，但身份可信度偏低）")
    print(f"  落盘：{IDENTITY_PATH}（已 600，var/ 在 .gitignore 内）")
    print("  ⚠️ 铸造换的是身份，不是出口 IP；出口被标记时仍需 BAIDU_PROXY_POOL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
