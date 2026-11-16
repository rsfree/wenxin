#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进程内用量记录：每个模型的调用数 / 成功率 / p50 延迟，以及最近若干条调用明细。

**为什么是进程内**：本服务单 worker 是架构决定（速率闸门与冷却窗都是进程内状态），
所以用量与它们同源、同生命周期。多 worker 时这里会各记各的 —— 那是部署问题，
不该在面板里假装是全局计数（"看起来对、加起来不对"比没有更坏）。

**为什么不用数据库**：这个服务的排障问题是「刚才那次为什么 502」，
答案是最近 200 条调用 + 每个模型的错误分布。落盘库在这里是过度设计，
且会让单 worker 假设悄悄变成"多实例共享"的错觉。
"""

from __future__ import annotations

import statistics
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any

__all__ = ["record", "snapshot", "reset", "_RECENT_CAP"]

#: 最近调用明细的容量。200 条 ≈ 一小时的正常流量，够复盘"刚才那几次"。
_RECENT_CAP = 200
#: 每个模型保留的延迟样本（算 p50）。与明细分开：明细有容量上限，p50 要看全量趋势。
_LAT_CAP = 200

_RECENT: deque[dict[str, Any]] = deque(maxlen=_RECENT_CAP)


@dataclass
class _Agg:
    calls: int = 0
    ok: int = 0
    dry: int = 0
    errors: Counter = field(default_factory=Counter)
    lat: deque = field(default_factory=lambda: deque(maxlen=_LAT_CAP))

    def add(self, *, ok: bool, dry: bool, ms: float, error: str) -> None:
        self.calls += 1
        self.ok += int(ok)
        self.dry += int(dry)
        if error:
            self.errors[error] += 1
        if not dry and ok:                     # 干跑没有上游耗时，混进 p50 会污染
            self.lat.append(ms)


_AGG: dict[str, _Agg] = {}


def record(*, model: str, title: str = "", ok: bool, ms: float,
           dry: bool = False, path: str = "", error: str = "") -> None:
    """记一次调用。**失败也要记** —— 只记成功的计数器会让人误以为一切正常。"""
    _AGG.setdefault(model, _Agg()).add(ok=ok, dry=dry, ms=ms, error=error)
    _RECENT.append({
        "t": time.strftime("%H:%M:%S"),
        "model": model,
        "title": title,
        "ok": ok,
        "dry": dry,
        "ms": round(ms, 1),
        "path": path,
        "error": error,
    })


def _p50(values: deque) -> float | None:
    return round(statistics.median(values), 1) if values else None


def snapshot(limit: int = 50) -> dict[str, Any]:
    """面板用的快照：总览 + 每模型 + 最近 `limit` 条（新的在前）。"""
    total_calls = sum(a.calls for a in _AGG.values())
    total_ok = sum(a.ok for a in _AGG.values())
    real = [ms for a in _AGG.values() for ms in a.lat]
    models = [
        {
            "model": name,
            "calls": a.calls,
            "ok": a.ok,
            "failed": a.calls - a.ok,
            "dry": a.dry,
            "ok_rate": round(a.ok / a.calls, 3) if a.calls else None,
            "p50_ms": _p50(a.lat),
            "errors": dict(a.errors.most_common(4)),
        }
        for name, a in sorted(_AGG.items(), key=lambda kv: -kv[1].calls)
    ]
    return {
        "summary": {
            "calls": total_calls,
            "ok": total_ok,
            "failed": total_calls - total_ok,
            "ok_rate": round(total_ok / total_calls, 3) if total_calls else None,
            "p50_ms": round(statistics.median(real), 1) if real else None,
            "models_seen": len(_AGG),
        },
        "models": models,
        "recent": list(_RECENT)[-max(1, min(limit, _RECENT_CAP)):][::-1],
        "capacity": _RECENT_CAP,
    }


def reset() -> None:
    """清空（测试用）。"""
    _AGG.clear()
    _RECENT.clear()
