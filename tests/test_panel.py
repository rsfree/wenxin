"""管理面板用例：外壳免鉴权 / api 需 Key / 日志环形缓冲 / 与既有端点同口径。

零真实上游：面板的「试跑」直接打 `/v1/images/generations`，所以这里只验
「面板没有自造一条旁路」—— 干跑请求必须落到与线上完全相同的校验与出站计划上。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from loguru import logger

from app import identity, usage
from app.admin import _LOG_CAP, recent_logs, reset_logs
from app.main import create_app
from app.models import CAPABILITIES, availability
from app.upstream.baidu import BaiduClient
from tests.helpers import PNG_SMALL, UpstreamSpy, data_uri, settings


def _inject_client(app: Any, spy: Any) -> None:
    """把上游换成走 MockTransport 的实例（与 test_api._inject 同构）。"""
    app.state.client = BaiduClient(app.state.settings, transport=spy.transport())


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


# ---------------------------------------------------------------- 用量
def test_usage_records_success_and_failure(tmp_path):
    """用量必须**成功与失败都进账**：只记成功的计数器会让人误以为一切正常。"""
    
    usage.reset()
    app = _app(tmp_path)
    with TestClient(app) as c:
        ok = c.post("/v1/images/generations",
                    json={"model": "wenxin:clarity", "image": data_uri(PNG_SMALL),
                          "dry_run": True})
        bad = c.post("/v1/images/generations",
                     json={"model": "wenxin:nope", "image": data_uri(PNG_SMALL)})
        snap = c.get("/panel/api/usage").json()
    assert ok.status_code == 200 and bad.status_code == 400
    assert snap["summary"]["calls"] == 2
    assert snap["summary"]["ok"] == 1 and snap["summary"]["failed"] == 1
    assert snap["summary"]["p50_ms"] is None, "干跑不该污染 p50"
    row = next(m for m in snap["models"] if m["model"] == "wenxin:clarity")
    assert row["dry"] == 1 and row["p50_ms"] is None
    assert any(r["error"] == "unknown_model" for r in snap["recent"])


def test_usage_endpoint_needs_key(tmp_path):
    app = _app(tmp_path, API_KEYS="sk-live")
    with TestClient(app) as c:
        assert c.get("/panel/api/usage").status_code == 401


# ------------------------------------------------------------ 一键干跑
def test_dryrun_all_covers_every_registered_capability(tmp_path):
    """自检表必须覆盖注册表全集，且**每个都真的是干跑**（零上游、不出图）。"""
    app = _app(tmp_path)
    with TestClient(app) as c:
        spy = UpstreamSpy()
        _inject_client(app, spy)
        r = c.post("/panel/api/dryrun-all")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == len(CAPABILITIES)
    assert len(body["rows"]) == len(CAPABILITIES)
    conf = app.state.settings
    expected_usable = {
        n for n, cap in CAPABILITIES.items()
        if availability(cap, allow_unverified=conf.ALLOW_UNVERIFIED,
                        legacy=conf.LEGACY != "off")[0]
    }
    assert body["available"] == len(expected_usable), "可用性必须来自 availability()，不是干跑结果"
    assert body["gated"] == body["total"] - len(expected_usable)
    assert body["gated"] >= 1, "默认部署下未取证能力应如实报门禁，而不是伪装通过"
    assert body["preview_ok"] == body["total"], "干跑按设计穿透门禁 ⇒ 预览应全部可构造"
    assert {row["model"] for row in body["rows"]} == set(CAPABILITIES)
    assert spy.conversations == 0, "干跑不得产生任何上游对话请求"
    assert spy.seen == [] or all("conversation" not in str(req.url) for req in spy.seen)


def test_dryrun_all_needs_key(tmp_path):
    app = _app(tmp_path, API_KEYS="sk-live")
    with TestClient(app) as c:
        assert c.post("/panel/api/dryrun-all").status_code == 401


# ------------------------------------------------------------ 身份铸造
def test_mint_hot_applies_and_persists(tmp_path, monkeypatch):
    """铸造后：进程内立刻换 cookie、旧 token/lid 缓存必须丢、产物落盘。"""
    out = tmp_path / "identity.json"
    monkeypatch.setattr(identity, "IDENTITY_PATH", out)

    async def fake_mint(**_kw):
        return "BAIDUID=NEW:FG=1; BAIDUID_BFESS=NEW:FG=1", ["BAIDUID", "BAIDUID_BFESS"]

    monkeypatch.setattr(identity, "mint", fake_mint)

    app = _app(tmp_path, COOKIE="BAIDUID=OLD")
    with TestClient(app) as c:
        client = app.state.client
        client.creds._token, client.creds._lid, client.creds._at = "tok-old", "lid-old", 1.0
        r = c.post("/panel/api/mint")
        settings = app.state.settings
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == "in-process"
    assert settings.COOKIE.startswith("BAIDUID=NEW"), "必须热生效到 settings"
    assert client.creds._token == "" and client.creds._lid == "", "旧 token/lid 必须丢弃"
    assert out.exists() and "BAIDUID=NEW" in out.read_text()
    assert oct(out.stat().st_mode)[-3:] == "600"


def test_panel_html_has_usage_and_actions(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as c:
        html = c.get("/panel").text
    for needle in ("data-v=\"usage\"", "一键干跑（零上游）", "铸造新身份", "/panel/api/usage"):
        assert needle in html, needle
