# baidu-service · ai-prod 部署

- **节点**：ai-prod `210.121.44.245`（首尔，裸机）
- **目录**：`/opt/baidu`（沿用旧服务名；容器 `baidu`，域名 `baidu.1task.cn`）
- **镜像**：`ghcr.io/rsfree/wenxin:<tag>`（**public，节点直接 pull**）
- **端口**：`127.0.0.1:39014 -> 容器 8700`（**只绑回环**）
- **入口**：`https://baidu.1task.cn`（nginx + ACME 证书）

## 升级步骤（当前线上 0.0.18）

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

## 管理面板只能走 SSH 隧道

`/panel` 与 `/stats` 在**入口层**返回 404（`baidu.1task.cn-ssl.conf` 里两条 `location`）。原因：
本部署 `BAIDU_API_KEYS` **为空**（服务侧不鉴权），面板若公开，任何人都能试跑、翻日志、
甚至「铸造新身份」改写线上 cookie。

```bash
ssh -N -L 18700:127.0.0.1:39014 -i ~/.ssh/id_ed25519_auto_macstudio root@210.121.44.245
open http://127.0.0.1:18700/panel
```

## ⚠️ 已知安全项（决策留待业务方）

`BAIDU_API_KEYS` 为空 ⇒ **`/v1/images/generations` 对公网无鉴权**（本次升级前就如此）。
本服务用的是匿名 cookie，被刷的代价是：消耗该匿名身份的额度、可能触发上游风控。
若要收紧：在 `.env` 设 `BAIDU_API_KEYS=<key1,key2>` 后重启，并让调用方带 `Authorization: Bearer`。
**先确认没有未带 key 的调用方再改**，否则会打断现网集成。

## 回滚

```bash
cd /opt/baidu
cp docker-compose.yml.bak-20261116 docker-compose.yml
cp .env.bak-20261116 .env
docker compose up -d            # 回到 0.0.13（旧镜像 ghcr.io/rsfree/baidu:0.0.13）
```
旧镜像 `ghcr.io/rsfree/baidu:0.0.13` 仍在 GHCR（改名迁移前的包），可回滚。

## 变更记录

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
