"""管理面板用例：外壳免鉴权 / api 需 Key / 日志环形缓冲 / 与既有端点同口径。

零真实上游：面板的「试跑」直接打 `/v1/images/generations`，所以这里只验
「面板没有自造一条旁路」—— 干跑请求必须落到与线上完全相同的校验与出站计划上。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from loguru import logger

from app.admin import _LOG_CAP, recent_logs, reset_logs
from app.main import create_app
from tests.helpers import PNG_SMALL, data_uri, settings


def _app(tmp_path: Any, **overrides: Any):
    media = tmp_path / "media"
    media.mkdir(parents=True, exist_ok=True)
    return create_app(settings(MEDIA_DIR=str(media), **overrides))


def test_panel_html_is_anonymous_and_contains_shell(tmp_path):
    """HTML 里没有秘密 ⇒ 免鉴权；但必须真的是一份能跑的外壳。"""
    app = _app(tmp_path, API_KEYS="sk-live")
    with TestClient(app) as c:
        r = c.get("/panel")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "wenxin-service" in body
    assert "/v1/images/generations" in body, "测试台必须直连既有端点，不能自造旁路"
    assert "sk-live" not in body, "密钥绝不能出现在 HTML 里"


def test_panel_logs_requires_key_when_api_keys_configured(tmp_path):
    app = _app(tmp_path, API_KEYS="sk-live")
    with TestClient(app) as c:
        assert c.get("/panel/api/logs").status_code == 401
        assert c.get("/panel/api/logs",
                     headers={"authorization": "Bearer wrong"}).status_code == 401
        ok = c.get("/panel/api/logs", headers={"authorization": "Bearer sk-live"})
    assert ok.status_code == 200
    assert ok.json()["capacity"] == _LOG_CAP


def test_panel_logs_open_when_api_keys_empty(tmp_path):
    """API_KEYS 空 = 鉴权整体关闭（与 /v1 同语义），面板不能例外地要求 Key。"""
    app = _app(tmp_path, API_KEYS="")
    with TestClient(app) as c:
        r = c.get("/panel/api/logs")
    assert r.status_code == 200


def test_log_ring_collects_and_caps(tmp_path):
    reset_logs()
    _app(tmp_path)  # 装 sink（幂等）
    logger.warning("面板日志自检 {} 条", 1)
    items = recent_logs(50)
    assert items, "日志应进入环形缓冲"
    assert items[-1]["msg"].startswith("面板日志自检")
    assert items[-1]["level"] == "WARNING"
    assert {"t", "level", "msg"} == set(items[-1]), "只留这三样，凭据无从泄露"


def test_panel_dry_run_hits_the_shared_validation_path(tmp_path):
    """面板的试跑就是 /v1 —— 干跑必须给出与线上一致的出站计划（零上游）。"""
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/v1/images/generations", json={
            "model": "wenxin:clarity", "image": data_uri(PNG_SMALL), "dry_run": True,
        })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["preview"]["would_post_to"].endswith("/aichat/api/conversation")
    assert body["effective"]["query"] == "变清晰"


def test_stats_is_an_ops_endpoint_behind_the_same_key(tmp_path):
    """/stats 是运维面：与 /v1 同一把 Key，且给出闸门/风险窗/span 三样。"""
    app = _app(tmp_path, API_KEYS="sk-live")
    with TestClient(app) as c:
        assert c.get("/stats").status_code == 401
        r = c.get("/stats", headers={"authorization": "Bearer sk-live"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"version", "risk_window", "rate_gate", "spans"}
    assert body["risk_window"]["cooling"] is False
    assert isinstance(body["spans"], list)


def test_landing_page_links_the_panel_and_llms_txt(tmp_path):
    """落地页是给人看的第一屏：必须能点进面板与说明书，否则等于藏起来。"""
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.get("/")
    assert r.status_code == 200
    assert 'href="/panel"' in r.text
    assert 'href="/llms.txt"' in r.text
