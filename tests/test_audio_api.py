#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`/v1/audio/*` 端点契约（OpenAI 兼容形态）—— TTS 与 ASR。

上游 mock 方式与 test_api 一致：MockTransport 换 client（TTS 走 HTTP）；
ASR 是 WebSocket，替身直接 patch `BaiduClient.asr`（WS 不在 httpx 的面上）。
"""

from __future__ import annotations

import io
import json
import wave
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from app.upstream.baidu import BaiduClient
from tests.helpers import UpstreamSpy, settings

SPEECH = "/v1/audio/speech"
ASR_URL = "/v1/audio/transcriptions"


def _app(tmp_path, **overrides: Any):
    media = tmp_path / "media"
    media.mkdir(parents=True, exist_ok=True)
    conf = settings(MEDIA_DIR=str(media), **overrides)
    return create_app(conf), media


def _inject(app, spy: UpstreamSpy, **overrides: Any) -> None:
    conf = (app.state.settings.model_copy(update=overrides)
            if overrides else app.state.settings)
    app.state.client = BaiduClient(conf, transport=spy.transport())


def _wav_bytes(seconds: float = 0.2, rate: int = 16000) -> bytes:
    n = int(rate * seconds)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x01" * n)
    return buf.getvalue()


# ------------------------------------------------------------------ TTS


def _tts_spy(segments: list[str] | None = None) -> UpstreamSpy:
    segs = segments or ["https://tts.baidu.com/text2audio?a=1"]

    def tts(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert isinstance(body.get("text"), str) and body["text"]
        return httpx.Response(200, json={"status": 0, "message": "",
                                         "data": {"contentList": segs}})

    return UpstreamSpy(routes={
        "/aichat/api/tts": tts,
        "/text2audio": lambda r: httpx.Response(200, content=b"\xff\xf3FAKEMP3"),
    })


def test_tts_happy_path_returns_mp3_bytes(tmp_path):
    app, _ = _app(tmp_path)
    spy = _tts_spy()
    with TestClient(app) as c:
        _inject(app, spy)
        r = c.post(SPEECH, json={"model": "wenxin:tts", "input": "你好，世界"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.content == b"\xff\xf3FAKEMP3"
    assert r.headers["x-segments"] == "1"
    # 请求形状：路径 + body 带 text
    tts_req = [q for q in spy.seen if q.url.path == "/aichat/api/tts"][0]
    assert tts_req.url.params["from"] == "csaitab"
    assert tts_req.url.params["rate"] == "64"
    assert tts_req.url.params["tk"]
    assert "per" not in tts_req.url.params      # 默认不传，服务端给 4100


def test_tts_merges_segments(tmp_path):
    app, _ = _app(tmp_path)
    spy = _tts_spy(["https://tts.baidu.com/text2audio?a=1",
                    "https://tts.baidu.com/text2audio?a=2"])
    with TestClient(app) as c:
        _inject(app, spy)
        r = c.post(SPEECH, json={"model": "wenxin:tts", "input": "第一句。第二句。"})
    assert r.status_code == 200
    assert r.content == b"\xff\xf3FAKEMP3" * 2
    assert r.headers["x-segments"] == "2"


def test_tts_voice_and_rate_pass_through(tmp_path):
    app, _ = _app(tmp_path)
    spy = _tts_spy()
    with TestClient(app) as c:
        _inject(app, spy)
        r = c.post(SPEECH, json={"model": "wenxin:tts", "input": "你好",
                                 "voice": "3", "rate": "128"})
    assert r.status_code == 200
    req = [q for q in spy.seen if q.url.path == "/aichat/api/tts"][0]
    assert req.url.params["per"] == "3"
    assert req.url.params["rate"] == "128"


def test_tts_validation_errors(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _inject(app, _tts_spy())
        # 未知 model
        r = c.post(SPEECH, json={"model": "wenxin:draw", "input": "x"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "unknown_model"
        # 缺 input
        r = c.post(SPEECH, json={"model": "wenxin:tts"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "missing_input"
        # 非法 rate
        r = c.post(SPEECH, json={"model": "wenxin:tts", "input": "x", "rate": "44.1"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_rate"
        # 非法 voice
        r = c.post(SPEECH, json={"model": "wenxin:tts", "input": "x", "voice": "abc"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_voice"
        # 未知字段
        r = c.post(SPEECH, json={"model": "wenxin:tts", "input": "x", "foo": 1})
        assert r.status_code == 400 and r.json()["error"]["code"] == "unknown_field"


def test_tts_upstream_reject_is_upstream_error(tmp_path):
    app, _ = _app(tmp_path)

    def tts(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": -1, "message": "invalid_from"})

    spy = UpstreamSpy(routes={"/aichat/api/tts": tts})
    with TestClient(app) as c:
        _inject(app, spy)
        r = c.post(SPEECH, json={"model": "wenxin:tts", "input": "x"})
    assert r.status_code == 400
    body = r.json()["error"]
    assert body["upstream"] is True


# ------------------------------------------------------------------ ASR


def test_asr_happy_path(tmp_path, monkeypatch):
    app, _ = _app(tmp_path)
    seen: dict[str, Any] = {}

    async def fake_asr(self, pcm: bytes, *, timeout_s: float = 90.0):
        seen["pcm_bytes"] = len(pcm)
        return "今天天气不错。", {"path": "asr", "pcm_bytes": len(pcm),
                                  "seconds": round(len(pcm) / 32000, 2), "frames": 3}

    monkeypatch.setattr(BaiduClient, "asr", fake_asr)
    wav = _wav_bytes(0.2)
    with TestClient(app) as c:
        r = c.post(ASR_URL, files={"file": ("a.wav", wav, "audio/wav")},
                   data={"model": "wenxin:asr"})
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "今天天气不错。"
    # wav 0.2s @16k mono → 6400 samples * 2 bytes
    assert seen["pcm_bytes"] == 6400
    assert body["bytes"] == 6400


def test_asr_resamples_wav(tmp_path, monkeypatch):
    app, _ = _app(tmp_path)
    seen: dict[str, Any] = {}

    async def fake_asr(self, pcm: bytes, *, timeout_s: float = 90.0):
        seen["n"] = len(pcm) // 2
        return "x", {"path": "asr", "pcm_bytes": len(pcm), "seconds": 0.1, "frames": 1}

    monkeypatch.setattr(BaiduClient, "asr", fake_asr)
    wav = _wav_bytes(0.1, rate=48000)     # 48k → 16k
    with TestClient(app) as c:
        r = c.post(ASR_URL, files={"file": ("a.wav", wav, "audio/wav")},
                   data={"model": "wenxin:asr"})
    assert r.status_code == 200
    assert abs(seen["n"] - 1600) <= 2


def test_asr_validation_errors(tmp_path, monkeypatch):
    app, _ = _app(tmp_path)

    async def fake_asr(self, pcm: bytes, *, timeout_s: float = 90.0):  # pragma: no cover
        raise AssertionError("不该被调用")

    monkeypatch.setattr(BaiduClient, "asr", fake_asr)
    with TestClient(app) as c:
        # 未知 model
        r = c.post(ASR_URL, files={"file": ("a.wav", _wav_bytes(), "audio/wav")},
                   data={"model": "wenxin:tts"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "unknown_model"
        # 缺 file
        r = c.post(ASR_URL, data={"model": "wenxin:asr"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "missing_file"
        # 不支持的格式
        r = c.post(ASR_URL, files={"file": ("a.mp3", b"ID3xxxx", "audio/mpeg")},
                   data={"model": "wenxin:asr"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_audio"
        # 坏 wav
        r = c.post(ASR_URL, files={"file": ("a.wav", b"not-a-wav", "audio/wav")},
                   data={"model": "wenxin:asr"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_wav"


def test_asr_accepts_raw_pcm(tmp_path, monkeypatch):
    app, _ = _app(tmp_path)
    seen: dict[str, Any] = {}

    async def fake_asr(self, pcm: bytes, *, timeout_s: float = 90.0):
        seen["n"] = len(pcm)
        return "raw", {"path": "asr", "pcm_bytes": len(pcm), "seconds": 0.01, "frames": 1}

    monkeypatch.setattr(BaiduClient, "asr", fake_asr)
    pcm = b"\x00\x00" * 1600
    with TestClient(app) as c:
        r = c.post(ASR_URL, files={"file": ("a.pcm", pcm, "application/octet-stream")},
                   data={"model": "wenxin:asr"})
    assert r.status_code == 200 and r.json()["text"] == "raw"
    assert seen["n"] == 3200
