#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语音合成 / 识别的**协议纯函数**（TTS 请求构造、ASR 帧构造、WAV 解码重采样）。

网络调用在 `client.py`（`BaiduClient.tts` / `BaiduClient.asr`）——这里只做
「形状」与「编码」：零网络、可单测、改错立刻可见。

两条链路的实证出处：`docs/UPSTREAM.md` §17（2026-11-17 实测）。
"""

from __future__ import annotations

import base64
import io
import struct
import time
import uuid
import wave
from typing import Any

from .capabilities import build_chat_token

# ---------------------------------------------------------------------------
# TTS（/aichat/api/tts）
# ---------------------------------------------------------------------------

#: 站点「语音播报」实测的固定 from（写错回 `invalid_from`）
TTS_FROM = "csaitab"
#: 默认发音人（站点默认 per=4100）
TTS_VOICE = "4100"
#: `rate` 是**比特率**（32/64/128 kbps，实测时长不变、字节数成比例），不是语速
TTS_RATE = "64"
TTS_RATES = ("32", "64", "128")


def tts_url(token: str, lid: str, text: str, *, voice: str | None = None,
            rate: str = TTS_RATE) -> str:
    """构造 TTS 请求 URL。

    `tk` 与 chat_token **完全同构**：`base64(token|md5(text)|ts|lid)-lid-3`，
    只是 md5 的输入是待合成的文本（不是 query）——实测抓包 base64 解码确认。
    `text` 不进 query（站点的前端 forEach 显式跳过它），走 POST body。
    `voice`（→ `per`）可选：实测站点默认不传、由服务端给 4100。
    """
    tk = build_chat_token(token, text, lid)
    per = f"&per={voice}" if voice else ""
    return ("/aichat/api/tts?from=" + TTS_FROM +
            "&appId=&agentSearchId=&rate=" + str(rate) +
            "&mid=" + per + "&tk=" + tk + "&")


def parse_tts_response(payload: Any) -> list[str]:
    """解析 TTS 响应 → 分句音频 URL 列表。非 0 status 视为失败（显式抛错）。"""
    if not isinstance(payload, dict):
        raise ValueError("TTS 响应不是 JSON 对象")
    status = payload.get("status")
    if status not in (0, "0"):
        raise ValueError(f"TTS 上游拒绝：status={status} message={payload.get('message')!r}")
    data = payload.get("data") or {}
    return [u for u in (data.get("contentList") or []) if isinstance(u, str) and u]


def merge_mp3(segments: list[bytes]) -> bytes:
    """把分句 mp3 字节流合并为一条。

    MPEG ADTS 是自同步帧流，直接拼接即可播放（各段都是同一编码参数）；
    不做 ID3 处理（百度 tts 的段不带 ID3）。
    """
    return b"".join(segments)


def audio_data_uri(raw: bytes, *, mime: str = "audio/mpeg") -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


# ---------------------------------------------------------------------------
# ASR（wss://vse.baidu.com/ws_api）
# ---------------------------------------------------------------------------

#: 反编译自 common.js VoiceInput 组件（2026-11-17）
ASR_URL_BASE = "wss://vse.baidu.com/ws_api"
ASR_DEV_KEY = "com.baidu.searchbox.fangyan"
ASR_DEV_PID = 8068              # 普通话 + 标点输入法模型
ASR_SAMPLE = 16000
ASR_CUID = "self_defined_server_id_like_mac_address"


def asr_url() -> str:
    return f"{ASR_URL_BASE}?sn={uuid.uuid4()}"


def asr_start_frame(*, punc: bool = True) -> dict[str, Any]:
    """START 帧（逐字对齐实测抓到的报文）。"""
    return {
        "type": "START",
        "data": {
            "user": "baidu_pc",
            "dev_key": ASR_DEV_KEY,
            "dev_pid": ASR_DEV_PID,
            "cuid": ASR_CUID,
            "sample": ASR_SAMPLE,
            "format": "pcm",
            "type": 1,
            "role_num": 1,
            "vad_type": 1,
            "vad_mode": 0,
            "channels": 1,
            "need_session_finish": True,
            "punc": bool(punc),
            "start_timestamp": int(time.time() * 1000),
        },
    }


ASR_FINISH_FRAME = {"type": "FINISH"}


def asr_result(frame: Any) -> str | None:
    """从一帧响应取识别文本；错误码显式区分（-3005 = 会话已结束，忽略）。"""
    if not isinstance(frame, dict):
        return None
    err = frame.get("err_no")
    if err not in (None, 0, "0", -3005, "-3005"):
        raise RuntimeError(
            f"ASR 上游错误 {err}：{frame.get('err_msg')!r}")
    if frame.get("type") == "HEARTBEAT":
        return None
    res = frame.get("result")
    return res if isinstance(res, str) and res else None


# ---------------------------------------------------------------------------
# WAV 解码 → 16k mono s16le PCM（标准库实现，不引 ffmpeg）
# ---------------------------------------------------------------------------

_TARGET = ASR_SAMPLE


def _to_mono(channels: int, sampwidth: int, frames: bytes) -> list[int]:
    """任意声道/位深 → 单声道整数样本（16bit 尺度）。"""
    if sampwidth == 1:                       # 8bit 无符号
        vals = [b - 128 for b in frames]
        scale = 256
    elif sampwidth == 2:                     # 16bit 有符号
        n = len(frames) // 2
        vals = list(struct.unpack(f"<{n}h", frames[: n * 2]))
        scale = 1
    elif sampwidth == 3:                     # 24bit 有符号
        n = len(frames) // 3
        vals = []
        for i in range(n):
            b0, b1, b2 = frames[i * 3], frames[i * 3 + 1], frames[i * 3 + 2]
            v = b0 | (b1 << 8) | (b2 << 16)
            if v & 0x800000:
                v -= 1 << 24
            vals.append(v >> 8)              # 归一化到 16bit 尺度
        scale = 1
    elif sampwidth == 4:                     # 32bit 有符号
        n = len(frames) // 4
        vals = [v >> 16 for v in struct.unpack(f"<{n}i", frames[: n * 4])]
        scale = 1
    else:
        raise ValueError(f"不支持的 WAV 位深：{sampwidth * 8}bit（支持 8/16/24/32）")
    if scale > 1:
        vals = [max(-32768, min(32767, v)) for v in vals]
    if channels <= 1:
        return vals
    # 多声道取平均
    out = []
    for i in range(0, len(vals) - channels + 1, channels):
        out.append(sum(vals[i: i + channels]) // channels)
    return out


def _resample(samples: list[int], src_rate: int, dst_rate: int = _TARGET) -> list[int]:
    """线性插值重采样（与站点前端 ky() 的算法同构，够用且可测）。"""
    if src_rate == dst_rate or not samples:
        return samples
    ratio = dst_rate / src_rate
    n_out = int(len(samples) * ratio)
    out = [0] * n_out
    for i in range(n_out):
        pos = i / ratio
        left = int(pos)
        frac = pos - left
        right = min(left + 1, len(samples) - 1)
        out[i] = int(samples[left] + (samples[right] - samples[left]) * frac)
    return out


def pcm16k_from_wav(data: bytes) -> bytes:
    """WAV → 16kHz 单声道 s16le 裸 PCM（ASR 只吃这个形态）。

    只支持 PCM 编码的 WAV（`wave` 标准库的能力边界）；压缩格式（mp3/m4a）
    由调用方在入口层拒绝——**依赖面刻意不引解码器**（容器无 ffmpeg）。
    """
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getcomptype() != "NONE":
            raise ValueError(f"不支持的 WAV 压缩类型：{w.getcomptype()}（需未压缩 PCM）")
        channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    samples = _to_mono(channels, sampwidth, frames)
    samples = _resample(samples, rate)
    return struct.pack(f"<{len(samples)}h", *samples)
