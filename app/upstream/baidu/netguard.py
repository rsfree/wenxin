#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外链准入门（SSRF 防线）。

为什么会存在：`image` / `mask` 字段允许 http(s) URL，本服务会**代取**它
（`media.resolve_input` → `client.fetch_url`）。不校验目标，这个端点就是一台
内网探测器 —— 调用方可以让网关去读 `http://127.0.0.1:8080/` 或云元数据
`http://169.254.169.254/latest/meta-data/`。

三层判据，任一不通过即拒（与 image-adapter §2.3 同源）：

  1. scheme 只收 http / https；
  2. 字面量 IP、本机名与内网后缀直接拒；
  3. DNS 解析出的**每一个** A/AAAA 都必须是公网地址。

⚠️ 残余风险（诚实登记，不假装已解决）：

  · **DNS rebinding** —— 解析与连接之间目标可被换成内网地址。彻底消除要把校验后的
    IP 钉进连接（自定义 transport + SNI 处理），本服务不做：收益不抵复杂度。
  · **走代理池时**（`BAIDU_PROXY_POOL` 非空），DNS 由**代理解析**，本机 getaddrinfo
    的结果不代表代理侧结果 ⇒ 第 3 层在这条路径上退化。第 1、2 层仍然有效。
  · 解析失败**不拦** —— 域名都解析不出来就不可能连到内网，交给 httpx 报网络错误。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from ...errors import ApiError

__all__ = ["ALLOWED_SCHEMES", "check_url", "check_url_text", "redact_url"]

ALLOWED_SCHEMES: tuple[str, ...] = ("http", "https")

#: 本机/元数据类的**名字**（字面量 IP 由 `_blocked` 判）。
_BLOCKED_HOSTS: frozenset[str] = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "metadata", "metadata.google.internal", "metadata.goog",
})

#: 内网风格的域名后缀（`.internal` 在云厂商里普遍指向内网）。
_BLOCKED_SUFFIXES: tuple[str, ...] = (".localhost", ".local", ".internal", ".localdomain")

#: 显式网段：`is_private` 覆盖不到的保留段（CGNAT / 文档段 / 基准测试段）必须列全。
_BLOCKED_NETS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(n) for n in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
        "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
        "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
        "224.0.0.0/4", "240.0.0.0/4",
        "::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8",
    )
)


def _addr(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """把主机名当 IP 字面量解析；不是字面量返回 None。

    IPv4-mapped（`::ffff:127.0.0.1`）先还原成 v4 再判 —— 否则会被当成普通 v6 放过。
    """
    try:
        addr = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _blocked(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_multicast or addr.is_reserved or addr.is_unspecified):
        return True
    return any(addr in net for net in _BLOCKED_NETS if net.version == addr.version)


def check_url_text(url: str) -> None:
    """纯函数部分（不发 DNS）：scheme + 主机名字面量。

    单独暴露是因为它可以在校验层复用，且测试不需要任何桩。
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ApiError(
            400, "invalid_image_url",
            f"外链只接受 http / https，收到 {scheme or '(无 scheme)'}；"
            "data URI 与裸 base64 走 image 字段的另外两种形态",
        )
    host = (parts.hostname or "").strip().lower()
    if not host:
        raise ApiError(400, "invalid_image_url", "外链缺少主机名")

    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
        raise ApiError(
            400, "image_url_blocked",
            f"目标主机（{host}）属于本机或内网域名，拒绝代取。"
            "外链必须是公网可访问的图片地址",
        )

    literal = _addr(host)
    if literal is not None and _blocked(literal):
        raise ApiError(
            400, "image_url_blocked",
            f"目标是内网 / 保留地址（{host}），拒绝代取。"
            "外链必须是公网可访问的图片地址",
        )


async def _resolve(host: str) -> list[str]:
    """解析出主机名的全部 A/AAAA。**失败返回空表** —— 见模块文档「解析失败不拦」。"""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, type=socket.SOCK_STREAM,
        )
    except (socket.gaierror, UnicodeError, OSError):
        return []
    return [str(info[4][0]) for info in infos]


async def check_url(url: str) -> None:
    """文本校验 + DNS 解析校验（`fetch_url` 的准入门）。"""
    check_url_text(url)
    host = (urlsplit(url).hostname or "").strip().lower()
    if _addr(host) is not None:
        return                                  # 字面量已在文本层判完
    for ip in await _resolve(host):
        resolved = _addr(ip)
        if resolved is not None and _blocked(resolved):
            raise ApiError(
                400, "image_url_blocked",
                f"目标主机（{host}）解析到内网 / 保留地址（{ip}），拒绝代取",
            )


def redact_url(url: str) -> str:
    """给错误报文用的 URL：**只留 scheme://host[:port]/path**。

    上游结果 CDN 的 URL 带签名（`?sign=…&q-ak=…`），整条进报文等于把凭据写进日志
    和调用方手里（image-adapter §5.4 的实测反例）。
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:                             # IPv6 字面量要带回方括号
        host = f"[{host}]"
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:                          # 非法端口：不带端口即可
        port = ""
    return f"{parts.scheme}://{host}{port}{parts.path or '/'}"
