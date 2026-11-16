#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""风控与容量实测档案 —— 管理台「风控」页的数据源。

**这是"证据"而不是"配置"**：每条数字都对应一次真实压测（机器、时间、速率、结果），
来源是 `docs/UPSTREAM.md` §11（编辑链路）与 §13（文生图）。改这里的数字等于篡改证据，
必须同时在文档里补实验记录。

为什么不放进 .env：闸门参数（`MIN_INTERVAL` / `PER_MINUTE`）是**运行时配置**，
本模块是**历史测量**。两者混在一起，会让"我们现在能跑多快"和"上次测到什么"互相污染。
"""

from __future__ import annotations

from typing import Any

__all__ = ["MEASURED_AT", "EXPERIMENTS", "FINDINGS", "payload"]

#: 最近一次压测日期（与 docs/UPSTREAM.md 的章节日期一致）
MEASURED_AT = "2026-11-17"

#: 单条实验：一个身份、一档速率、一次结果。
#: `outcome` 取值：`clean`（零风控）/ `hit`（命中 risk_control）/ `hard`（出口已打标，首发即拒）
EXPERIMENTS: tuple[dict[str, Any], ...] = (
    # ---- 编辑链路（node-4，61.111.253.121）----
    {"date": "2026-11-17", "channel": "编辑", "host": "node-4",
     "identity": "cookie-1", "rate": 53, "sent": 62, "ok": 55, "outcome": "hit",
     "note": "满并发（驱动器节流 bug 的意外高值）；命中后 17 分钟仍拒"},
    {"date": "2026-11-17", "channel": "编辑", "host": "node-4",
     "identity": "cookie-2", "rate": 25, "sent": 31, "ok": 29, "outcome": "hit",
     "note": "72.5s 后命中"},
    {"date": "2026-11-17", "channel": "编辑", "host": "node-4",
     "identity": "cookie-3", "rate": 12, "sent": 1, "ok": 0, "outcome": "hard",
     "note": "出口已打标：全新身份首发即拒"},
    # ---- 文生图（node-5，61.111.253.131）----
    {"date": "2026-11-17", "channel": "文生图", "host": "node-5",
     "identity": "cookie-5", "rate": 12, "sent": 36, "ok": 36, "outcome": "clean",
     "note": "3 分钟零风控；单发 p50≈21s，每发 4 张"},
    {"date": "2026-11-17", "channel": "文生图", "host": "node-5",
     "identity": "cookie-5（已用 36）", "rate": 24, "sent": 20, "ok": 19, "outcome": "hit",
     "note": "累计 55 发命中"},
    {"date": "2026-11-17", "channel": "文生图", "host": "node-5",
     "identity": "cookie-6", "rate": 18, "sent": 45, "ok": 44, "outcome": "hit",
     "note": "146.7s / 44 发命中"},
    {"date": "2026-11-17", "channel": "文生图", "host": "node-5",
     "identity": "cookie-7", "rate": 24, "sent": 1, "ok": 0, "outcome": "hard",
     "note": "出口已打标：全新身份首发即拒"},
)

#: 结论（面板直接展示，措辞与 docs 一致）
FINDINGS: tuple[str, ...] = (
    "不存在「每分钟 N 次」阈值：两条链路、两台机器的触发量都落在 29–55 发，"
    "而速率相差 2 倍以上 ⇒ 更像**每身份累计预算 ≈45–55 发**。",
    "生产默认 8 次/分、4s 最小间隔，低于所有未打标状态的触发点，余量约 1.5–3 倍。",
    "换身份只能重置一轮预算；**两轮之后出口被硬打标**，新铸身份首发即拒"
    "（node-4 / node-5 各复现一次）。",
    "命中后停手并不会快速恢复：编辑链路 17 分钟仍被拒；服务侧 600s 冷却窗是下限而非上限。",
    "**唯一可持续的换线手段是真出口**（BAIDU_PROXY_POOL）；伪造 XFF 未取证。",
    "主链被判死时老接口仍可用（同刻同 IP 旧/新身份均出图）⇒ `BAIDU_LEGACY=fallback` 是有依据的兜底。",
)

#: 未取证项（别当成已知）
UNKNOWN: tuple[str, ...] = (
    "伪造 X-Forwarded-For / X-Real-IP 是否影响评分（未做隔离实验）",
    "预算是否按 24h 滑窗重置（未等到重置窗口）",
    "换真出口是否立即恢复（两台机器各只有单个公网 IP，无第二出口）",
    "不同机房/AS 是否同一预算尺度（只测了首尔 MOACK 与法兰克福 NetLab 的两台）",
)


def payload() -> dict[str, Any]:
    """给面板的静态部分（实时闸门状态由调用方另外拼）。"""
    return {
        "measured_at": MEASURED_AT,
        "experiments": [dict(row) for row in EXPERIMENTS],
        "findings": list(FINDINGS),
        "unknown": list(UNKNOWN),
    }
