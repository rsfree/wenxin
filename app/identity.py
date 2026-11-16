#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""匿名身份铸造（纯 HTTP，免登录、零生成请求）。

与 `docs/UPSTREAM.md §3` 同一套判据；CLI（`scripts/mint_identity.py`）与**面板**
（`POST /panel/api/mint`）共用这一份实现 —— 两处各写一遍必然漂移。

**两步采集**（实测 2026-11-16）：单入口只能拿到半套身份。
  · `www.baidu.com`   → `BAIDUID` / `BIDUPSID` / `PSTM`
  · `image.baidu.com` → `BAIDUID_BFESS`（须带着上一步的 cookie 去要，才会与 BAIDUID 对齐）

⚠️ 铸造换的是**身份**，不是**出口 IP**。门禁是出口维度的风险评分：
   出口被标记时，铸多少新身份都不解决（要换的是 `BAIDU_PROXY_POOL`）。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import time
from datetime import datetime, timezone

import httpx

__all__ = ["mint", "save_identity", "IDENTITY_PATH", "IDENTITY_FIELDS"]

#: 与客户端同款 UA（站点对 UA 有明显的分流行为）。
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")

#: 参与身份的 cookie 名（顺序即拼接顺序）。
IDENTITY_FIELDS = ("BAIDUID", "BIDUPSID", "BAIDUID_BFESS", "PSTM")

#: 采集顺序：先种子站拿基础身份，再带 cookie 去图片站补 `*_BFESS`。
MINT_STEPS = ("https://www.baidu.com", "https://image.baidu.com")

IDENTITY_PATH = pathlib.Path("var/identity.json")


async def mint(*, attempts: int = 2, backoff: float = 1.5,
               timeout: float = 30.0) -> tuple[str, list[str]]:
    """铸一枚匿名身份 ⇒ `(cookie 串, 字段名列表)`；失败抛 `RuntimeError`（带可行动信息）。"""
    last = ""
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout,
                                 trust_env=False, headers={"User-Agent": UA}) as client:
        for attempt in range(1, attempts + 1):
            picked: dict[str, str] = {}
            for step in MINT_STEPS:
                cookie_so_far = "; ".join(picked[f] for f in IDENTITY_FIELDS if f in picked)
                headers = {"Cookie": cookie_so_far} if cookie_so_far else {}
                try:
                    resp = await client.get(step + "/", headers=headers)
                except httpx.HTTPError as exc:
                    last = f"{step} 请求失败：{exc}"
                    continue
                for raw in resp.headers.get_list("set-cookie"):
                    pair = str(raw).split(";", 1)[0].strip()
                    name = pair.split("=", 1)[0].strip()
                    if name in IDENTITY_FIELDS and name not in picked:
                        picked[name] = pair
                if not resp.headers.get_list("set-cookie"):
                    last = f"{step} 无 Set-Cookie（HTTP {resp.status_code}）"

            if "BAIDUID" in picked:
                cookie = "; ".join(picked[f] for f in IDENTITY_FIELDS if f in picked)
                return cookie, sorted(picked)

            if attempt < attempts:
                await asyncio.sleep(backoff)
    raise RuntimeError(
        f"铸匿名身份失败：没有下发 BAIDUID（{last}）。"
        f"多为瞬时 WAF 拦截，稍后重试即可；持续失败则说明该出口被判死。"
    )


def save_identity(cookie: str, fields: list[str], elapsed_s: float,
                  path: pathlib.Path | None = None) -> dict:
    """落盘 `var/identity.json`（600）。返回写入的元数据。

    `path` 默认在**调用时**解析（不要写成默认参数）—— 默认参数在 import 期求值，
    测试里 monkeypatch `IDENTITY_PATH` 会失效，而且会真的写进仓库目录。
    """
    target = path or IDENTITY_PATH
    if not target.parent.is_dir():
        target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cookie": cookie,
        "fields": fields,
        "source": list(MINT_STEPS),
        "minted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_s": round(elapsed_s, 3),
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    target.chmod(0o600)
    return payload


def mint_blocking() -> tuple[str, list[str], float]:
    """CLI 用的同步入口（返回 cookie/字段/耗时）。"""
    t0 = time.time()
    cookie, fields = asyncio.run(mint())
    return cookie, fields, time.time() - t0
