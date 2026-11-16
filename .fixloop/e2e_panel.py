#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E2E 判据：本地部署 + 管理面板，用 bsk 驱动真实 Chromium。

一条命令，只认退出码：

    python3 .fixloop/e2e_panel.py            # 默认（干跑，零上游）
    python3 .fixloop/e2e_panel.py --real     # 追加真跑一项（真实上游，约 5-7s）
    python3 .fixloop/e2e_panel.py --mutation # 自检：故意把断言改错，必须返回非 0

设计约束：
- **不猜 ref**：每步都从当轮 VOM 里按可见文本找 `@eN`，不硬编码（bsk 的 ref 每次同步都会变）。
- **不确定就同步**：任何动作失败后必 `bsk sync` 再决策，绝不盲目重放（bsk 安全约束）。
- **留痕**：每次运行追加 `.fixloop/runs.ndjson`，全文进 `.fixloop/logs/rNN.log`。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Any

BASE = "http://127.0.0.1:8700"
PROBE_PORT = 8799
PROBE_DIR = pathlib.Path("/tmp/fixloop-web")
IMAGE_URL = f"http://127.0.0.1:{PROBE_PORT}/logo-e2e.jpg"
SCOPE = "fixloop"
ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXLOOP = ROOT / ".fixloop"
LOGS = FIXLOOP / "logs"
RUNS = FIXLOOP / "runs.ndjson"

REF_RE = re.compile(r'(@e\d+)\s+[a-zA-Z]+\s+"([^"]*)"')

_env = dict(os.environ)
_env["PATH"] = f"{pathlib.Path.home()}/.local/bin:" + _env.get("PATH", "")


class StepError(RuntimeError):
    pass


def start_probe() -> subprocess.Popen | None:
    """起一个只读静态图床（真实调用要上游来抓这张图）。已在跑则复用。"""
    import socket
    import shutil

    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", PROBE_PORT)) == 0:
            return None
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    src = ROOT / "testdata" / "logo-404.jpg"
    shutil.copyfile(src, PROBE_DIR / "logo-e2e.jpg")
    proc = subprocess.Popen([sys.executable, "-m", "http.server", str(PROBE_PORT),
                             "--bind", "127.0.0.1"], cwd=PROBE_DIR,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    return proc


def bsk(*args: str, timeout: float = 60) -> tuple[int, str]:
    proc = subprocess.run(["bsk", *args, "--scope", SCOPE], capture_output=True,
                          text=True, timeout=timeout, env=_env)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def must(*args: str, what: str = "", timeout: float = 60) -> str:
    code, out = bsk(*args, timeout=timeout)
    if code != 0:
        # bsk 的安全约束：动作失败后必须先同步，否则后续动作会被台账拦住
        bsk("sync")
        raise StepError(f"{what or args[0]} 失败（rc={code}）：{out.strip().splitlines()[0] if out.strip() else '无输出'}")
    return out


def find_ref(vom: str, needle: str) -> str:
    for ref, text in REF_RE.findall(vom):
        if needle in text:
            return ref
    raise StepError(f"页面里找不到含「{needle}」的可交互节点（VOM 可能未同步）")


def wait_for(vom: str, needle: str, *, where: str, timeout: float = 30.0,
             interval: float = 1.0) -> str:
    """同步轮询直到 `needle` 出现。**单次 sync 不足以等异步结果**——
    真实调用约 5s，点完立刻快照会拍到「尚未运行」。"""
    deadline = time.time() + timeout
    while True:
        if needle in vom:
            return vom
        if time.time() >= deadline:
            raise StepError(f"{where}：等待「{needle}」超时（{timeout:.0f}s）")
        time.sleep(interval)
        vom = must("sync", what="轮询同步", timeout=max(30.0, timeout))


def set_checkbox(vom: str, needle: str, want: bool) -> str:
    """幂等设置复选框：**先读状态**再决定点不点（冷启后状态会重置，盲点必错）。"""
    for line in vom.splitlines():
        m = re.search(r"(@e\d+)\s+checkbox\s+\"[^\"]*" + re.escape(needle), line)
        if not m:
            continue
        state = "[checked]" in line
        if state == want:
            return vom
        must("click", m.group(1), what=f"切换「{needle}」→ {want}")
        return must("sync", what="切换复选框后同步")
    raise StepError(f"页面里找不到复选框「{needle}」")


def click_text(vom: str, needle: str) -> str:
    ref = find_ref(vom, needle)
    must("click", ref, what=f"点击「{needle}」")
    return must("sync", what="点击后同步")


def require(vom: str, needle: str, where: str) -> None:
    if needle not in vom:
        raise StepError(f"{where}：VOM 里缺少「{needle}」")


def run_flow(real: bool, mutation: bool) -> list[str]:
    """返回已通过的检查项名；任何一项不满足直接抛 StepError。"""
    done: list[str] = []

    bsk("close")            # 先清掉可能残留的会话（没有则忽略失败）

    vom = must("open", f"{BASE}/panel", what="打开面板")
    require(vom, "wenxin-service", "外壳")
    require(vom, "版本", "外壳")
    done.append("外壳加载")

    # ---- R2 总览（能力数与 /readyz 对账，不硬编码——降级/放开都不该改判据）----
    require(vom, "OK", "总览")
    import urllib.request
    with urllib.request.urlopen(f"{BASE}/readyz", timeout=10) as resp:
        caps = json.load(resp)["checks"]["capabilities"]
    expect = f"{caps['available']} / {caps['total']}"
    require(vom, expect, "总览")
    if "not-yet-fetched" in vom:
        raise StepError("总览：凭据来源仍是原始枚举 not-yet-fetched（未做可读化）")
    if not any(x in vom for x in ("尚未现取", "已从首页现取", "显式配置")):
        raise StepError("总览：凭据来源未做可读化（既非‘尚未现取’也非‘已从首页现取’）")
    done.append("R2 总览状态与能力面")

    # ---- R3 能力页 ----
    vom = click_text(vom, "能力")
    require(vom, "clarity", "能力页")
    require(vom, "变清晰", "能力页")
    require(vom, "可用", "能力页")
    if "**" in vom:
        raise StepError("能力页：取证列仍含 markdown '**'（未剥净）")
    done.append("R3 能力表")

    # ---- R4 测试台干跑 ----
    vom = click_text(vom, "测试台")
    require(vom, "粘贴图片 URL", "测试台")          # ← 首轮必然在这里失败（尚未实现）
    url_ref = find_ref(vom, "图片URL")
    must("fill", url_ref, "--value", IMAGE_URL, what="填图片 URL")
    vom = must("sync", what="填 URL 后同步")
    require(vom, IMAGE_URL, "测试台")               # 让价值回显到页面，便于断言

    vom = set_checkbox(vom, "干跑开关", True)
    run_ref = find_ref(vom, "运行")
    must("click", run_ref, what="点击运行", timeout=120)
    vom = wait_for(vom, "未触上游", where="结果区", timeout=30)
    # 断言必须落在**结果区专有**的文字上：干跑标记 + 零上游说明 + 通路标识
    require(vom, "未触上游", "结果区")
    require(vom, "dry-run", "结果区")
    require(vom, "变清晰" if not mutation else "必然不存在的字串", "结果区")
    done.append("R4 干跑链路")

    # ---- R6 日志页 ----
    vom = click_text(vom, "日志")
    require(vom, "BAIDU_", "日志页")
    done.append("R6 日志页")

    # ---- R8 hash 直达（冷启：bsk 对已存在的会话再 open 需要显式 observation）----
    bsk("close")
    vom = must("open", f"{BASE}/panel#try", what="hash 直达")
    require(vom, "粘贴图片 URL", "hash 直达")
    done.append("R8 hash 直达页签")

    # ---- R5 真跑（可选） ----
    if real:
        vom = click_text(vom, "测试台")
        url_ref = find_ref(vom, "图片URL")
        must("fill", url_ref, "--value", IMAGE_URL, what="填图片 URL")
        vom = must("sync", what="同步")
        vom = set_checkbox(vom, "干跑开关", False)   # 关掉干跑才是真实调用
        run_ref = find_ref(vom, "运行")
        must("click", run_ref, what="真实运行", timeout=180)
        vom = wait_for(vom, "conversation", where="真实结果", timeout=120)
        require(vom, "conversation", "真实结果")
        require(vom, "1616x1616", "真实结果")
        done.append("R5 真实出图")

    bsk("close")
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="追加真实上游一轮")
    ap.add_argument("--mutation", action="store_true", help="自检：断言故意写错，必须失败")
    args = ap.parse_args()

    LOGS.mkdir(parents=True, exist_ok=True)
    round_no = sum(1 for _ in RUNS.open()) + 1 if RUNS.exists() else 1
    log_path = LOGS / f"r{round_no:02d}.log"

    probe_proc = start_probe()
    t0 = time.time()
    passed: list[str] = []
    err = ""
    try:
        passed = run_flow(args.real, args.mutation)
        code = 0
    except StepError as exc:
        err = str(exc)
        code = 1
        try:
            code2, last = bsk("sync")
            (LOGS / f"r{round_no:02d}.vom.txt").write_text(last, encoding="utf-8")
        except Exception:  # noqa: BLE001 - 现场抓不到就算了，不掩盖真实失败
            pass
    except Exception as exc:  # noqa: BLE001 - 判据自身异常也算失败
        err = f"{type(exc).__name__}: {exc}"
        code = 1
    finally:
        bsk("close")
        if probe_proc is not None:
            probe_proc.terminate()

    elapsed = round(time.time() - t0, 1)
    log_path.write_text(
        f"round={round_no}\nreal={args.real} mutation={args.mutation}\n"
        f"elapsed={elapsed}s exit={code}\n\npassed:\n" +
        "\n".join(f"  - {p}" for p in passed) +
        f"\n\nerror:\n  {err or '（无）'}\n", encoding="utf-8")

    record: dict[str, Any] = {
        "round": round_no, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "cmd": f"e2e_panel.py{' --real' if args.real else ''}{' --mutation' if args.mutation else ''}",
        "exit_code": code, "passed": len(passed),
        "failed": 0 if code == 0 else 1,
        "first_error": (err.splitlines()[0] if err else ""),
        "log_path": str(log_path.relative_to(ROOT)),
        "git_head": subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                                   capture_output=True, text=True).stdout.strip(),
        "attribution": "" if code == 0 else "IMPL",
        "decision": "DONE" if code == 0 else "FIX",
    }
    with RUNS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"round {round_no} · exit={code} · {elapsed}s")
    for p in passed:
        print(f"  PASS {p}")
    if err:
        print(f"  FAIL {err}")
    print(f"  log → {log_path.relative_to(ROOT)}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
