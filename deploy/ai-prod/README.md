# baidu-service · ai-prod 部署

- **节点**：ai-prod `210.121.44.245`（首尔，裸机）
- **目录**：`/opt/baidu`（沿用旧目录/容器名；**对外域名 = `wenxin.1task.cn`**）
- **镜像**：`ghcr.io/rsfree/wenxin:<tag>`（**public，节点直接 pull**）
- **端口**：`127.0.0.1:39014 -> 容器 8700`（**只绑回环**）
- **入口**：`https://wenxin.1task.cn`（nginx + ACME 证书）。
  ⚠️ 旧域名 `baidu.1task.cn` 已于 **2026-11-18 停用**（vhost 移入 `/root/baidu-domain-retired-20261118/`、
  DNS A 记录已删）；如需回滚见文末「回滚」。

## 升级步骤（当前线上 0.0.20）

```bash
# 本机：先发版，拿到新 tag
git push origin main          # CI 发版 → ghcr.io/rsfree/wenxin:<tag>

# 节点：备份 → 改 tag → pull → 重建
ssh -i ~/.ssh/id_ed25519_auto_macstudio root@210.121.44.245
cd /opt/baidu
cp docker-compose.yml docker-compose.yml.bak-$(date +%Y%m%d)
cp .env .env.bak-$(date +%Y%m%d)
sed -i 's/^BAIDU_IMAGE_TAG=.*/BAIDU_IMAGE_TAG=<新版本>/' .env
docker compose pull && docker compose up -d
curl -s --noproxy '*' http://127.0.0.1:39014/healthz   # 期望 version == <新版本>
```

`.env` 里是**真实 cookie**（`BAIDU_COOKIE`）——升级时只改 `BAIDU_IMAGE_TAG`，别整体覆盖 `.env`。

## 第二入口：wenxin.1task.cn

同一服务实例（同一容器 `127.0.0.1:39014`）的第二个域名，**key = `sk-wenxin`**
（约定：`sk-` + 二级域名）。两个 key 都在 `.env` 的 `BAIDU_API_KEYS` 里（逗号分隔），
从任一域名进来都认。

**部署脚本**：`bash /opt/baidu/setup-wenxin-domain.sh [--dry-run]`（幂等）
1. 前置检查：DNS → 本机、:80 vhost 存在、`.env` 含 `sk-wenxin`
2. acme.sh 签发（HTTP-01，webroot `/www/wwwroot/acme`）
3. 写 `wenxin.1task.cn-ssl.conf` → nginx -t → reload
4. 公网自检（无 key 401 / key 200 / panel 404）

**DNS**：`wenxin.1task.cn A 210.121.44.245`（阿里云 hichina，无泛解析）——
2026-11-18 已通过 jump 上的 `jdns add 1task.cn wenxin A 210.121.44.245` 添加
（RecordId 2122862525552353280，ttl 600）。

**已上线验证（2026-11-18）**：证书 LE ECC（至 2027-02-16，acme.sh cron 续期）；
`/healthz` 0.0.19；`/capabilities` 无 key/错 key → 401、`sk-wenxin`/`sk-baidu` → 200；
`/panel` `/stats` → 404；`/v1/models` 19 项；新域名 TTS 200 / 2.4s 音频。

## 管理面板只能走 SSH 隧道

`/panel` 与 `/stats` 在**入口层**返回 404（`wenxin.1task.cn-ssl.conf` 里两条 `location`）。原因：
本部署 `BAIDU_API_KEYS` **为空**（服务侧不鉴权），面板若公开，任何人都能试跑、翻日志、
甚至「铸造新身份」改写线上 cookie。

```bash
ssh -N -L 18700:127.0.0.1:39014 -i ~/.ssh/id_ed25519_auto_macstudio root@210.121.44.245
open http://127.0.0.1:18700/panel
```

## 鉴权（2026-11-17 起）

- **Key 命名约定：`sk-` + 二级域名** —— 本站点 `wenxin.1task.cn` ⇒ **`sk-wenxin`**
  （旧 `sk-baidu` 仍保留在 `.env` 里，属宽限期；确认无调用方后可移除）。
  值只写在 `.env` 的 `BAIDU_API_KEYS`，**不入库、不进文档**。
- 调用方须带 `Authorization: Bearer sk-baidu`；`/v1/models` 与 `/healthz` 按设计免鉴权
  （发现端点，兄弟服务一致）。
- 改配置后 `docker compose up -d --force-recreate`（`env_file` 变更不会自动重建容器）。

已验证（公网）：无 key → **401**；错 key → **401**；`sk-baidu` → **200**；
`/v1/models` → **200**；`/healthz` → `0.0.18`。

## 回滚

```bash
cd /opt/baidu
cp docker-compose.yml.bak-20261116 docker-compose.yml
cp .env.bak-20261116 .env
docker compose up -d            # 回到 0.0.13（旧镜像 ghcr.io/rsfree/baidu:0.0.13）
```
旧镜像 `ghcr.io/rsfree/baidu:0.0.13` 仍在 GHCR（改名迁移前的包），可回滚。

## 全形态实测（2026-11-19，0.0.20）

部署后对 `https://wenxin.1task.cn` 实跑，**图片 9 发 + TTS 1 发，零风控命中**。
契约面（零上游）16/16 通过：数组/对象 → `invalid_image`、`[]` → `missing_input`、
`169.254.169.254` / `127.0.0.1` / `[::1]` → `image_url_blocked`、`file://` →
`invalid_image_url`、干跑放行、`/panel` `/stats` 404。

| 形态 | 模型 | 结果 |
|---|---|---|
| data URI（默认 b64） | clarity | 4096×4096 · 23.4s |
| 落盘 url | clarity | 4096×4096 · 15.2s |
| **http(s) URL 作为输入** | clarity | 4096×4096 · 24.5s（代取 453KB，SSRF 放行公网） |
| 裸 base64 | clarity | 4096×4096 · 15.1s |
| mask（黑底白框） | erase | 1280×1280 · 9.8s |
| size=4:3 | expand | 4 图 1360×1024 · 24.9s |
| style=miyazaki | restyle | 1616×1616 · 18.4s |
| t2i（prompt） | draw | 4 图 864×1152 · 26.1s |
| legacy 兜底通路 | dewatermark | 819×819 · 13.0s |
| 音频端点 | tts | 29376B mp3 · 3.4s |

⚠️ 已知缺口：`data[].url` 是**相对路径**（`/files/xxx.jpg`），不是可直接访问的绝对地址；
拿它回灌 `image` 会 400 `invalid_input_format`。见 `docs/IMAGE-CONTRACT.md` §4。

## 变更记录

- **2026-11-19（0.0.19 → 0.0.20）**：图片契约按 image-adapter 校准。
  ① 外链代取加 SSRF 准入门（`app/upstream/baidu/netguard.py`：scheme 白名单、私网/环回/
  元数据地址、DNS 全答案；重定向改为自己跟、逐跳校验、5 跳上限）；
  ② `image` 传数组/对象改报 400 `invalid_image`（原先是误导性的 `missing_input`），
  `[]` 视为没传；③ 错误报文里的上游 URL 过 `redact_url`，不再带签名 query。
  备份：`.env.bak-20261119` / `docker-compose.yml.bak-20261119`。
- **2026-11-18（域名收敛）**：`wenxin.1task.cn` 上线并成为**唯一入口**；
  `baidu.1task.cn` 停用（vhost 移入 `/root/baidu-domain-retired-20261118/`，
  DNS `jdns delete 1task.cn baidu A` 已删 rid=2102975959858595840）。
  停用前核对访问日志：当日 18 次请求全部来自本机验证与扫描器，无第三方调用方。
- **2026-11-18（0.0.18 → 0.0.19）**：能力面 15 → 23 项（可用 19 项）。新增
  `wenxin:draw`（文生图）/`wenxin:lyrics`（歌词）/`wenxin:tts`（语音合成）/
  `wenxin:asr`（语音识别，含长音频自动分段），面板加「风控」页。
  新端点 `POST /v1/audio/speech`、`POST /v1/audio/transcriptions`（同一个 `sk-baidu`）。
  依赖新增 `python-multipart`、`websockets`。
  公网实测：TTS 2.6s 音频 / ASR 文本回环一致 / lyrics 725 帧 1088 字 / draw 4 图 23s。
  同日加第二入口域名 `wenxin.1task.cn`（key `sk-wenxin`，见上节）——DNS 待加。
- **2026-11-16（0.0.13 → 0.0.18）**：仓库改名 `rsfree/baidu` → `rsfree/wenxin`，镜像随之改到
  `ghcr.io/rsfree/wenxin`（包已 public，不再需要节点源码构建）；新增管理面板；
  入口层加 `/panel` `/stats` 拦截；`BAIDU_LEGACY=fallback` 生效。

## 已验证（2026-11-16）

| 检查 | 结果 |
|---|---|
| 容器 | `ghcr.io/rsfree/wenxin:0.0.18` · healthy |
| `/healthz`（隧道内） | `{"status":"ok","version":"0.0.18"}` |
| `/healthz`（公网） | 200 |
| `/v1/models` | 200，**15 个模型** |
| `/panel` `/stats`（公网） | **404**（入口层拦截） |
| `/panel`（隧道） | 200 |
| 干跑 | 200，`legacy.mode=fallback`，零上游 |
| **真实调用** | 主链 `conversation`，1616×1616，20.2s，无告警 |
