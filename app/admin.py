#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内嵌式管理面板：单文件前端（无构建）+ **零重复逻辑**的后端。

设计约束（与兄弟服务 workbuddy2api 同源，但语义按本服务裁剪）：

1. **不复制业务逻辑**。面板的「试跑」直接打既有的 `POST /v1/images/generations`
   —— 校验 / 闸门 / 错误信封 / 干跑全走同一条路径。任何在面板里另写一份
   `validate_request` 的做法，都会在下次改契约时悄悄漂移。
2. **不复制观测数据**。总览读既有的 `/healthz` + `/readyz`（它们本就是给外部探针的），
   能力表读既有的 `/capabilities`。面板只补两样别人没有的：
   HTML 外壳，和**日志环形缓冲**（进程内内存，最多 `_LOG_CAP` 条）。
3. **HTML 本身不含秘密**，可匿名加载；`/panel/api/*` 与 `/v1/*` 用**同一口径**的
   Bearer 鉴权（`API_KEYS`，空 = 关闭）。这是 workbuddy2api 面板的既定做法：
   前端问用户要 key，只发给 api 端点。
4. **单 worker 假设不变**：日志缓冲与速率闸门一样是进程内状态。

⚠️ 反代部署时 `/panel` 与 `/stats` 同属运维面，应当一并挡住公网。
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from loguru import logger

__all__ = ["mount_admin", "recent_logs", "reset_logs"]

_PANEL_HTML = Path(__file__).parent / "panel" / "index.html"

#: 日志环形缓冲容量。2000 条 ≈ 几 MB 的上限，够定位「刚才那次为什么 502」。
_LOG_CAP = 2000

#: `{时间, 级别, 消息}` —— 只留这三样：诊断够用，且**天然不含凭据**
#: （logger 侧已有脱敏；cookie/token 从不进日志文本）。
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
