"""外链准入门（SSRF 防线）的用例。

为什么单独一组：`image` 允许 http(s) URL ⇒ 服务会代取它。没有这层校验，
这个端点就是一台内网探测器（127.0.0.1 / 169.254.169.254 / 10.x）。

六个面必须都咬住：
  ① scheme 白名单；② 字面量私网/环回/元数据；③ 本机名与内网后缀；
  ④ DNS 解析到私网；⑤ **重定向逐跳校验**（httpx 自动跟随会绕过它）；
  ⑥ 报文里的 URL 去签名（上游 CDN 的 query 不能进错误体）。

DNS 一律用桩（`netguard._resolve`）：用例不依赖外网，也不会因为某天
`cdn.example` 真被解析成内网而变成假绿。
"""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.errors import ApiError, UpstreamUnavailableError
from app.upstream.baidu import netguard
from app.upstream.baidu.client import BaiduClient
from app.upstream.baidu.netguard import check_url, check_url_text, redact_url
from tests.helpers import arun

PUBLIC_IP = "93.184.216.34"


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """可改的解析表；未登记的域名默认解析到公网。"""
    table: dict[str, list[str]] = {
        "cdn.example": [PUBLIC_IP],
        "slow.example": [PUBLIC_IP],
    }

    async def _fake_resolve(host: str) -> list[str]:
        return table.get(host, [PUBLIC_IP])

    monkeypatch.setattr(netguard, "_resolve", _fake_resolve)
    return table


def _client(handler) -> BaiduClient:
    return BaiduClient(Settings(_env_file=None, COOKIE="BAIDUID=x"),
                       transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------- ① scheme


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x/a.png", "javascript:alert(1)"])
def test_rejects_non_http_scheme(url: str) -> None:
    with pytest.raises(ApiError) as ei:
        check_url_text(url)
    assert ei.value.code == "invalid_image_url"
    assert ei.value.status == 400


# ------------------------------------------------- ② 字面量私网 / ③ 本机名


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/x.png",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/x.png",
    "http://192.168.1.1/x.png",
    "http://172.16.0.9/x.png",
    "http://100.64.0.1/x.png",          # CGNAT：is_private 不覆盖，靠显式网段
    "http://0.0.0.0/x.png",
])
def test_rejects_private_and_special_literals(url: str) -> None:
    with pytest.raises(ApiError) as ei:
        check_url_text(url)
    assert ei.value.code == "image_url_blocked"


@pytest.mark.parametrize("url", [
    "http://[::1]/x.png",
    "http://[::ffff:127.0.0.1]/x.png",  # v4-mapped 必须还原成 v4 再判
    "http://[fe80::1]/x.png",
    "http://[fd00::1]/x.png",
])
def test_rejects_ipv6_internal(url: str) -> None:
    with pytest.raises(ApiError) as ei:
        check_url_text(url)
    assert ei.value.code == "image_url_blocked"


@pytest.mark.parametrize("url", [
    "http://localhost/x.png",
    "http://api.localhost/x.png",
    "http://printer.local/x.png",
    "http://metadata.google.internal/x",
    "http://db.internal/x.png",
])
def test_rejects_internal_host_names(url: str) -> None:
    with pytest.raises(ApiError) as ei:
        check_url_text(url)
    assert ei.value.code == "image_url_blocked"


def test_allows_public_literal(dns: dict[str, list[str]]) -> None:
    arun(check_url("https://8.8.8.8/x.png"))          # 不抛即通过


# ------------------------------------------------------------ ④ DNS 解析


def test_rejects_host_resolving_to_private(dns: dict[str, list[str]]) -> None:
    dns["evil.example"] = ["10.1.2.3"]
    with pytest.raises(ApiError) as ei:
        arun(check_url("https://evil.example/a.png"))
    assert ei.value.code == "image_url_blocked"


def test_rejects_when_any_answer_is_private(dns: dict[str, list[str]]) -> None:
    """双栈/多 A 记录：**任一**是内网就拒（不能挑着用公网那条）。"""
    dns["mixed.example"] = [PUBLIC_IP, "192.168.0.10"]
    with pytest.raises(ApiError) as ei:
        arun(check_url("https://mixed.example/a.png"))
    assert ei.value.code == "image_url_blocked"


def test_dns_failure_is_not_a_rejection(dns: dict[str, list[str]]) -> None:
    """解析不出来 ⇒ 连不上内网，交给 httpx 报网络错误（不在这里误判）。"""
    dns["nx.example"] = []
    arun(check_url("https://nx.example/a.png"))


# ------------------------------------------------------- ⑤ 重定向逐跳校验


def test_fetch_blocks_redirect_to_loopback(dns: dict[str, list[str]]) -> None:
    """302 → 127.0.0.1 必须在**第二跳**被拦。

    这条是「httpx 自动跟随」的变异自证：把 `follow_redirects` 改回 True，
    本用例立刻转红（请求会打到 MockTransport 的 /secret 分支）。
    """
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        if request.url.host == "cdn.example":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        return httpx.Response(200, content=b"should-not-be-read")

    c = _client(handler)
    with pytest.raises(ApiError) as ei:
        arun(c.fetch_url("https://cdn.example/a.png", 1024))
    arun(c.aclose())
    assert ei.value.code == "image_url_blocked"
    assert hits == ["https://cdn.example/a.png"], "第二跳不该发出"


def test_fetch_follows_public_redirect(dns: dict[str, list[str]]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a.png":
            return httpx.Response(302, headers={"location": "/b.png"})
        return httpx.Response(200, content=b"final", headers={"content-type": "image/png"})

    c = _client(handler)
    raw, ct = arun(c.fetch_url("https://cdn.example/a.png", 1024))
    arun(c.aclose())
    assert raw == b"final" and ct == "image/png"


def test_fetch_stops_after_redirect_limit(dns: dict[str, list[str]]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/again"})

    c = _client(handler)
    with pytest.raises(UpstreamUnavailableError) as ei:
        arun(c.fetch_url("https://cdn.example/loop", 1024))
    arun(c.aclose())
    assert "重定向超过" in str(ei.value)


# ------------------------------------------------------------- ⑥ 报文脱敏


def test_redact_url_strips_query_and_userinfo() -> None:
    signed = ("https://aisearch.cdn.bcebos.com/pic_create/a.jpg"
              "?authorization=bce-auth-v1%2Fak&q-ak=AKIDxxxx&x-bce-signature=deadbeef")
    assert redact_url(signed) == "https://aisearch.cdn.bcebos.com/pic_create/a.jpg"

    with_user = "https://user:pass@cdn.example:8443/a.png?token=1"
    assert redact_url(with_user) == "https://cdn.example:8443/a.png"


def test_error_message_does_not_leak_signed_query(dns: dict[str, list[str]]) -> None:
    """取回失败时，错误报文里不能出现签名参数。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"nope")

    c = _client(handler)
    with pytest.raises(UpstreamUnavailableError) as ei:
        arun(c.fetch_url("https://cdn.example/a.png?q-ak=AKIDsecret&sign=deadbeef", 1024))
    arun(c.aclose())
    assert "AKIDsecret" not in str(ei.value)
    assert "sign=" not in str(ei.value)
