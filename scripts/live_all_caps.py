#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把站点工具栏的 11 项能力**全量真跑**一遍，产出报告 + 结果图。

⚠️ **真实调用**（文心免登录、零费用，但**别连打**）：串行 + 遵循服务端闸门
（`BAIDU_MIN_INTERVAL` / `PER_MINUTE`），遇到 429 按 `Retry-After` 退避重试。

覆盖清单（= 站点工具栏，2026-11-16 截图）：变清晰 / 消除 / 抠图 / 扩图 / 局部替换 /
文字替换 / 图片修复 / 换风格 / AI重绘 / 相似图 / 提线稿。
其余能力（滤镜 / 美颜 / 去文字 / 去水印 / 背景替换）不在工具栏，另有覆盖。

用法::

    python scripts/live_all_caps.py --image testdata/logo-404.jpg
    python scripts/live_all_caps.py --image ... --mask testdata/mask-tagline.png --only clarity,matting
    python scripts/live_all_caps.py --image ... --base http://127.0.0.1:8700 --out testdata/all
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import time
import urllib.error
import urllib.request
from typing import Any

#: 响应 mime → 落盘扩展名（WebP 是抠图的默认形态，别当 jpg）。
_EXT_BY_MIME = {"image/webp": "webp", "image/png": "png", "image/jpeg": "jpg"}

#: 与站点工具栏一一对应。`extra` 里的 `__MASK__` 由 `--mask` 注入。
CASES: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
    ("clarity", "变清晰", "jd", {}),
    ("erase", "消除", "sc", {"__MASK__": True, "prompt": "（无）"}),
    ("matting", "抠图", "kt", {}),
    ("expand", "扩图", "kt2", {"size": "16:9"}),
    ("replace", "局部替换", "jbth", {"__MASK__": True, "prompt": "一条深蓝色的细横线"}),
    ("textreplace", "文字替换", "wzth", {}),
    ("restore", "图片修复", "tpxf", {}),
    ("restyle", "换风格", "hfg", {"style": "油画风"}),
    ("redraw", "AI重绘", "aihc", {}),
    ("similar", "相似图", "xst", {}),
    ("sketch", "提线稿", "txg", {}),
)


def _data_uri(path: pathlib.Path) -> str:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def _post(base: str, body: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any], dict[str, str]]:
    req = urllib.request.Request(
        f"{base.rstrip('/')}/v1/images/generations",
        data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r), dict(r.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw), dict(exc.headers)
        except ValueError:
            return exc.code, {"error": {"message": raw[:300]}}, dict(exc.headers)


def _run_one(base: str, model: str, body: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any]]:
    """带 429 退避的调用（闸门是服务端的事，客户端只负责礼让）。"""
    for attempt in range(3):
        code, payload, headers = _post(base, body, timeout)
        if code != 429 or attempt == 2:
            return code, payload
        wait = float(headers.get("retry-after") or 10)
        print(f"      429 限速，{wait:.0f}s 后重试（{attempt + 1}/2）")
        time.sleep(min(wait + 1, 60))
    return 429, {"error": {"message": "重试耗尽"}}


def main() -> int:
    ap = argparse.ArgumentParser(description="站点工具栏 11 项能力全量真跑")
    ap.add_argument("--image", required=True, type=pathlib.Path)
    ap.add_argument("--mask", type=pathlib.Path, help="黑底白框遮罩（消除/局部替换用）")
    ap.add_argument("--base", default="http://127.0.0.1:8700")
    ap.add_argument("--out", default=pathlib.Path("testdata/all"), type=pathlib.Path)
    ap.add_argument("--only", default="", help="逗号分隔，只跑子集（如 clarity,matting）")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--dry", action="store_true", help="只干跑（零上游），不产图")
    args = ap.parse_args()

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    args.out.mkdir(parents=True, exist_ok=True)
    image_uri = _data_uri(args.image)
    mask_uri = _data_uri(args.mask) if args.mask else ""

    rows: list[dict[str, Any]] = []
    for idx, (short, title, tag, extra) in enumerate(CASES, 1):
        model = f"wenxin:{short}"
        if only and short not in only:
            continue
        body: dict[str, Any] = {"model": model, "image": image_uri}
        for k, v in extra.items():
            if k == "__MASK__":
                if mask_uri:
                    body["mask"] = mask_uri
                continue
            body[k] = v

        print(f"[{idx:02d}/{len(CASES)}] {title}（{model}）…", end=" ", flush=True)
        t0 = time.time()
        if args.dry:
            code, payload = _run_one(args.base, model, {**body, "dry_run": True}, args.timeout)
        else:
            code, payload = _run_one(args.base, model, body, args.timeout)
        elapsed = time.time() - t0

        row: dict[str, Any] = {"model": model, "title": title, "code": code,
                               "elapsed_s": round(elapsed, 1)}
        if code == 200:
            item = (payload.get("data") or [{}])[0]
            row["size"] = item.get("size")
            row["upstream_path"] = (payload.get("upstream") or {}).get("path")
            row["warnings"] = payload.get("warnings") or []
            blob = item.get("b64_json")
            if blob and not args.dry:
                raw = base64.b64decode(blob)
                # 按**响应里的 mime** 定扩展名：抠图返回的是 WebP（带 alpha），
                # 一律当 jpg 存会得到"能打开但扩展名错"的文件（实测踩过）。
                ext = _EXT_BY_MIME.get(str(item.get("mime") or ""), "jpg")
                if not ext:
                    ext = "png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"
                fp = args.out / f"{idx:02d}-{short}{'-dry' if args.dry else ''}.{ext}"
                fp.write_bytes(raw)
                row["file"] = str(fp)
                row["bytes"] = len(raw)
            print(f"✓ {elapsed:.1f}s {row.get('size') or ''}")
        else:
            err = payload.get("error") or {}
            row["error_kind"] = err.get("kind")
            row["error_message"] = (err.get("message") or "")[:200]
            print(f"✗ {elapsed:.1f}s HTTP {code} {err.get('kind') or ''}")
        rows.append(row)
        time.sleep(0.5)

    ok = [r for r in rows if r["code"] == 200]
    report = {
        "image": str(args.image), "base": args.base,
        "dry_run": args.dry, "total": len(rows), "ok": len(ok),
        "rows": rows,
    }
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    lines = ["| # | 能力 | 结果 | 耗时 | 输出尺寸 | 通路 |", "|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        mark = "✅" if r["code"] == 200 else f"❌ {r.get('error_kind') or r['code']}"
        lines.append(f"| {i} | {r['title']} `{r['model']}` | {mark} | {r['elapsed_s']}s "
                     f"| {r.get('size') or '—'} | {r.get('upstream_path') or '—'} |")
    (args.out / "report.md").write_text("\n".join(lines) + "\n")

    print("\n" + "\n".join(lines))
    print(f"\n合计 {len(ok)}/{len(rows)} 成功；报告 → {args.out}/report.md")
    return 0 if len(ok) == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
