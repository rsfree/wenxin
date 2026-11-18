# 图片契约对照：image-adapter × baidu-service

> **结论：本服务的图片能力是 image-adapter 契约的一个实现，不是另一套协议。**
> 唯一端点 `POST /v1/images/generations`；文生图 / 图生图靠 `image` 字段在不在区分。
> 本文逐条登记：已对齐的、**有理由不照搬的**、本次补掉的缺口、以及仍然存在的风险。
> 上游事实见 [`UPSTREAM.md`](UPSTREAM.md)；对外契约见 [`INTERFACE.md`](INTERFACE.md)。

---

## 1. 已对齐（有测试守着）

| 契约条款 | 本服务落点 | 守着它的用例 |
|---|---|---|
| 单端点承载文生图/图生图 | `wenxin:draw` 只给 `prompt`；其余能力给 `image` | `test_api.py::test_happy_path_bos_upload` |
| `image` 只接受**字符串引用**（data URI / URL / 裸 b64） | `media.resolve_input` 三形态归一 | `test_api.py`、`media.py` 注释 |
| `image` 不收数组/对象 | `service.validate_request` → 400 `invalid_image` | `test_request_validation_errors` |
| 空数组 = 没传 | 同上（`[]` 归一为 `None`） | `test_empty_image_array_means_absent` |
| 类型按**真实字节**嗅探，不信 `Content-Type` | `media.sniff` | `test_api.py` 各类输入 |
| 载体编码过 SSRF 白名单 | `upstream/baidu/netguard.py` | `test_ssrf.py`（28 例） |
| 校验是纯函数，无网络/厂商判断 | `validate_request` 只读 payload | `test_api.py` |
| 不认的字段 drop 或 400，不静默丢 | `_ALLOWED_KEYS` → 400；`_KNOWN_UNSUPPORTED` → `unsupported[]` | `test_known_unsupported_fields_are_reported_not_swallowed` |
| `model` 回显客户端原名 | 响应 `model` = `wenxin:*` | `test_api.py` |
| `n` ≠ `sequential_image_generation` | 两者都进 `unsupported[]`，**不互映射** | 同上 |
| 归因按「谁的错」翻，不照抄上游码 | `UPSTREAM_KIND_STATUS`：auth→503 / risk→429 / timeout→504 / upstream→502 | `test_api.py` 各错误用例 |
| 错误报文不带凭据 | `netguard.redact_url` 剥 query；报文只留 scheme/host/path | `test_ssrf.py::test_error_message_does_not_leak_signed_query` |
| 断言放在最深观测点 | 断言 `body["error"]["code"]`、反序列化后的字段面 | 全部用例 |

`normalize()` 口径只登记一处：默认 `response_format` 在 `service.validate_request`，
文档在 `INTERFACE.md §2`，没有第二种读法。

---

## 2. 有理由的差异（刻意不照搬）

| 契约条款 | 不照搬的理由 |
|---|---|
| **`size` 三形态吸附到最小合法像素** | 上游没有像素档位语义，只有「扩图比例」。硬把 `1024x1024` 吸附成 `1920x1920` 是**制造假能力**。现行：`expand` 消费并把 `WxH` 化简为比例；其余能力忽略 + `warnings[]` 明示 |
| **`watermark` 默认关且显式下发** | 上游请求体里没有这个开关（只有结果 URL 的水印参数）。能做的是**剥离结果 URL 的水印参数**（`BAIDU_STRIP_WATERMARK`），已做。`watermark` 字段进 `unsupported[]`，不假装生效 |
| **`response_format` 不归一化** | 本条在 apiduo 的落点是「归一到 url」；本服务相反，默认 `b64_json`。理由：上游给的是**带签名的临时 URL**，直透等于把会过期的地址交给调用方；b64 是稳定副本。`url` 模式回 `/files/{name}` 的**相对路径**（落盘、无 TTL）——比签名临时 URL 稳定，但需调用方拼 base |
| **收敛成方舟错误码表（8 个码）** | 本服务是**单一上游的专用出口**，不是多租户网关。`missing_mask` / `unknown_style` / `risk_control_cooldown` 这一级细码的诊断价值，高于把它们压成 `InvalidParameter`。骨架仍是 `{error:{code,message}}`，加性键（`kind`/`upstream`/`frames`）不改变语义 |
| **错误报文隐藏上游身份** | 本项目的定位就是「文心助手的出口」，README 直说上游是谁。脱敏的目标是**凭据**（签名参数、token、cookie），不是厂商名 |
| **多图 `image: []` 保序转发** | 上游是单图语义（`UPSTREAM.md`），多图不是「另一种接法」，是另一个协议 ⇒ 明确 400，不做「取第一张」的静默降级 |

---

## 3. 本次补掉的缺口（2026-11-19）

| # | 缺口 | 修法 |
|---|---|---|
| 1 | **外链代取无 SSRF 防线**：`fetch_url` 不校验 scheme、不拦私网/环回/元数据地址，`follow_redirects=True` 让 `302 → 127.0.0.1` 直接绕过 | 新增 `upstream/baidu/netguard.py`：scheme 白名单 + 字面量 IP/本机名/内网后缀 + DNS 全答案校验；**重定向自己跟**（`follow_redirects=False`，最多 5 跳，逐跳过门） |
| 2 | **`image` 传数组报「缺少输入图片」**：归因错误，调用方会去补图，真正的问题是多图语义 | 明确 400 `invalid_image`（含 `image_type`），`[]` 归一为「没传」 |
| 3 | **错误报文里带上游签名 URL**：`url[:120]` 会把 `?q-ak=…&sign=…` 写进响应和日志 | `redact_url` 只留 `scheme://host[:port]/path` |

变异自证（删掉守卫，用例必须转红）已跑：
- 还原 `follow_redirects=True` ⇒ `test_fetch_stops_after_redirect_limit` 红；
- 删掉 `await check_url(current)` ⇒ `test_fetch_blocks_redirect_to_loopback` 红；
- 删掉 `invalid_image` 分支 ⇒ `test_request_validation_errors` 红。

---

## 4. 已知缺口（本次未改，需决策）

**`data[].url` 是相对路径**（`/files/<sha1前20>.jpg`），不是 image-adapter 契约里那种
可直接访问的绝对地址。实测后果：拿它回灌 `image` → 400 `invalid_input_format`。

三种改法，各有代价：
1. 从请求推导（`request.base_url` + `X-Forwarded-*`）—— 反向代理头可被伪造，内网调用会
   得到 `127.0.0.1:39014` 这种不可用地址；
2. 加配置 `BAIDU_PUBLIC_BASE_URL` —— 可控，但多一个旋钮（本项目纪律：每个旋钮必须有读者）；
3. 保持相对路径 + 文档写明 —— 零改动，把拼接责任交给调用方。

本次选 3（不擅自改契约），文档已在 `INTERFACE.md §6` 写明。

## 5. 残余风险（不假装已解决）

1. **DNS rebinding**：解析与连接之间目标可被换成内网地址。彻底消除要把校验后的 IP
   钉进连接（自定义 transport + SNI 处理），本服务不做 —— 收益不抵复杂度。
2. **走代理池时**（`BAIDU_PROXY_POOL` 非空）：DNS 由代理解析，本机 `getaddrinfo` 的
   结果不代表代理侧结果 ⇒ 第三层在这条路径上退化。scheme 与字面量两层仍有效。
3. **多图不支持**：上游单图语义，非本服务遗漏。
4. **`/files/{name}` 无 TTL**：由部署方清理（`INTERFACE.md §6` 已登记）。
