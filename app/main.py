#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP 层：对外契约的**唯一入口**（契约全文 docs/INTERFACE.md）。

三件必须做对的事：

  1. **同步直给** —— POST 一次返回结果；上游就是同步 SSE，不做假异步。
  2. **错误信封统一** —— 我们自己的错误 `{"error":{code,message}}`；
     上游错误额外带 `kind` / `upstream:true` / 详情字段；重试语义用 `Retry-After` 表达。
  3. **鉴权语义** —— 静态 Bearer 白名单；`API_KEYS` 空 = 关闭（仅限内网，启动 WARNING）；
     `/v1/models` 刻意免鉴权（发现端点，与兄弟服务一致）；其余对外端点与运维面都要 Key。

⚠️ `gunicorn` 目标必须是**工厂**：`app.main:create_app()`（见 gunicorn_conf.py）。
"""

from __future__ import annotations

import hmac
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger

from . import __version__
from .admin import mount_admin
from .config import Settings, get_settings
from .errors import UPSTREAM_KIND_STATUS, ApiError, UpstreamError
from .gate import RateGate, RiskWindow
from .landing import render as render_landing
from .llms_txt import render as render_llms_txt
from .models import (
    ACCEPTS,
    CAPABILITIES,
    DELIBERATELY_ABSENT,
    MODEL_RELEASED_AT,
    NOT_REGISTERED,
    OWNED_BY,
    available,
    availability,
)
from .observability import setup as setup_observability
from .observability import spans
from .upstream.baidu import audio
from .service import run, validate_request
from . import usage
from .upstream.baidu import BaiduClient

__all__ = ["create_app"]


def _api_keys(settings: Settings) -> list[str]:
    return [k.strip() for k in settings.API_KEYS.split(",") if k.strip()]


async def require_api_key(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """静态 Bearer 白名单。

    白名单为空 ⇒ **鉴权整体关闭**（仅限内网），启动时打 WARNING。
    比对用 `hmac.compare_digest`（恒定时间），不用 `in`。

    ⚠️ 配置取自 `request.app.state.settings` 而**不是**全局 `get_settings()`：
    否则 `create_app(settings=...)` 传进来的配置对鉴权无效，测试会假绿。
    """
    settings: Settings = request.app.state.settings
    keys = _api_keys(settings)
    if not keys:
        return "anonymous"
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "unauthorized", "缺少 Bearer 凭据")
    token = authorization.split(" ", 1)[1].strip()
    if not any(hmac.compare_digest(token, k) for k in keys):
        raise ApiError(401, "unauthorized", "凭据不在白名单")
    return "client"


def _client(request: Request) -> BaiduClient:
    return request.app.state.client


def _gate(request: Request) -> RateGate:
    return request.app.state.gate


def _risk(request: Request) -> RiskWindow:
    return request.app.state.risk


def _header_dry_run(request: Request) -> bool:
    header = (request.headers.get("x-avm-dry-run") or "").strip().lower()
    return header in ("1", "true", "yes")


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()
    setup_observability(
        level=s.LOG_LEVEL, logfire_token=s.LOGFIRE_TOKEN, service_name=s.OTEL_SERVICE_NAME,
        environment=s.LOGFIRE_ENVIRONMENT, scrubbing=s.OTEL_SCRUBBING,
        capture_upstream=s.OTEL_CAPTURE_UPSTREAM,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = s
        app.state.client = BaiduClient(s)
        app.state.gate = RateGate(s.MIN_INTERVAL, s.PER_MINUTE)
        app.state.risk = RiskWindow(s.COOLDOWN)
        Path(s.MEDIA_DIR).mkdir(parents=True, exist_ok=True)
        if not _api_keys(s):
            logger.warning("BAIDU_API_KEYS 为空：鉴权已整体关闭，请只在受信内网这样部署")
        if not s.COOKIE:
            logger.warning("未配 BAIDU_COOKIE：所有真实调用会回 503"
                           "（文心免登录也需要一份 cookie —— 可用 wenxin/tools/mint_identity.py 铸造）")
        if s.ALLOW_UNVERIFIED:
            logger.warning("BAIDU_ALLOW_UNVERIFIED=1：未取证能力已放开（后果由部署方承担）")
        if s.LEGACY != "off":
            logger.warning("BAIDU_LEGACY={}：老接口兜底已启用（{}）——"
                           .format(s.LEGACY, s.LEGACY_BASE.rstrip("/") + "/aigc"))
        if s.PROXY_POOL.strip():
            logger.warning("BAIDU_PROXY_POOL 已配置（{} 个入口）：每个上游请求会换一个新出口"
                           .format(app.state.client.pool_size))
        try:
            yield
        finally:
            await app.state.client.aclose()

    app = FastAPI(
        title="baidu-service",
        version=__version__,
        summary=("文心助手（wenxin.baidu.com / chat.baidu.com）图片编辑的同步出口："
                 "18 项能力，图进图出；对外模型名 wenxin:*"),
        lifespan=lifespan,
    )

    # ---------- 错误信封 ----------

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        headers: dict[str, str] = {}
        retry_after = exc.extra.get("retry_after")
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            headers["Retry-After"] = str(int(retry_after))
        return JSONResponse(status_code=exc.status, content=exc.payload(), headers=headers)

    @app.exception_handler(UpstreamError)
    async def _upstream_error(_: Request, exc: UpstreamError) -> JSONResponse:
        status = UPSTREAM_KIND_STATUS.get(exc.kind, 502)
        body: dict[str, Any] = {
            "error": {
                "code": exc.kind,
                "message": exc.message,
                "kind": exc.kind,
                "upstream": True,
            }
        }
        for k, v in (exc.detail or {}).items():
            if v is not None:
                body["error"][k] = v
        headers: dict[str, str] = {}
        if exc.retry_after and exc.retry_after > 0:
            headers["Retry-After"] = str(int(exc.retry_after))
        return JSONResponse(status_code=status, content=body, headers=headers)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常：{}", exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal", "message": f"内部错误：{type(exc).__name__}"}},
        )

    # ---------- 健康与运维 ----------

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    async def readyz(request: Request) -> dict[str, Any]:
        client = _client(request)
        legacy = s.LEGACY != "off"
        checks = {
            "api_keys_enabled": bool(_api_keys(s)),
            "upstream_endpoint": client.endpoint,
            "cookie_configured": bool(s.COOKIE),
            "credentials": client.creds.diagnostics(),   # 只有来源/次数，绝无凭据值
            "upload_mode": s.UPLOAD_MODE,
            "strip_watermark": s.STRIP_WATERMARK,
            "result_fetch": {"timeout_s": s.RESULT_FETCH_TIMEOUT, "retries": s.RESULT_FETCH_RETRIES},
            "legacy_limits": {"max_side": s.LEGACY_MAX_IMAGE_SIDE,
                              "max_bytes": s.LEGACY_MAX_IMAGE_BYTES,
                              "rotate_cookie_on_burn": s.ROTATE_COOKIE_ON_BURN},
            "legacy": {"mode": s.LEGACY, "base": s.LEGACY_BASE if legacy else None},
            "proxy_pool": client.masked_proxies(),
            "risk_window": _risk(request).snapshot(),
            "rate_gate": _gate(request).snapshot(),
            "capabilities": {
                "total": len(CAPABILITIES),
                "available": len(available(allow_unverified=bool(s.ALLOW_UNVERIFIED),
                                           legacy=legacy)),
            },
            "media_dir": s.MEDIA_DIR,
        }
        degraded = (not s.COOKIE) or checks["risk_window"]["cooling"]
        return {"status": "degraded" if degraded else "ok", "checks": checks}

    @app.get("/stats")
    async def stats(request: Request, _: str = Depends(require_api_key)) -> dict[str, Any]:
        return {
            "version": __version__,
            "risk_window": _risk(request).snapshot(),
            "rate_gate": _gate(request).snapshot(),
            "spans": spans()[-50:],
        }

    # ---------- 发现端点 ----------

    @app.get("/v1/models")
    async def list_models(request: Request) -> dict[str, Any]:
        """OpenAI 兼容四键形态；**只列本部署可调用的模型**（免鉴权，与兄弟服务一致）。

        全集（含未取证项的**原因与开启方式**）在 `GET /capabilities`。
        `BAIDU_LEGACY != off` 时，**有老接口映射**的能力同样计入（依据是实测出图）。
        """
        settings: Settings = request.app.state.settings
        caps = available(allow_unverified=settings.ALLOW_UNVERIFIED,
                         legacy=settings.LEGACY != "off")
        return {
            "object": "list",
            "data": [
                {"id": name, "object": "model", "created": MODEL_RELEASED_AT, "owned_by": OWNED_BY}
                for name in sorted(caps)
            ],
        }

    @app.get("/", include_in_schema=False)
    async def index(request: Request) -> HTMLResponse:
        """根路径落地页（**给人看**）。本服务是纯 API —— 裸 404 会被当成"打不开"。"""
        settings: Settings = request.app.state.settings
        return HTMLResponse(render_landing(settings))

    @app.get("/llms.txt", include_in_schema=False)
    async def llms_txt(request: Request) -> PlainTextResponse:
        """给 LLM / Agent 的服务说明书（llmstxt.org 约定）——**免鉴权**。

        内容**从注册表派生**（能力表 / 必填输入 / 通路 / 本部署可用性 / 风格表 / 错误码），
        所以不会随能力变更而漂移。
        """
        settings: Settings = request.app.state.settings
        return PlainTextResponse(render_llms_txt(settings),
                                 media_type="text/markdown; charset=utf-8")

    @app.get("/capabilities")
    async def capabilities(request: Request,
                           _: str = Depends(require_api_key)) -> dict[str, Any]:
        settings: Settings = request.app.state.settings
        allow = settings.ALLOW_UNVERIFIED
        legacy = settings.LEGACY != "off"
        models = []
        not_available = []
        for name in sorted(CAPABILITIES):
            cap = CAPABILITIES[name]
            ok, reason = availability(cap, allow_unverified=allow, legacy=legacy)
            entry = {
                "id": name,
                "title": cap.title,
                "tool_type": cap.tool_type,
                "legacy_type": cap.legacy_type,
                "legacy_extra": dict(cap.legacy_extra) or None,
                "legacy_only": cap.legacy_only,
                "requires_mask": cap.requires_mask,
                "legacy_uses_prompt": cap.legacy_uses_prompt,
                "entry_type": cap.entry_type,
                "needs_style": cap.needs_style,
                "styles": [{"id": i, "label": lb} for i, lb in cap.style_table] or None,
                "accepts": list(ACCEPTS),
                "supports_ratio": cap.supports_ratio,
                "verified": cap.verified,
                "evidence": cap.evidence,
                "notes": cap.notes,
            }
            if ok:
                models.append(entry)
            else:
                entry["reason"] = reason
                not_available.append(entry)
        return {
            "models": models,
            "not_available": not_available,
            "not_registered": NOT_REGISTERED,
            "deliberately_absent": DELIBERATELY_ABSENT,
            "legacy": {"mode": settings.LEGACY, "base": settings.LEGACY_BASE if legacy else None},
        }

    # ---------- 执行端点（同步直给） ----------

    @app.post("/v1/images/generations")
    async def images_generations(request: Request,
                                 _: str = Depends(require_api_key)) -> JSONResponse:
        try:
            payload = await request.json()
        except ValueError as exc:
            raise ApiError(400, "invalid_json", "请求体不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_body", "请求体必须是 JSON 对象")

        started = time.perf_counter()
        error_code = ""
        upstream_path = ""
        title = ""
        model_name = str(payload.get("model") or "")
        try:
            # 校验放进 try：**未知模型这类 400 也要进用量**，否则客户端调错能力在面板上完全隐形
            (cap, image_value, mask_value, style_value, size, prompt,
             response_format, dry, unsupported) = validate_request(payload)
            title = cap.title
            dry_run = dry or _header_dry_run(request)
            body = await run(
                settings=s, client=_client(request), gate=_gate(request), risk=_risk(request),
                cap=cap, image_value=image_value, mask_value=mask_value, style_value=style_value,
                size=size,
                prompt=prompt, response_format=response_format, dry_run=dry_run,
                unsupported=unsupported,
            )
            upstream_path = ((body.get("upstream") or {}).get("path")
                             or ("dry-run" if body.get("dry_run") else ""))
            return JSONResponse(status_code=200, content=body)
        except UpstreamError as exc:
            error_code = exc.kind
            raise
        except ApiError as exc:
            error_code = exc.code
            raise
        finally:
            # 面板的用量页靠这一条：**成功与失败都进账**，否则成功率是无源之水
            usage.record(model=model_name or "unknown", title=title, ok=not error_code,
                         ms=(time.perf_counter() - started) * 1000,
                         dry=bool(payload.get("dry_run")), path=upstream_path,
                         error=error_code)

    # ---------- 语音（OpenAI 兼容：/v1/audio/*） ----------
    # 两条独立传输层（不走 conversation）：TTS = /aichat/api/tts，ASR = vse.baidu.com WS。
    # 证据与形状详见 docs/UPSTREAM.md §17。

    @app.post("/v1/audio/speech")
    async def audio_speech(request: Request,
                           _: str = Depends(require_api_key)) -> Response:
        """TTS（OpenAI `POST /v1/audio/speech` 形态）。

        入参：`{"model":"wenxin:tts","input":"...","voice":"4100"?,"rate":"64"?}`；
        返回 `audio/mpeg` 字节流（多分句已在服务端合并）。
        """
        try:
            payload = await request.json()
        except ValueError as exc:
            raise ApiError(400, "invalid_json", "请求体不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_body", "请求体必须是 JSON 对象")

        started = time.perf_counter()
        error_code = ""
        model_name = str(payload.get("model") or "")
        try:
            cap = CAPABILITIES.get(model_name)
            if cap is None or cap.media_kind != "tts":
                raise ApiError(400, "unknown_model",
                               f"TTS 端点只接受 model=wenxin:tts（收到 {model_name!r}）")
            unknown = set(payload) - {"model", "input", "voice", "rate", "response_format"}
            if unknown:
                raise ApiError(400, "unknown_field",
                               f"不认识的字段：{'、'.join(sorted(unknown))}。"
                               f"TTS 接受：model、input、voice、rate、response_format")
            text = payload.get("input")
            if not isinstance(text, str) or not text.strip():
                raise ApiError(400, "missing_input", "缺少 input（要合成的文本）")
            voice = payload.get("voice")
            if voice is not None:
                resolved = audio.resolve_voice(str(voice))
                if not resolved:
                    voices = "、".join(f"{k}({v['name']})" for k, v in audio.TTS_VOICES.items())
                    aliases = "、".join(sorted(set(audio.VOICE_ALIASES)))
                    raise ApiError(
                        400, "invalid_voice",
                        f"voice={voice!r} 不可用。实测可用音色：{voices}；"
                        f"语义别名：{aliases}")
                voice = resolved
            rate = str(payload.get("rate") or "64")
            if rate not in ("32", "64", "128"):
                raise ApiError(400, "invalid_rate",
                               "rate 是**比特率**（32/64/128 kbps，实测不是语速）")
            raw, info = await _client(request).tts(text.strip(), voice=voice, rate=rate)
            logger.info(f"tts: {info['segments']} segs / {info['bytes']}B / rate={rate}")
            return Response(content=raw, media_type="audio/mpeg",
                            headers={"X-Segments": str(info["segments"])})
        except UpstreamError as exc:
            error_code = exc.kind
            raise
        except ApiError as exc:
            error_code = exc.code
            raise
        finally:
            usage.record(model=model_name or "unknown", title="语音合成",
                         ok=not error_code,
                         ms=(time.perf_counter() - started) * 1000,
                         dry=False, path="tts", error=error_code)

    @app.post("/v1/audio/transcriptions")
    async def audio_transcriptions(request: Request,
                                   _: str = Depends(require_api_key)) -> JSONResponse:
        """ASR（OpenAI `POST /v1/audio/transcriptions` 形态，multipart）。

        入参：`file`（wav 或 16k mono s16le 裸 pcm）+ `model=wenxin:asr`；
        返回 `{"text": "..."}`。容器无 ffmpeg ⇒ 压缩格式（mp3/m4a）明确拒绝，
        由调用方先转 wav（这一条写进 docs/INTERFACE.md）。
        """
        started = time.perf_counter()
        error_code = ""
        model_name = ""
        try:
            try:
                form = await request.form()
            except Exception as exc:  # noqa: BLE001 - 非 multipart 会在这里炸
                raise ApiError(400, "invalid_form",
                               "请求必须是 multipart/form-data（字段 file + model）") from exc
            model_name = str(form.get("model") or "")
            cap = CAPABILITIES.get(model_name)
            if cap is None or cap.media_kind != "asr":
                raise ApiError(400, "unknown_model",
                               f"ASR 端点只接受 model=wenxin:asr（收到 {model_name!r}）")
            upload = form.get("file")
            if upload is None or isinstance(upload, str):
                raise ApiError(400, "missing_file", "缺少 file（音频文件）")
            data = await upload.read()
            if not data:
                raise ApiError(400, "empty_file", "file 是空文件")
            if len(data) > 25 * 1024 * 1024:
                raise ApiError(400, "file_too_large", "file 超过 25MB 上限")
            name = (upload.filename or "").lower()
            ctype = (upload.content_type or "").lower()
            if name.endswith(".wav") or ctype in ("audio/wav", "audio/x-wav", "audio/wave"):
                try:
                    pcm = audio.pcm16k_from_wav(data)
                except Exception as exc:  # noqa: BLE001 - wave 的异常类型不统一
                    raise ApiError(400, "invalid_wav",
                                   f"WAV 解析失败：{exc}（需未压缩 PCM 编码的 wav）") from exc
            elif name.endswith(".pcm") or ctype in ("application/octet-stream", ""):
                pcm = data          # 裸 pcm：约定 16k mono s16le
            else:
                raise ApiError(
                    400, "unsupported_audio",
                    f"不支持的音频格式 {name or ctype!r}：只接受 wav（自动转 16k mono）"
                    f"或 16k mono s16le 裸 pcm。容器无 ffmpeg，mp3/m4a 请先自行转换")
            text, info = await _client(request).asr(pcm)
            logger.info(f"asr: {info['seconds']}s audio → {len(text)} chars")
            return JSONResponse({"text": text, "bytes": info["pcm_bytes"],
                                 "seconds": info["seconds"]})
        except UpstreamError as exc:
            error_code = exc.kind
            raise
        except ApiError as exc:
            error_code = exc.code
            raise
        finally:
            usage.record(model=model_name or "unknown", title="语音识别",
                         ok=not error_code,
                         ms=(time.perf_counter() - started) * 1000,
                         dry=False, path="asr", error=error_code)

    # ---------- 管理面板（运维面） ----------
    # 依赖用参数注入（而不是 admin 里 import require_api_key），避免 main ↔ admin 循环导入。
    mount_admin(app, require_key=require_api_key)

    # ---------- 结果取件（response_format=url 时产生） ----------
    # 🔴 目录必须在 mount **之前**存在：StaticFiles(check_dir=True) 在挂载时就校验，
    # 否则 create_app 直接抛（而生命周期里的 mkdir 跑在挂载之后，救不了）。
    Path(s.MEDIA_DIR).mkdir(parents=True, exist_ok=True)
    app.mount("/files", StaticFiles(directory=str(Path(s.MEDIA_DIR))), name="files")

    return app


if __name__ == "__main__":  # python -m app.main
    # ⚠️ 入口必须写在这里：`python -m app.main` 跑的是本文件，不会去执行 app/__main__.py
    # （grok 轮踩过：写错位置会**静默退出 0**，看起来像起过了）。
    _s = get_settings()
    uvicorn.run("app.main:create_app", factory=True, host="127.0.0.1", port=_s.PORT)
