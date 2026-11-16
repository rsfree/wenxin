# 上游取证与实现说明（文心助手 · chat.baidu.com）

> **唯一研究真源**在另仓：`reverse-proxy/docs/upstream/wenxin-image-edit-api.md`（实证版契约，
> 含完整逆向过程与实验记录）与 `reverse-proxy/wenxin/probe/`（探针）。
> 本文只留**本服务实现所依赖的结论**与**工程取舍**，不复制研究过程。

---

## 0. 一句话

文心助手「图片编辑」= **一条同步 SSE 接口**（`POST /aichat/api/conversation`）+
**Cookie（免登录也需要）** + **body 内自算 `chat_token`**；
门禁是**出口维度的风险评分挑战**（昆仑），不是计数器频控。

---

## 1. 请求骨架（本服务逐字复刻）

端点 `POST https://chat.baidu.com/aichat/api/conversation`（`Origin/Referer` 指 wenxin.baidu.com）。

**能力由三处同时决定**（`app/upstream/baidu/capabilities.build_body` 从同一来源派生）：

| 位置 | 取值 |
|---|---|
| `searchInfo.mcpInfo.ext.type` | toolType 字符串（如 `"3"`） |
| `searchInfo.sa` / 顶层 `sa` | `workspace_piccreate_<toolType>` |
| `query[1].data.text.query` | 能力中文名（如 `变清晰`）——**必须与 `chat_token` 的 md5 完全一致** |

> ⚠️ **两种请求形状**（2026-09-24 实测）：以上是 **workspace 形状**（默认，覆盖绝大多数能力）。
> 少数工具在站点 UI 里走**工具入口形状**：`searchInfo.sa = searchbox_image` +
> ⚠️ 另有**专属 sa 覆写**：换风格实测 `sa=workspace_piccreate_hfg`（不是 `workspace_piccreate_14`），
> 且 `ext` 里带 `style`(id)/`text`(标签)、`image_source=1`，`query[TEXT]` 为**风格标签** ——
> 见 §7 的攻克记录。
>
> `searchInfo.enter_type = pic_picfunc_<N>` + **无 `mcpInfo`** + `query[TEXT].query = 自然语言指令`
> （UI 实测入口码：背景替换 11 / 风格转换 14 / 局部替换 5 / 涂抹消除 8 / 相似图 7）。
> 本服务对 `restyle`/`bgreplace` 已按该形状构造（`Capability.entry_type`），但**实测这两条
> 仍不出图**（编辑器/agent 形态）⇒ 门禁；详见 §7。

`chat_token = btoa(<8位session token>|<md5(query)>|<ts_ms>|<lid>)-<lid>-3`：

- `lid` 必须是**页面级 searchframeLid**（用会话 `ori_lid` 会稳定吃 `tokenFail`，
  而报错文案只说「出了点小问题」，极易误判成风控）；
- `session token` 每次加载页面都会变、实测存活 ≥10 分钟 ⇒ **不手工填，现取**。

## 2. 响应骨架（SSE）

```
帧序：basedata → waiting-resp → markdown-yiyan（文案）→ image-generate（图）→ generate-complete → endTurn
```

- 结果图：`generator.component == "image-generate"` 的 `data.items[{originUrl,previewUrl,width,height}]`；
- 🔴 `items` 会被上游**显式给成 `null`**（此时可能只给一个 `picEditBaseUrl` 的 App 深链）
  ⇒ 消费侧必须写 `data.get("items") or []`；
- 🔴 **首帧顶层 `chatHitKunlun`**（约 0.1s 可知）= 风控信号；命中即断连、**不重试**。

## 3. 凭据模型（本服务的关键结论）

| 凭据 | 必需性 | 说明 |
|---|---|---|
| `BAIDU_COOKIE` | **必需**（唯一硬条件） | 免登录 ≠ 免 cookie：去掉 cookie 立刻「服务繁忙」无图（A/B 实证） |
| `token` / `lid` | 可选 | 首页 HTML 内嵌（`aiTabFrameBaseData`），**现取 + TTL 540s 缓存** |
| `BAIDU_SESSION_TOKEN` / `BAIDU_LID` | 可选 | 只在排障/上游改版时钉值；显式配置时**不自动现取、失败不重试** |

**cookie 从哪来**（免登录、零生成请求）：

```bash
python wenxin/tools/mint_identity.py            # 在 reverse-proxy 仓；产物 wenxin/var/identity.json
# 该文件 {"cookie": "BAIDUID=…; BIDUPSID=…", …} —— 把 cookie 字段填进 BAIDU_COOKIE
```

- 实测（2026-09-24）：**纯 HTTP 铸造的 7 项 cookie 即可跑通完整流程**（真实出图验收）；
  不需要浏览器、不需要登录；curl_cffi/Playwright 档保留为兜底。

## 4. 输入图：三级策略

| `BAIDU_UPLOAD_MODE` | 行为 | 建议 |
|---|---|---|
| `bos`（**默认**） | 恒转存：代取 → 标准化 → BOS 四段式上传 → 登记 → CDN URL | 生产推荐（多花 1~2s 换稳定） |
| `direct` / `auto` | 外链直传；非 URL 输入自动降级为转存 | 图小、MIME 标准、域名主流时可用 |

- 上游**只认自己抓得到的 URL**；外链直传失败的根因是「百度服务端抓取失败」
  （体积/非标 MIME 两大诱因），不是"外链不支持"；
- BOS 链（`app/upstream/baidu/client.upload_image`，纯 HTTP、免登录、不计费）：
  `GET /aichat/api/file/sts` → 初始化分片 → PUT → 合并 → `POST /aichat/api/file/upload`，
  签名 = **BCE v1**（`bce-auth-v1/...`）；
- 🔴 `jpg` 必须映射 `image/jpeg`（写成 `image/jpg` 是历史上的真实失败诱因）；
- 转存前标准化（`BAIDU_NORMALIZE=1`）：限最长边 ≤2048、≤2MB、统一标准 MIME；
  透明通道保留 PNG；**解不开的图原样上送并留 warning**（magic 已证明它是图片）。

## 5. 风控（昆仑）—— 本服务限流设计的依据

> 研究结论（截至 2026-09-24，含两轮复核）：

- **~~不是身份/cookie 维度~~（2026-11-17 修正，见 §11）**：2026-09-24 的「热/新/无 cookie
  三方负对照同刻同型命中」是在**出口已被长期标记**的状态下做的 —— 那个状态下确实换身份无效。
  但在**干净出口上被短时打标**的状态下，换新身份同 IP **立即恢复**（实测）。
  ⇒ 正确模型是「(出口, 身份) 二维评分」，不是单一维度。
- **不是计数器频控**：45 次连打全成功已证否；受控诱导 30 次（24 次/分）也未触发。
- **是出口（IP）维度的风险评分 → 验证挑战**：
  - 换到**未滥用**出口立即恢复（实证）；
  - **滥用一轮会把整片出口/池一起覆盖**（"换 IP 无效"的观感由此而来）；
  - 「客户端 TLS/JS 指纹」轴**未被单独隔离**（无正面证据，也未证伪）。
- 首帧 `chatHitKunlun` 命中 ⇒ 本服务**立即断连 + 开冷却窗（默认 600s，窗内零上游请求）**。

**因此本服务的策略**（进程内闸门，单 worker）：

1. **降速不触发**：串行 + 最小间隔 4s + 每分钟 8 次（`BAIDU_MIN_INTERVAL/PER_MINUTE`）；
2. **命中即熔断**：冷却窗（`BAIDU_COOLDOWN`）+ `/readyz` 可见；
3. **换线靠真出口**：`BAIDU_PROXY_POOL`（逗号分隔；每请求新建连接轮换）——
   这是**唯一被证明有效**的换线手段；**伪造 XFF/头部未被证明有效**（未做实证隔离实验）。
   ⚠️ 用它跑量会把整池出口一起覆盖，由部署方决定。

## 6. 结果与水印

- 结果 URL 尾部带 `?x-bce-process=image/watermark,...`（AI 水印处理参数）；
  **默认剥离**（`BAIDU_STRIP_WATERMARK=1`）⇒ 下载即无水印原图；
- 本服务把结果图**取回并转存**（上游 URL 带签名、有有效期）——调用方拿到稳定副本。

## 6.5 老接口（image.baidu.com/aigc）—— 兜底链路的取证（2026-09-24）

**结论：活着、匿名可用、秒级出图；全量类型矩阵已实测。**（旧研究里"已失效"的说法是误判
—— 那次的「网络异常」实为**服务端抓不到输入 URL**，与 `picInfo` base64 直传无关。）

链路（复刻自 `MeUtils/meutils/apis/baidu/bdaitpzs.py`）：

```
POST {LEGACY_BASE}/aigc/pccreate     表单：type=<tt> / picInfo=<纯 base64> / picInfo2=<遮罩> / …
  → {"status":0,"pcEditTaskid":"daq25sufqu04ejcm9j00","resType":0,"token":"…"}
GET  {LEGACY_BASE}/aigc/pcquery?taskId=…
  → {"isGenerate":true,"progress":100,…,"picArr":[{"src":"data:image/jpeg;base64,…"}]}
```

- **凭据**：同款百度系 cookie（本服务的 `BAIDU_COOKIE` 直接可用；实测匿名即可）；
- **输入**：`picInfo` 直传 base64（无需先上传、绕开"抓不到 URL"类失败）；`picInfo2` = 遮罩位（见下）；
- **type 全量矩阵（每型 ≥1 次真实出图；经本服务端到端 3.1–3.2s，比主链 6.0s 还快
  —— 免 BOS 上传与 chat_token 链路）**：

  | type | 能力 | 实测结果 | 附加参数 |
  |---|---|---|---|
  | 3 | 变清晰 | ✅ 3600×2400 | — |
  | 1 | 去水印 | ✅ 3.1s / 900×600 | — |
  | 9 | 抠图 | ✅ 4.1s / 900×600 | — |
  | 15 | 提线稿 | ✅ 10.8s / 3600×2400 | — |
  | 4 | 扩图 | ✅ 1368×1024 —— **慢，首次轮询停在 `progress:99`，长轮询 11.6s 才出图** | `ext_ratio` |
  | 8 | 消除 | ✅ 7.3s / 900×600（经服务） | `picInfo2` |
  | 5 | 局部替换 | ✅ 5.5s / 900×600（经服务，`prompt`→`text`） | `picInfo2` + `text` |
  | 6 | AI重绘 | ✅ 7.2s / 1440×960（经服务） | `create_level=2` |
  | 7 | 相似图 | ✅ 10.7s / 900×600（经服务） | `create_level=5` |
  | 12 / 14 | 背景替换 / 风格 | ⛔ **未实测**：需 `text`/`style` 语义字段，本入口面无对应字段 ⇒ 不接 | — |

- **遮罩语义（判决实验，2026-09-24）**：`picInfo2` = **黑底 + 白框**（**白色=要处理的区域**）。
  合成目标（红块 60000 px）对照：**黑底白框 → 残留 0**（完全消除）；白底黑框 → 残留 59972
  （未处理）。⇒ **反约定无效**，别用白底黑框。
- **"慢"≠失败**：`type=4` 的 `progress:99` 是假终点 ⇒ 轮询预算按 ≥60s 配
  （`LEGACY_POLL_TRIES × LEGACY_POLL_INTERVAL`，默认 40 × 1.5s）。
- **与主链的关系**：不同 host / 不同产品 ⇒ 昆仑挑战（只卡 chat.baidu.com 的 conversation）
  **不覆盖它** —— 这是「风控兜底」成立的结构性依据。⚠️ 但「主链被标记时老接口仍通」
  这一格**尚未在实时被标记场景下对照过**（手上没有被标记出口）；上线
  `BAIDU_LEGACY=fallback` 后一旦发生风控会自动切换并留痕（`effective.path=legacy`）。
- 老接口自身的风控/限流**未观测到**（本地调用量小）—— 按同样的纪律使用（别连打）。

---

## 7. 未取证 / 刻意不做（别假装知道）

**主链侧**：

- 去水印（toolType 2）：2026-09-12 起复测为「跳交互式编辑器」形态（`items:null` + 深链），
  未复现出图 ⇒ 默认门禁（**老接口 type=1 已补位**）；
- ✅ **换风格（toolType 14）2026-09-24 攻克**（从站点真实报文逆出，已上线）：
  `sa=workspace_piccreate_hfg`（**专属字母码**）+ `enter_type=pic_picfunc_14` +
  `mcpInfo.ext{type:14, image, image_source:**1**, channel:edit, style:<id>, text:<标签>}` +
  `query=[IMAGE, TEXT(<标签>)]` ⇒ **出图**（真跑：宫崎骏风 10.3s / 油画风 10.7s，900×600）。
  风格表 = `GET image.baidu.com/aigc/extinfo` 的 `style[]`（**17 项 id↔标签**，服务端下发）。
  对照（三条都试过、都不出图）：旧 workspace 形状（`sa=workspace_piccreate_14`、无 style/text、
  `image_source=0`）只回 `picEditBaseUrl`（跳编辑器）；工具入口形状（无 mcpInfo）只回对话文字；
  老接口 `type=14` 九次尝试恒 status 5。
- ✅ **背景替换（toolType 11）2026-09-24 攻克**（经老接口）：主链形态不可用（workspace 回「未识别到主体」、
  工具入口形状只回对话文字），但**老接口 `type=12` + `picInfo2`(遮罩) + `text` 实测出图**
  （全白遮罩 92KB / 上半框遮罩 76KB；经服务 4.1s / 3.8s，900x600）。
  🔴 **此前 7 次失败全因没带遮罩** —— 站点该工具面板正是「识别背景 / 涂抹要替换区域 + 输入替换的内容」；
  遮罩语义与消除/局部替换一致（黑底白框，白=要处理）。
  两能力的**站点入口码实测**：背景替换 `pic_picfunc_11`、风格转换 `pic_picfunc_14`、
  局部替换 `_5`、涂抹消除 `_8`、相似图 `_7`；
- 局部替换 / 消除：主链需**涂抹遮罩**，遮罩参数未逆向 ⇒ 主链形态默认门禁
  （**老接口遮罩语义已判决并生效**，见 §6.5）；
- **toolType 13 / 28~34：不是能力**（2026-09-24 实测）—— 回 `image-generate` + `items:null` +
  `picEditBaseUrl`，且链接里 `toolType=0&word=` **为空** ⇒ 上游对「未知 toolType」的**通用编辑器兜底跳转**。
  §3 曾把这记为「相关图编 / 出图 ✅」，**是对兜底响应的误读，已撤**；该条目**不注册**（见 `NOT_REGISTERED`）；
- 生成文案（toolType 16）：不产图，刻意不暴露；
- 冷却时长（命中后多久自动解除）：**未定论**；本服务保守取 600s。

**老接口侧**：

- 🔴 **教训（2026-09-24 当日自我纠正）**：`type=12`（背景替换）曾被判「已死」——
  七次尝试（两种端点 × 两种输入形态）create 恒被拒。**该结论是错的**：那七次**都没带遮罩**，
  而遮罩是该 type 的**必需参数**（站点 UI 面板正是「涂抹要替换区域 + 输入替换的内容」）。
  补上 `picInfo2` 后**立刻出图**（全白遮罩 92KB、上半框 76KB；经服务 4.1s / 3.8s）。
  ⇒ **判死前必须把该 type 的必需参数集试全**：`参数不全的失败 ≠ 编号不存在`；
  凡"涂抹/局部"类工具，先怀疑缺遮罩。
- 风格(14)：**2026-09-24 判死（有对照）** —— 九次尝试（5 个 `style` 值 × 两种端点 × 两种输入形态），
  create 受理但任务恒 `status:5`；**同期 `type=3` 对照 4/4 出图**（对照有效）。
  ⚠️ 但同样的能力在**主链**用专属形状可用（见上：`workspace_piccreate_hfg` + `ext.style/text`）—— 
  「老接口编号死了」≠「能力做不了」，两条通路要分别下结论。
- ⚠️ 老接口 `type=6/7` 与新接口 toolType `6/7` **不是一回事**：新接口 6/7 返回「服务繁忙」
  （编号不存在），老接口 6/7 = AI重绘/相似图 —— **两套编号体系别混**。

---

## 8. 复测记录：主链「服务繁忙」的**真因是输入图 URL 不可抓**（2026-11-16）

**症状**：主链 `wenxin:clarity` 恒回「抱歉，服务繁忙，请稍后再试」，5 帧、`kunlun` 为空
（不是昆仑挑战）。按 §3 的 A/B，该文案首疑「cookie 不被接受」——**这个归因是错的**（见下）。

**三步隔离（同机、同参数，每一步只改一个变量）**：

| # | 改的变量 | 身份 | 输入图送法 | 主链结果 |
|---|---|---|---|---|
| 1 | 基线 | 浏览器导出（~20 项 cookie） | BOS 转存（**自己拼的 URL**） | ❌ 5 帧 / 服务繁忙 |
| 2 | **只换身份** | 现场铸造（4 项，`scripts/mint_identity.py`） | 同 #1 | ❌ **表现完全一致** |
| 3 | **只换输入送法** | 同 #2 | **外链直传**（页面给的带签名 CDN URL） | ✅ **4.1s，1616×1616** |

**定因**（同一个对象、两种 URL，匿名 GET）：

```
无签名  https://<CDN_BASE>/<prefix><name>        → HTTP 403（101B 错误体）
带签名  <file_url>?authorization=bce-auth-v1/…   → HTTP 200（32377B 原图）
```

`POST /aichat/api/file/upload` 响应里的 `data.file_url` **正是带签名的那一个**
（24h 有效期），而 `upload_image()` 当时丢弃它、自己拼了无签名地址。
上游抓不到输入图，就只回一句笼统的「服务繁忙」——**与 cookie 缺失的症状逐字相同**。

**修复**：`upload_image()` 优先返回登记录下发的 `file_url`，仅在缺失或非绝对地址时回退拼装
（`tests/test_client.py::test_upload_prefers_signed_file_url_from_registration` 守这条）。
修复后 **本地字节 → BOS → 主链** 打通：7.0s，1616×1616。

**旁证（本来就在指向这里）**：同 IP、同身份下**老接口一直是好的** —— 它是 `picInfo`
**base64 直传**，整条链路根本没有「上游去抓 URL」这个环节。当时把这条差异当成
「两条链路门禁独立」，其实是把答案当成了干扰项。

**教训（比结论更重要）**：

1. **一次只改一个变量**。#1↔#2 只换身份 ⇒ 干净地否掉「身份」假说；#2↔#3 只换输入送法
   ⇒ 一击定因。三个变量一起怀疑，只会得到「都像、都不确定」。
2. **笼统错误文案是最大的排障陷阱**。「服务繁忙」在 §3 由 cookie 缺失导致、在这里由
   输入图 403 导致 —— **同一个字符串，至少两种病因**。
3. **上游已经给了字段就别自己拼**。登记响应里的 URL 是带签名的可用值，
   拼装值是「看起来对、抓起来 403」的定时炸弹。
4. **「免费/免登录」不等于「无外部依赖」**：这条链依赖上游**能访问到我们给的 URL**，
   任何自建存储一旦收紧 ACL，症状会伪装成风控。

---

## 9. 照片对照：推翻「弱能力」判断 + 文字替换第 4 次复现（2026-11-16）

**背景**：用白底矢量 logo 跑工具栏 11 项时，`提线稿`近乎空白（暗像素 0.09%）、
`AI重绘`/`图片修复`差异极小（Δ2.6）、`相似图`文字糊化 —— 一度被读成「能力不行」。

**对照实验**：换一张**真实照片**（2048²，雨夜街道 + 猫 + 日文霓虹招牌）重跑：

| 能力 | 输出 | 与源图 Δ | 判定 |
|---|---|---|---|
| 提线稿 | 4096×4096 **L** | **160.8** | 暗像素 **11.48%** ⇒ 真有轮廓线，可用 |
| 相似图 | 512×512 | 24.4 | 构图/姿态明显变化 ⇒ 生效 |
| 变清晰 | 2048×2048 | 11.4 | 可用 |
| AI重绘 | 1408×1408 | 10.6 | 保守重绘，可用 |
| 图片修复 | 1408×1408 | 9.6 | 轻度降噪/调色，可用 |

⇒ **同一批能力，换输入就从「近乎无输出」变成「正常产出」。** 白底矢量 logo 是分布外样本；
判能力好坏必须用**该能力的典型输入**（照片），否则测的是"输入不匹配"，不是能力。

**同一张照片上的文字替换**：仍是 6 帧 + `items:null` + `picEditBaseUrl`
（第 4 次观测、第 3 类输入）⇒ 与输入无关，是该 toolType 已转编辑器形态，
故登记为 `verified=False`（见 `app/models.py`）。

---

## 10. 老接口 `type=1` 的 resType=2 是**内容**拒绝，不是体积（2026-11-16）

**现象**：`wenxin:dewatermark` 主链回编辑器链接（与 §8.3 一致）后，兜底走老接口 `type=1`，
被 `resType=2` 拒。错误文案把该码解释成「入参体积/内容」，容易被读成"压缩没做好"。

**交叉对照**（同一份压缩结果，同一条链路，只换 type）：

```
fit_legacy_image(2048² → max_side 640 / max_bytes 40000)
  → 512×512, 39519 B  （确在限额内）

type=1 去水印    → ✗ resType=2
type=3 变清晰    → ✓ 出图 370514B（task=dbtdjjdlnld66meqjfug）
```

⇒ **入参体积没问题**，是 `type=1` 对这份**合成水印**（半透明白字叠加）不认。
真实水印（2026-09-24 那份证据）能否过，用合成图判不了 —— 与「白底矢量 logo 不能判照片向能力」
是同一条教训的两面：**测什么，得先有该能力的典型输入**。

**处置**：不改代码（压缩实现 `fit_legacy_image` 会逐级降质再缩边，实测能把 5.6MB 压到 39.5KB）；
不改注册表（`dewatermark` 仍是 `verified=False` + 老接口映射，开 `BAIDU_LEGACY` 即可尝试）。
待有**真实带水印照片**时复验。

**附带发现（待人像对照）**：`wenxin:beauty`(toolType 24) 在**猫的照片**上返回编辑器链接。
美颜是面向人脸的能力，猫属分布外样本 —— 按上面的教训，**不据此判死**，需一张人像复验。

---

## 11. 出口打标实验：频控没有阈值，只有评分；身份能短暂复位，出口会被永久记账（2026-11-17）

**动机**：需要回答「IP 频控是多少、能否绕过」。§5 只有定性结论，没有速率数据。

**方法**（旧 node-4 `61.111.253.121`，首尔 MOACK AS138195；**专用消耗机**，非生产出口）：

- 同机起 N 个单 worker 实例，`BAIDU_MIN_INTERVAL=0` / `BAIDU_PER_MINUTE=100000` / `MAX_WAIT=0`
  ⇒ **把本服务的进程内闸门完全摘掉**，让"并发/速率"成为唯一变量；
- 每实例同时只允许 1 个在途请求（否则拿到的是本服务 429，不是上游行为）；
- 模型 `wenxin:clarity`、同一张 404² 图、`BAIDU_LEGACY=off`（强制主链，避免兜底掩盖信号）；
- 判据：`code=risk_control`（首帧 `kunlun_popup`，HTTP 429，耗时从 ~9s 掉到 ~2.2s）。

**结果**：

| # | 身份 | 名义速率 | 实际 | 结果 |
|---|---|---|---|---|
| 1 | cookie-1（新铸） | 阶梯首级（驱动器 bug ⇒ 满并发） | **~53 次/分** | 55 发成功后命中；随后 15/30/60/120/189/251/313/376/438/501s 单发探测**全部仍拒**；首次命中后 **17 分钟**再测仍拒（未测到恢复边界） |
| 2 | cookie-2（新铸） | 24 次/分 | ~25 次/分 | **29 发成功后（+72.5s）命中** |
| 3 | cookie-3（新铸） | 12 次/分 | — | **第 1 发即命中**（0 成功） |

**2×2 隔离（同一时刻、同一 IP、同一图）**：

| 组合 | 结果 |
|---|---|
| 旧 cookie + 旧进程 | ✗ risk_control 2.1s |
| 旧 cookie + **新进程**（token/lid 现取） | ✗ risk_control 3.0s |
| **新 cookie** + 新进程 | ✅ 1616×1616 8.3s |

⇒ 差别只在 cookie：**不是进程缓存、不是 token/lid 过期**。

**同一时刻，老接口（`type=3`）用旧身份与新身份各一发，全部成功**（4.1s / 4.0s，1616²）
⇒ **两条链路的风控互不相干**，主链被判死不影响兜底。

**结论（三条，按可信度排序）**：

1. **不存在"IP 频控 = N 次/分"这样的阈值**。触发点随速率与身份历史滑动：
   53 次/分 → 55 发；25 次/分 → 29 发；而**已打标的出口上，12 次/分也会首发即拒**。
   这与 §5「风险评分」一致 —— 评分是连续量，问"阈值是多少"本身就不成立。
2. **身份（cookie）是评分的一个自变量，但只能短暂复位**：干净出口上换身份立即恢复；
   可一旦出口被打标到一定分数，**新铸身份的第一发就被拒**（#3）。因此"轮换 cookie 跑量"
   不是可持续的绕过手段 —— 它买到的窗口约 30–60 发，而代价是把出口推入"换身份也无效"的状态。
3. **真正可用的降级是走老接口**（本服务 `BAIDU_LEGACY=fallback` 已内置）：
   主链被挑战时它照常出图，且它对 IP 不敏感（历史取证：限流粘在 cookie 上）。

**对本服务配置的含义**：生产默认 **4s 间隔 / 8 次每分**，低于本轮所有未打标状态的触发点
（25 次/分在 72s 才失守），余量约 3 倍；**无需调整**。命中后开 600s 冷却窗、不重试的既有设计，
被本轮"命中后 17 分钟仍未恢复"的数据支持（600s 窗是下限）。

**未取证**（不要当成已知）：

- 未测「伪造 `X-Forwarded-For` / `X-Real-IP`」是否影响评分（§5 记为未证伪）；
- 未测「换真出口立即恢复」（本机只有单个公网 IP，`BAIDU_PROXY_POOL` 无样本）；
- 未测出口打标的**自然过期时间**（17 分钟仍在拒，未测到边界）；
- 单机单 /24 单次实验，未做跨机器复现。

---

## 12. 文生图：同一条 conversation 接口的「无图」形状（2026-11-17 抓包 + 端到端复核）

**来源**：bsk 驱动文心页面真实画了一次（4 张 864×1152），在页内装 `fetch`/`XHR` 钩子
抓到请求体；再用本服务按同形状复跑，结果一致。

**形状（与 workspace 图片编辑形状的三处差异）**：

| | 图片编辑（原有） | 文生图（新增） |
|---|---|---|
| `query` | `[IMAGE, TEXT]` | **`[TEXT]`**（只有提示词） |
| `mcpInfo` | `{type:"workspaceImage", ext:{type,image,...}}` | **不存在** |
| `sa` / `enter_type` | `workspace_piccreate_<tt>` / `unknown` | **`bkb` / `sidebar_dialog`** |
| `image` 参数 | 必填 | **必须不传**（传了即 400） |

另有 `chatParams.chat_samples="WISE_NEW_CSAITAB"`、`isInnovate=2`、
`capsuleSelectMode="fast"` 三个字段，抓包里都有，已一并复刻。

**端到端结果**：默认配置（无需 `ALLOW_UNVERIFIED`）下
`POST /v1/images/generations {"model":"wenxin:draw","prompt":"…"}`
→ **17.3s / 12 帧 / 4 张 864×1152 `image/jpeg`**，`upstream.path="conversation"`。
SSE 解析器**无需改动** —— 图片帧结构与编辑链路同源。

**能力面影响**：注册 19→20，默认可用 15→16（`/v1/models`、`/readyz`、CI 冒烟三处同步）。

**仍未取证**：图生图（带图的自然语言改写）与多图生图（多张参考图）需要再抓一次
「上传图片 + 提交」的报文；当前**不登记、不假装支持**。`wenxin:redraw` / `wenxin:similar`
仍覆盖"整图重绘 / 相似变体"这两类常见需求。

---

## 13. 文生图的频率特征：不是速率阈值，是「每身份预算 ≈45–55 发」（2026-11-17）

**方法**：旧 node-5 `61.111.253.131`（未用于生产），挂载含 `wenxin:draw` 的代码，
起 12 个单 worker 实例、闸门归零、每实例单在途；单发 p50≈21s，每发 4 张 864×1152。

| 轮次 | 身份 | 速率 | 发出 | 成功 | 结果 |
|---|---|---|---|---|---|
| 1 | cookie-5（新） | 12 次/分 × 180s | 36 | 36 | ✅ 零风控 |
| 2 | cookie-5（已用 36） | 24 次/分 | 20 | 19 | ⛔ 47.5s / **累计 55 发**命中 |
| 3 | cookie-6（新） | 18 次/分 × 180s | 45 | 44 | ⛔ 146.7s / 44 发命中 |
| 4 | cookie-7（新） | 24 次/分 | 1 | 0 | ⛔ **首发即拒** |

**判读**：

1. **第 3 轮 44 发、第 2 轮累计 55 发** —— 同量级，而速率相差 33%。
   与 §11 编辑链路（25 次/分 29 发、53 次/分 55 发）合并看，最稳的模型是
   **「每身份累计预算」**，不是「每分钟 N 次」。
2. **12 次/分 × 3 分钟安全**（36 发）。生产默认 8 次/分 ⇒ 余量约 1.5 倍。
3. **第 4 轮复现了 §11 的出口打标**：两轮之后，新铸身份的第一发直接被拒。
   ⇒ 再次证明「轮换 cookie 跑量」不可持续，只会把出口推进硬打标状态。
4. 文生图每次请求产出 4 张，**上游算力消耗显著高于编辑链路**，但预算量级相近
   （44–55 vs 29–55），说明预算大概率按「请求数」而非「输出图数」记账。

**未取证**：预算是否按 24h 滑窗重置（未等到重置）；不同出口/机房是否同一预算尺度。

---

## 14. 图生图 / 多图生图：两条形状都试过，**都不成立**（2026-11-17，负结果）

**目标**：给一张图 + 一句自然语言 → 出改写后的图；以及多张参考图。

**试过的形状（都是真实调用）**：

| 变体 | 形状 | 结果 |
|---|---|---|
| A | chat 形状（`sa=bkb`、无 `mcpInfo`）+ `[IMAGE, TEXT]` | 12 帧、**零图**、返回文字与追问（"要不要给整个毛毡 logo 添加…"）⇒ 被当成**看图对话** |
| B | workspace 形状（`sa=workspace_piccreate_3`、带 `mcpInfo`）+ TEXT=prompt | 6 帧、出图，但**prompt 被完全忽略**：输入"变成黑白线稿"，输出仍是彩色 logo 的 4× 放大版 |
| C | workspace 形状但 `ext` **不带 type** + TEXT=prompt | 5 帧、**零图**，`抱歉，服务繁忙` ⇒ 该形状不被受理 |

**三个变体全部否定**（A 变看图对话 / B 忽略 prompt / C 直接不受理）。
**结论**：文生图能用 `bkb` 形状，是因为"只有文字"时上游默认走画图；一旦带上图片，
同一条形状就被路由到多模态对话。而 workspace 形状的 TEXT 是**工具名槽位**，
自然语言塞进去不生效（它会按 `ext.type` 去跑对应工具）。

**这说明图生图需要第三种形状**：图片先进"图片生成工作台"（上传/生成后的上下文），
再由该工作台的输入框提交 prompt。这个上下文不是靠改 payload 能伪出来的。

**未取证的卡点**：把图片送进图片生成工作台需要**上传**，而 `bsk upload` 在本机对文件输入
返回 `-32000 Not allowed`（§ 之外的已知环境限制，裸 `input[type=file]` 同样失败）。
⇒ 要拿到这条形状，必须在真实浏览器里**人工上传一次图片**并提交，同时抓包。

**因此本轮不登记 `wenxin:img2img` / `wenxin:multi2img`** —— 不制造假能力。
现有 `wenxin:redraw`（整图重绘）/ `wenxin:similar`（相似变体）仍覆盖"图→图"的两类常见需求，
但它们不吃自然语言指令。

---

## 15. 非图片能力（PPT / 音乐）在匿名态不可用（2026-11-17）

**PPT 生成**：工作台形态是纯文本（主题 + 篇幅 + 视觉风格），与文生图同构。
但在匿名态提交时**前端直接弹登录框、零请求发出**：

```
请求数 0
页面：请使用百度APP扫码登录 … 点击登录，解锁全部AI功能
```

⇒ 不是"请求失败"，是**提交前就被前端拦掉**。要抓形状必须先有登录态。

**音乐生成**：模板是「写一首 X 的歌曲」+ 选曲风/音色。用 `bkb` 形状直接发文本指令，
请求能到上游（**9 帧**），但**零图、零文、零链接**——`SseOutcome` 三样都为空。
结合 PPT 的表现，最可能的解释是上游返回了登录引导卡（帧内字段不在本服务的解析面内）。

**结论（对能力面的意义）**：

- 文心的**图片**生成/编辑是匿名放开的（本服务 20 项即是证明）；
- **文档/媒体生成**（PPT、音乐、写作）统一在登录墙后；
- 因此"继续扩能力"的边际收益已经很低：能匿名吸收的图片类基本吸收完毕，
  剩下的大类都要引入登录态（cookie 绑定账号、会过期、风险落在账号上）。

**如果将来确实要接**：先登录抓一次完整 SSE，重点确认**结果帧给的是文件直链还是编辑器链接**
——若是后者（与 `ps`/`matting-pro`/`textreplace` 同型），则同样不可做。


---

## 16. 音乐绕登录：对抗审查全过程（2026-11-17）

**问题**：§15 记录音乐在匿名态「9 帧零内容」。本节记录绕登录的全部对抗路径与
**每一层的判定结果** —— 结论是：**音频不可绕，但歌词可以匿名拿到**，已登记为
`wenxin:lyrics`（文本产出型）。

### 16.1 前端层（UI 弹窗）—— 已绕过，只证明「墙在前端也有」

文心工具页（wenxin.baidu.com）音乐面板：填 prompt → 回车 ⇒ **前端弹登录框、
零请求发出**（与 PPT 同型）。bsk 独立 scope 实测：

- 输入框 `#ci-editor` 是 contenteditable 三段模板，bsk fill 报 not fillable；
- 回车提交走 `launchSearch`：`interactionType=E` 时前端有硬检查
  `(E===4||E===9||E===13) && !uc()` ⇒ 弹 `qc()` 登录框并 **return，不发请求**
  （chat-main-pc bundle @84577 反编译所得；4=office_create 7? 实为编号表
  `photo_editing:8, ai_ppt:9, ai_music_create:13, ai_numerology:16…`）。

**结论**：前端墙可被手工构造 payload 绕过 —— 但后端还有第二道（见 16.2）。

### 16.2 后端层（conversation 接口）—— workspace 音乐形状被硬拒

直打 `POST chat.baidu.com/aichat/api/conversation` 的变体矩阵（匿名 cookie、
间隔 5s、原始帧落盘 `/tmp/music_frames_*.jsonl`）：

| 变体 | 形状 | 结果 |
|---|---|---|
| A | 裸 bkb + 「写一首…的歌」（TEXT-only） | **493–667 帧 / 9.2s / 两版完整歌词文本流** |
| B | bkb + `searchInfo.interaction_type:13` | 8 帧，`chatSearchTips{type:login-guide}` 卡 |
| C | `sa=workspace_musiccreate_13` + `mcpInfo{type:workspaceMusic,ext:{type:"13"}}` + `interaction_type:13` | 同 B，8 帧 login-guide |
| D | 同 C 但 `mcpInfo.type=workspaceImage` | 6 帧「相关图编操作完成」+ 编辑器深链（后端把 13 当**图编** toolType 兜底） |
| E | `sa=workspace_piccreate_13`（塞进图片命名空间） | 同 D |

**login-guide 卡的关键字段**（后端下发，不是前端造的）：

```json
{"type": "login-guide", "scene": "create_limit", "todayRound": 15,
 "leftTitle": "登录体验完整功能", "leftBtn": "登录"}
```

**换新匿名身份复测（C 变体）**：`todayRound` 从 15 → 2，**仍然 login-guide**。
⇒ `scene=create_limit` 的语义是「**匿名身份禁止创作类交互**」，不是配额耗尽
（新身份第一发就被拒）。conversation 接口对创作类 interaction_type 有**服务端
硬判定**，与 cookie 新旧、todayRound 数值无关。**不可绕**。

### 16.3 旁路收益：裸 bkb = AI 写歌词（匿名可用）

A 变体揭示了一条**可用的匿名旁路**：裸 bkb 对话形状下，「写一首…的歌」被上游
路由为 **AI 写歌词**（markdown-yiyan 文本流），产出的不是音频而是**歌词**：

- 493–667 帧 / 9.2–10s；两版歌词（如「松弛治愈风」「城市晚风治愈感」，
  含主歌/副歌/桥段/结尾）；换新匿名身份一致复现。
- §15 的「9 帧零内容」与此不一致 —— 真实原因见 16.4 的解析 bug：
  文本一直在流里，只是被 `apply_frame` 的覆盖语义吞掉了。

**追问卡片**（`inputBoxGuide`）是「给这三首歌配一段简短的旋律描述」——
仍是文本对话路径，没有音频出口。音频生成的真实接口在登录态之后（App 端独立
服务），无登录态不可触达。

### 16.4 落地：`wenxin:lyrics` + apply_frame 的增量流修复

- `SseOutcome.text` 原语义是「**覆盖**」（图片流里 markdown-yiyan 只有一帧
  「已完成 xxx」完整文案）。歌词流是**逐字增量**（每帧 `data.value` 一两个字），
  覆盖语义把 493 帧拼成 `"\n\n"` —— 这正是 §15「零文」的真相。
- 修复：`apply_frame` 按 `data.typing` 字段二分 —— 有 `typing` = 打字机增量流
  ⇒ **拼接**；无 = 单帧完整文案 ⇒ 保持覆盖。两种形态不会混在同一个流里。
- 新字段 `Capability.text_output`：成功判据是 `out.text` 非空（不是出图）；
  装配时 `data[] = [{"type": "text", "text": ...}]`，不下图片。
- 能力面 20→21 / 可用 16→17（tests/CI/docs 三处同步）。
- 端到端（8701 本地实例）：HTTP 200 / 499 帧 / 9.9s / data[0].text 703 字符完整歌词。

### 16.5 对能力面的最终结论

- **音频生成（真音乐）**：不可做，维持 §15 判定 —— 且现在有了**后端硬证据**
  （`scene=create_limit` 登录卡），不再只是推测。
- **歌词生成**：可做且已做（`wenxin:lyrics`）—— 文心音乐工作台把「AI写歌词」
  作为匿名对话的一部分开放，本服务借同一形状吸收。
- 前端墙不是墙：任何「前端弹登录框」的功能都值得用直打后端的方式复核一次 ——
  后端判定可能比前端宽松（歌词）也可能相同（音频），只有实测才能定论。


### 16.6 第二轮对抗：sa 映射破译与类型级判定（同日续测）

**sa 映射全部破译**（common.js @598680 的 inputInfo 组装 + chat-main 的 updateSa）：

```js
y = {ai_chat:'aidialog', ai_ppt:'ppt', office_create:'office',
     photo_editing:'pic', ai_read:'read', ai_translate:'translate'};
capability.enabled && Object.assign(y, {ai_music_create:'music', ai_numerology:'yunshi',
                                         deep_decision:'deepdecision', ...});
sa = 'functab_' + y[panelType];        // 音乐 = functab_music
// 另一条路径（launchSearch）：sa = 'bkb_functab_' + 短名
```

注意 `capability.enabled` 开关：匿名态若为 false，y 表里没有 music，sa 会 fallback 到
`home_wenxin` —— 但这是前端行为，与后端判定无关（见下）。

**类型级判定的对照实验**（决定性）：

| 变体 | 形状 | 结果 |
|---|---|---|
| G1 | 裸 bkb + `interaction_type=8`（photo_editing）+ 柴犬插画 query | **37 帧正常**：`imageScroll` 组件 + 文本，无 login-guide |
| F1 | `sa=bkb_functab_music` + `interaction_type=13` | 8 帧 login-guide |
| F2 | `sa=functab_music` + `interaction_type=13`（enter_type=unknown） | 8 帧 login-guide |
| H1 | 裸 bkb + `it=13` + `mcpInfo{type:workspaceMusic}` | 8 帧 login-guide |

⇒ **后端拦截的是 `interaction_type=13` 本身**，与 sa / mcpInfo / enter_type 全部无关。
it=8 匿名可过、it=13 一律拒 ⇒ 这是**类型级白名单**，不是全局登录墙，
也不是「配额用尽」（换新身份 todayRound=2 仍拒，见 16.2）。

**UI 侧也是面板级判定**：音乐面板里「AI写歌词 / 自定义歌词」两种生成方式都试过提交
—— 均弹登录框、零请求。移动端 UA（iPhone 模拟）同样弹（同一份 SPA、同一处判定）。

**技能体系排除**：`GET /aichat/api/skills/panel` 匿名可取（83KB，13 个标签）；
「多媒体创作」13 个技能中**没有音乐**（有视频/海报/证件照）。⇒ 音乐是**独立内置工具**，
不是「技能授权不足」的问题，带 skillId 也解决不了。

**老接口排除**：`image.baidu.com/aigc/pccreate` 测 type=13/15/11 —— 13 报网络异常，
15/11 正常建任务但都是**图片任务**（pcEditTaskid），无音乐通路。

**公开资料**：GitHub 的 baiduchat2api 项目（百度 chat 逆向）只做对话模型，
不涉及音乐；没有其他人公开过文心音乐接口。

### 16.7 最终结论（第二轮后）

音频生成在匿名态**没有任何通路**。已穷尽的维度：

- 前端：填词/做同款/AI写歌词模式 × 桌面/移动 UA —— 全部弹登录、零请求
- 后端：4 种 sa × 4 种 mcpInfo 组合 × it=13 —— 全部 login-guide
- 技能体系：音乐不是技能，无 skillId 可用
- 老接口：无音乐类型
- 公开资料：无人公开过

**要真音乐，唯一的路是登录态 cookie**（BAIDUSS 等），代价：账号绑定、cookie 会过期、
风控落在账号上 —— 这是本服务「匿名吸收」原则的边界。


---

## 17. 语音：TTS / ASR 两条独立链路（2026-11-17，均匿名可用）

两者都**不走** `/aichat/api/conversation`——是**独立传输层**，与图片族互不影响。

### 17.1 TTS（`/aichat/api/tts`）

**发现路径**：从 `common.js` 的答案朗读组件反编译 → 抓站点「语音播报」按钮的真实请求。

**请求形状**（实测抓包 + 变体验证）：

```
POST https://chat.baidu.com/aichat/api/tts?from=csaitab&appId=&agentSearchId=
     &rate=<比特率>&mid=[&per=<发音人>]&tk=<tk>&
Content-Type: application/json
body: {"text": "<要合成的文本>"}
```

- **`tk` 与 chat_token 完全同构**：`base64("token|MD5(text)|ts|lid")-lid-3`。
  唯一差别是 md5 的输入是**待合成文本**（不是 query）。抓包 base64 解码逐字段确认。
- **`text` 不进 URL**（前端 `forEach` 显式跳过 `text` 键），走 POST body —— 这点不看
  前端代码只看抓包会漏（URL 里没有 text 却能合成）。
- `from` 必须是 `csaitab`（写 `pc` 等回 `{"status":-1,"message":"invalid_from"}`）。
- `rate` 是**比特率**不是语速：32/64/128 kbps，实测**时长不变**、字节数严格成比例。
- `per` 是发音人，**可透传**：不传由服务端给 4100；0/1/3/4 实测生效（音频 URL 的
  per 跟随、音色不同）。

**响应**：`{"status":0,"data":{"contentList":["https://tts.baidu.com/text2audio?...&auth=..."]}}`
—— 按标点分句，每段带 `auth` 签名的 mp3（16kHz mono，`per`/`rate` 在 URL 里）。

**落地**：`wenxin:tts` → `POST /v1/audio/speech`；服务端逐段代取后**字节合并**
（MPEG ADTS 自同步帧流，直接 concat 可播，不需要 ffmpeg）。

### 17.2 ASR（`wss://vse.baidu.com/ws_api`）

**发现路径**：`common.js` 的 `VoiceInput` 组件（`getUserMedia` + `AudioContext`）→
反编译出 WS 协议，**不需要登录**即可连。

**协议**（逐字对齐）：

```
1) 连接：wss://vse.baidu.com/ws_api?sn=<uuid>
2) START（文本帧）：
   {"type":"START","data":{"user":"baidu_pc","dev_key":"com.baidu.searchbox.fangyan",
     "dev_pid":8068,"cuid":"...","sample":16000,"format":"pcm","type":1,
     "role_num":1,"vad_type":1,"vad_mode":0,"channels":1,
     "need_session_finish":true,"punc":true,"start_timestamp":<ms>}}
3) 音频：裸 int16 PCM 二进制帧（16k mono s16le），**全速推流也可**（不必实时）
4) 结束：{"type":"FINISH"}
5) 响应：{"type","err_no","err_msg","result"}；
   MID_TEXT = 中间结果（**累积全文**，不是增量）；FIN_TEXT = 最终结果（带标点）；
   HEARTBEAT 忽略；err_no=-3005 = 会话结束（忽略）
```

**实测**：edge-tts 合成的 3.4s 语音，全速推流（无 sleep），`FIN_TEXT` 逐字正确
（"今天天气不错，晚上一起吃饭吧。"）。**无 cookie、无风控触发**。

**落地**：`wenxin:asr` → `POST /v1/audio/transcriptions`（OpenAI multipart 同形）。

**音频格式边界**：容器是 `python:3.13-slim`、**没有 ffmpeg**，依赖面刻意最小 ⇒
只接 **wav**（标准库 `wave` 解析 + 线性插值重采样到 16k mono，8/16/24/32bit 全支持）
与**裸 16k pcm**；mp3/m4a 明确 400（提示调用方先转），不引解码器。

### 17.3 与图片族的对照

| 维度 | 图片（conversation） | TTS | ASR |
|---|---|---|---|
| 传输 | POST SSE | POST JSON | WebSocket |
| 匿名 | ✅（创作类 it 白名单例外） | ✅ | ✅ 连 cookie 都不要 |
| 风控 | 每身份累计预算 ≈45–55 发 | 未见（QPS 未知） | 未见 |
| 产出 | 图 URL（需代取） | 分句 mp3 URL（需代取） | 文本 |
