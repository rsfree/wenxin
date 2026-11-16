#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语音模块的**协议纯函数**测试（TTS 形状 / ASR 帧 / WAV 解码重采样）。

证据出处：docs/UPSTREAM.md §17（2026-11-17 匿名实测）。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import struct
import time
import wave

import pytest

from app.upstream.baidu import audio


# ------------------------------------------------------------------ TTS

def test_tts_url_tk_is_chat_token_shape():
    """tk 与 chat_token 同构：base64(token|md5(text)|ts|lid)-lid-3（实测解码确认）。"""
    token, lid, text = "db88606b", "8571349814178692271", "你好，这是语音合成测试。"
    url = audio.tts_url(token, lid, text)
    assert url.startswith("/aichat/api/tts?from=csaitab&appId=&agentSearchId=&rate=64&mid=")
    tk = url.split("tk=", 1)[1].rstrip("&")
    raw = base64.b64decode(tk.rsplit("-", 2)[0]).decode()
    tok, md5, ts, lid_in = raw.split("|")
    assert tok == token
    assert md5 == hashlib.md5(text.encode()).hexdigest()
    assert lid_in == lid
    assert abs(int(ts) - time.time() * 1000) < 5000
    assert tk.endswith(f"-{lid}-3")
    # text 不进 query（站点前端显式跳过），走 POST body
    assert "text=" not in url


def test_tts_url_voice_and_rate():
    url = audio.tts_url("t", "l", "x", voice="3", rate="128")
    assert "&per=3&" in url and "rate=128" in url
    # 不传 voice 时不出 per（站点默认由服务端给 4100）
    assert "per=" not in audio.tts_url("t", "l", "x")


def test_parse_tts_response_ok_and_fail():
    urls = audio.parse_tts_response(
        {"status": 0, "data": {"contentList": ["https://a/1.mp3", "", "https://a/2.mp3"]}})
    assert urls == ["https://a/1.mp3", "https://a/2.mp3"]
    with pytest.raises(ValueError, match="invalid_from|status"):
        audio.parse_tts_response({"status": -1, "message": "invalid_from"})
    with pytest.raises(ValueError):
        audio.parse_tts_response([])


def test_merge_mp3_is_byte_concat():
    assert audio.merge_mp3([b"\xff\xf3", b"\xff\xf4"]) == b"\xff\xf3\xff\xf4"
    assert audio.merge_mp3([]) == b""


# ------------------------------------------------------------------ ASR

def test_asr_start_frame_matches_captured():
    """START 帧逐字对齐实测（common.js VoiceInput 组件反编译）。"""
    f = audio.asr_start_frame()
    assert f["type"] == "START"
    d = f["data"]
    assert d["dev_key"] == "com.baidu.searchbox.fangyan"
    assert d["dev_pid"] == 8068
    assert d["sample"] == 16000 and d["format"] == "pcm" and d["channels"] == 1
    assert d["vad_type"] == 1 and d["vad_mode"] == 0
    assert d["need_session_finish"] is True and d["punc"] is True
    assert isinstance(d["start_timestamp"], int)
    assert audio.ASR_FINISH_FRAME == {"type": "FINISH"}


def test_asr_result_extraction():
    assert audio.asr_result({"type": "MID_TEXT", "err_no": 0,
                             "result": "今天天气"}) == "今天天气"
    assert audio.asr_result({"type": "FIN_TEXT", "err_no": 0,
                             "result": "今天天气不错。"}) == "今天天气不错。"
    assert audio.asr_result({"type": "HEARTBEAT"}) is None
    assert audio.asr_result({"type": "MID_TEXT", "err_no": 0, "result": ""}) is None
    # -3005 = 会话结束，静默忽略
    assert audio.asr_result({"err_no": -3005}) is None
    # 其它错误码显式抛
    with pytest.raises(RuntimeError, match="ASR 上游错误"):
        audio.asr_result({"err_no": 3301, "err_msg": "bad"})


def _wav(samples: list[int], *, rate: int = 16000, channels: int = 1,
         sampwidth: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        if sampwidth == 1:
            frames = bytes(s + 128 for s in samples)
        elif sampwidth == 2:
            frames = struct.pack(f"<{len(samples)}h", *samples)
        elif sampwidth == 4:
            frames = struct.pack(f"<{len(samples)}i", *[s << 16 for s in samples])
        else:
            raise AssertionError
        w.writeframes(frames)
    return buf.getvalue()


def test_wav_passthrough_16k_mono():
    samples = [0, 1000, -1000, 32000, -32000]
    pcm = audio.pcm16k_from_wav(_wav(samples))
    assert struct.unpack(f"<{len(samples)}h", pcm) == tuple(samples)


def test_wav_resample_48k_to_16k():
    """48k → 16k：样本数应约 1/3（线性插值）。"""
    src = [int(10000 * (i % 10) / 10.0) for i in range(4800)]   # 0.1s @48k
    pcm = audio.pcm16k_from_wav(_wav(src, rate=48000))
    n = len(pcm) // 2
    assert abs(n - 1600) <= 2, f"expect ~1600 samples, got {n}"


def test_wav_stereo_to_mono():
    left = [100, 200, 300, 400]
    right = [300, 400, 500, 600]
    inter = []
    for a, b in zip(left, right):
        inter += [a, b]
    pcm = audio.pcm16k_from_wav(_wav(inter, channels=2))
    got = struct.unpack(f"<{len(pcm)//2}h", pcm)
    assert list(got) == [200, 300, 400, 500]     # 左右平均


def test_wav_8bit_and_32bit_paths():
    # 8bit 无符号（128 是零点）→ 16bit 有符号
    pcm = audio.pcm16k_from_wav(_wav([0, 64, -64], sampwidth=1))
    vals = struct.unpack("<3h", pcm)
    assert vals[0] == 0 and vals[1] > 0 and vals[2] < 0
    # 32bit → 16bit（_wav 里左移 16，这里应还原）
    pcm2 = audio.pcm16k_from_wav(_wav([10, -10], sampwidth=4))
    assert struct.unpack("<2h", pcm2) == (10, -10)


def test_wav_rejects_non_pcm_and_bad_input():
    # 非 wav 字节
    with pytest.raises(Exception):
        audio.pcm16k_from_wav(b"not a wav")


def test_audio_data_uri():
    uri = audio.audio_data_uri(b"\xff\xf3")
    assert uri.startswith("data:audio/mpeg;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == b"\xff\xf3"


def test_tts_url_json_serializable_frames():
    """START 帧必须能 JSON 序列化（WS 发送用）。"""
    json.dumps(audio.asr_start_frame(), ensure_ascii=False)


# ------------------------------------------------------------------ 音色
def test_resolve_voice_aliases_and_numbers():
    assert audio.resolve_voice(None) is None
    assert audio.resolve_voice("") is None
    assert audio.resolve_voice("4100") == "4100"
    assert audio.resolve_voice("female") == "4100"
    assert audio.resolve_voice("MALE") == "4106"      # 大小写不敏感
    assert audio.resolve_voice("child") == "4201"
    assert audio.resolve_voice("lowmale") == "4115"
    assert audio.resolve_voice("xiaomei") == "0"
    assert audio.resolve_voice("not-a-voice") is None


def test_tts_voices_whitelist_is_seven_and_well_formed():
    """白名单 = 实测授权的 7 个（其它编号上游回 501/err_subcode 23）。"""
    assert set(audio.TTS_VOICES) == {"0", "4003", "4100", "4106", "4115", "4118", "4201"}
    for per, meta in audio.TTS_VOICES.items():
        assert per.isdigit()
        assert meta["gender"] in ("female", "male", "child")
        assert meta["f0"].endswith("Hz")
