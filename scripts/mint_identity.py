#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""铸造匿名身份（纯 HTTP，免登录、零生成请求），产物写入 `var/identity.json`。

与 `app/upstream/baidu/client.py::mint_anonymous_cookie()` **同一套判据**，区别只在
使用场景：那边是「老接口被拉黑时进程内自救」，这边是「部署前铸一枚可长期用的身份」。

判据来源（docs/UPSTREAM.md §3）：
  · `BAIDU_COOKIE` 是唯一硬凭据；免登录 ≠ 免 cookie；
  · 纯 HTTP 铸造的 cookie 即可跑通完整流程，**不需要浏览器、不需要 Turnstile**。

⚠️ **分两个入口采集**（实测 2026-11-16）：
  · `www.baidu.com`     → `BAIDUID` / `BIDUPSID` / `PSTM`
  · `image.baidu.com`   → `BAIDUID_BFESS`（须带着上一步的 cookie 去要，才会与 `BAIDUID` 对齐）
  单入口只能拿到半套身份，这就是「字段越少越容易被判可疑」的由来。

⚠️ 铸造换的是**身份**，不是**出口 IP**。门禁是出口维度的风险评分：出口被标记时，
   铸多少新身份都不解决（要换的是 `BAIDU_PROXY_POOL`）。

用法::

    python scripts/mint_identity.py                 # 铸一枚，写 var/identity.json
    python scripts/mint_identity.py --print-cookie  # 只打印 cookie（管道用）
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

#: 与 client.py 同款 UA（站点对 UA 有明显的分流行为）。
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")

#: 参与身份的 cookie 名（顺序即拼接顺序）。
_IDENTITY_FIELDS = ("BAIDUID", "BIDUPSID", "BAIDUID_BFESS", "PSTM")

#: 采集顺序：先种子站拿基础身份，再带 cookie 去图片站补 `*_BFESS`。
_MINT_STEPS = ("https://www.baidu.com", "https://image.baidu.com")

_OUT = pathlib.Path("var/identity.json")


def _collect(headers: list[str], into: dict[str, str]) -> None:
    """把 `Set-Cookie` 里的身份字段并进 `into`（**首次出现优先**，不覆盖已有值）。"""
    for raw in headers:
        pair = str(raw).split(";", 1)[0].strip()
        name, _, _val = pair.partition("=")
        name = name.strip()
        if name in _IDENTITY_FIELDS and name not in into:
            into[name] = pair


def _fetch(url: str, cookie: str) -> tuple[list[str], int]:
    headers = {"User-Agent": _UA}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url.rstrip("/") + "/", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return [str(h) for h in (resp.headers.get_all("Set-Cookie") or [])], resp.status


def mint(*, attempts: int = 2, backoff: float = 1.5) -> tuple[str, list[str]]:
    """铸一枚匿名身份；返回 `(cookie 串, 字段名列表)`。失败抛 `RuntimeError`。"""
    last = ""
    for attempt in range(1, attempts + 1):
        picked: dict[str, str] = {}
        for step in _MINT_STEPS:
            cookie_so_far = "; ".join(picked[f] for f in _IDENTITY_FIELDS if f in picked)
            try:
                raw_headers, status = _fetch(step, cookie_so_far)
            except Exception as exc:  # noqa: BLE001 - 网络层失败一律退避重试
                last = f"{step} 请求失败：{exc}"
                raw_headers, status = [], 0
            _collect(raw_headers, picked)
            if not raw_headers:
                last = f"{step} 无 Set-Cookie（HTTP {status}）"

        if "BAIDUID" in picked:
            cookie = "; ".join(picked[f] for f in _IDENTITY_FIELDS if f in picked)
            return cookie, sorted(picked)

        if attempt < attempts:
            print(f"  ! 未拿到 BAIDUID（{last}）—— {backoff}s 后重试", file=sys.stderr)
            time.sleep(backoff)
    raise RuntimeError(
        f"铸匿名身份失败：没有下发 BAIDUID（{last}）。"
        f"多为瞬时 WAF 拦截，稍后重试即可；持续失败则说明该出口被判死。"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="铸造文心匿名身份（免登录、零生成请求）")
    ap.add_argument("--print-cookie", action="store_true", help="只打印 cookie，不落盘")
    args = ap.parse_args()

    t0 = time.time()
    cookie, fields = mint()
    elapsed = time.time() - t0

    if args.print_cookie:
        print(cookie)
        return 0

    _OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cookie": cookie,
        "fields": fields,
        "source": _MINT_STEPS,
        "minted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_s": round(elapsed, 3),
    }
    _OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    _OUT.chmod(0o600)

    missing = [f for f in _IDENTITY_FIELDS if f not in fields]
    print(f"✓ 已铸造身份（{elapsed:.3f}s，{len(fields)} 项：{'/'.join(fields)}）")
    if missing:
        print(f"  ⚠️ 未取到：{'/'.join(missing)}（仍可用，但身份可信度偏低）")
    print(f"  落盘：{_OUT}（已 600，var/ 在 .gitignore 内）")
    print("  ⚠️ 铸造换的是身份，不是出口 IP；出口被标记时仍需 BAIDU_PROXY_POOL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
