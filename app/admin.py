#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内嵌式管理面板：单文件前端（无构建）+ **不复制业务逻辑**的后端。

设计约束（与兄弟服务 workbuddy2api 同源，但语义按本服务裁剪）：

1. **不复制业务逻辑**。面板的「试跑」「一键干跑」都走既有的 `service.run()`
   —— 校验 / 闸门 / 错误信封 / 干跑全走同一条路径。面板里另写一份
   `validate_request` 或另拼一份出站请求，都会在下次改契约时悄悄漂移。
2. **不复制观测数据**。总览读既有的 `/healthz` + `/readyz`，能力表读 `/capabilities`。
   面板只补三样别人没有的：HTML 外壳、**日志环形缓冲**、**进程内用量**。
3. **HTML 本身不含秘密**，可匿名加载；`/panel/api/*` 与 `/v1/*` 用**同一口径**的
   Bearer 鉴权（`API_KEYS`，空 = 关闭）。密钥由前端向使用者索取，只发给 api 端点。
4. **单 worker 假设不变**：日志缓冲、用量、速率闸门都是进程内状态。
5. **配置只读**。改 `.env` 后重启才是唯一真实的配置路径 —— 面板热改会让
   "改了但没生效"变成常态（单 worker 下闸门/冷却窗是进程内状态，热改最多改一半）。

⚠️ 反代部署时 `/panel` 与 `/stats` 同属运维面，应当一并挡住公网。
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from loguru import logger

from . import identity, usage
from .errors import ApiError, UpstreamError
from .models import CAPABILITIES, availability
from .service import run, validate_request

__all__ = ["mount_admin", "recent_logs", "reset_logs"]

_PANEL_HTML = Path(__file__).parent / "panel" / "index.html"

#: 日志环形缓冲容量。2000 条 ≈ 几 MB 的上限，够定位「刚才那次为什么 502」。
_LOG_CAP = 2000

#: 1×1 透明 PNG —— 一键干跑用的占位输入（干跑不会把它送上游）。
_TINY_PNG = ("data:image/png;base64,"
             "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

_LOGS: deque[dict[str, str]] = deque(maxlen=_LOG_CAP)

_sink_installed = False


def _sink(message: Any) -> None:
    """loguru 的**可调用 sink**：把每条记录塞进环形缓冲。"""
    record = message.record
    _LOGS.append({
        "t": record["time"].strftime("%H:%M:%S"),
        "level": record["level"].name,
        "msg": record["message"],
    })


def _install_sink() -> None:
    """装一次即可。`create_app` 在测试里会被调很多次，重复装会**成倍复制日志**。"""
    global _sink_installed
    if _sink_installed:
        return
    logger.add(_sink, level="INFO", format="{message}", colorize=False, enqueue=False)
    _sink_installed = True


def recent_logs(limit: int = 300) -> list[dict[str, str]]:
    """最近 `limit` 条日志（先到先出）。测试与面板共用。"""
    n = max(1, min(int(limit), _LOG_CAP))
    return list(_LOGS)[-n:]


def reset_logs() -> None:
    """清空缓冲（测试用；运行期不需要这个动作）。"""
    _LOGS.clear()


def mount_admin(app: FastAPI, require_key: Callable[..., Any]) -> None:
    """挂上 `/panel` 与 `/panel/api/*`。

    `require_key` 由 `create_app` 传入（就是 `require_api_key` 本身）——
    用参数注入而不是 import，避免 `main ↔ admin` 的循环导入。
    """
    _install_sink()

    @app.get("/panel", include_in_schema=False)
    async def panel_html() -> HTMLResponse:
        """面板外壳。**免鉴权**：HTML 里没有任何秘密，密钥在前端向用户索取。"""
        return HTMLResponse(_PANEL_HTML.read_text(encoding="utf-8"))

    @app.get("/panel/api/logs")
    async def panel_logs(request: Request, n: int = 300,
                         _: str = Depends(require_key)) -> dict[str, Any]:
        """最近日志（运维面，需 Key）。"""
        return {"capacity": _LOG_CAP, "count": len(_LOGS), "items": recent_logs(n)}

    # ------------------------------------------------------------------ 用量
    @app.get("/panel/api/usage")
    async def panel_usage(request: Request, n: int = 50,
                          _: str = Depends(require_key)) -> dict[str, Any]:
        """进程内用量：总量 / 每模型成功率与 p50 / 最近调用明细。"""
        return usage.snapshot(limit=n)

    # ------------------------------------------------- 一键干跑（零上游自检）
    @app.post("/panel/api/dryrun-all")
    async def panel_dryrun_all(request: Request,
                               _: str = Depends(require_key)) -> dict[str, Any]:
        """把注册表里**每一项**都走一遍干跑，给出"当前部署谁真能跑"的自检表。

        零上游：`dry_run=True` 在 `service.run()` 里走的是**预览分支**，
        不建连、不占闸门名额。默认被门禁挡住的能力会如实回 503 原因，不伪装成通过。
        """
        state = request.app.state
        legacy_on = state.settings.LEGACY != "off"
        rows: list[dict[str, Any]] = []
        for name in sorted(CAPABILITIES):
            cap = CAPABILITIES[name]
            # 🔴 干跑**按设计穿透门禁**（零成本预览也要能看计划），所以"当前部署能不能真跑"
            # 必须另外用 availability() 判 —— 从干跑结果里读门禁只会得到"全都可跑"的假象。
            usable, reason = availability(cap, allow_unverified=state.settings.ALLOW_UNVERIFIED,
                                         legacy=legacy_on)
            payload: dict[str, Any] = {"model": name, "image": _TINY_PNG, "dry_run": True}
            if cap.requires_mask:
                payload["mask"] = _TINY_PNG
            if cap.needs_style and cap.style_table:
                payload["style"] = cap.style_table[0][0]
            if cap.supports_ratio:
                payload["size"] = "4:3"

            started = time.perf_counter()
            row: dict[str, Any] = {"model": name, "title": cap.title,
                                   "available": usable, "gated_reason": reason}
            try:
                parsed = validate_request(payload)
                (cap_obj, image_value, mask_value, style_value, size, prompt,
                 response_format, dry, unsupported) = parsed
                body = await run(
                    settings=state.settings, client=state.client, gate=state.gate,
                    risk=state.risk, cap=cap_obj, image_value=image_value,
                    mask_value=mask_value, style_value=style_value, size=size,
                    prompt=prompt, response_format=response_format, dry_run=True,
                    unsupported=unsupported)
                preview = body.get("preview") or {}
                row.update(ok=True, target=preview.get("would_post_to"),
                           legacy=bool(preview.get("legacy")))
            except (ApiError, UpstreamError) as exc:
                code = exc.code if isinstance(exc, ApiError) else exc.kind
                row.update(ok=False, error=code,
                           reason=(exc.message if isinstance(exc, ApiError) else str(exc))[:160])
            except Exception as exc:  # noqa: BLE001 - 自检要能报告，不要整页 500
                row.update(ok=False, error="unexpected", reason=f"{type(exc).__name__}: {exc}"[:160])
            row["ms"] = round((time.perf_counter() - started) * 1000, 1)
            rows.append(row)

        preview_ok = [r for r in rows if r["ok"]]
        usable = [r for r in rows if r["available"]]
        return {
            "total": len(rows),
            "preview_ok": len(preview_ok),        # 请求能构造（干跑无一例外）
            "available": len(usable),             # 本部署**真能跑**
            "gated": len(rows) - len(usable),     # 被门禁/未取证挡住
            "failed": len(rows) - len(preview_ok),
            "rows": rows,
        }

    # ------------------------------------------------------- 身份铸造（换身份）
    @app.post("/panel/api/mint")
    async def panel_mint(request: Request,
                         _: str = Depends(require_key)) -> dict[str, Any]:
        """现场铸一枚匿名身份并**在进程内立刻生效**（落盘 `var/identity.json`）。

        ⚠️ 两件必须说清的事（面板上也这么显示）：
          · 只换**身份**，不换**出口 IP** —— 出口被标记时铸新身份无效；
          · 热生效是进程内的，**重启后回退到 `.env` 的 `BAIDU_COOKIE`**；
            要持久化请把返回的 cookie 写进 `.env`。
        """
        state = request.app.state
        started = time.perf_counter()
        try:
            cookie, fields = await identity.mint()
        except RuntimeError as exc:
            raise ApiError(502, "mint_failed", str(exc)) from exc
        elapsed = time.perf_counter() - started

        state.settings.COOKIE = cookie          # 进程内热生效
        state.client.creds.reset()              # 旧 token/lid 与新身份不配对，必须丢
        meta = identity.save_identity(cookie, fields, elapsed)

        logger.info("面板铸造新身份：{} 项字段，{:.2f}s（进程内已生效，重启回退 .env）",
                    len(fields), elapsed)
        return {
            "ok": True,
            "fields": fields,
            "minted_at": meta["minted_at"],
            "elapsed_s": round(elapsed, 3),
            "persisted": str(identity.IDENTITY_PATH),
            "applied": "in-process",
            "note": ("已在本进程生效；重启后回退到 .env 的 BAIDU_COOKIE。"
                     "只换身份不换出口 —— 出口被标记时仍需 BAIDU_PROXY_POOL。"),
        }
