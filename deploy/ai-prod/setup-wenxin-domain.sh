#!/usr/bin/env bash
# 把 wenxin.1task.cn 接到已在跑的 baidu-service 实例上（同一容器 127.0.0.1:39014）。
#
# 前置（缺一不可）：
#   1. DNS：wenxin.1task.cn A 210.121.44.245 已生效（dig +short 能看到）
#   2. :80 vhost 已存在（wenxin.1task.cn.conf，ACME HTTP-01 入口）—— 本脚本会检查
#   3. 服务侧 .env 的 BAIDU_API_KEYS 含 sk-wenxin —— 本脚本会检查
#
# 幂等：证书已存在则跳过签发；vhost 覆盖写；最后 reload + 公网自检。
# 用法：bash setup-wenxin-domain.sh [--dry-run]
set -euo pipefail

DOMAIN="wenxin.1task.cn"
IP="210.121.44.245"
WEBROOT="/www/wwwroot/acme"
VHOST_DIR="/www/server/panel/vhost/nginx"
CERT_DIR="/www/server/panel/vhost/cert/$DOMAIN"
UPSTREAM="127.0.0.1:39014"
KEY="sk-wenxin"
DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
fail() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

say "1/6 前置检查"
resolved=$(dig +short "$DOMAIN" A | head -1 || true)
[[ "$resolved" == "$IP" ]] || fail "DNS 未就绪：$DOMAIN → '${resolved:-空}'（期望 $IP）"
[[ -f "$VHOST_DIR/$DOMAIN.conf" ]] || fail "缺少 :80 vhost：$VHOST_DIR/$DOMAIN.conf"
grep -q "$KEY" /opt/baidu/.env || fail "/opt/baidu/.env 的 BAIDU_API_KEYS 不含 $KEY"
echo "✓ DNS / vhost / key 都就绪"

say "2/6 申请证书（acme.sh，HTTP-01）"
[[ $DRY -eq 1 ]] && { echo "[dry-run] 将 issue（若缺失）+ install-cert"; exit 0; }
mkdir -p "$WEBROOT/.well-known/acme-challenge" "$CERT_DIR"
if [[ -f "$CERT_DIR/fullchain.pem" ]]; then
  echo "证书已就位，跳过签发（续期由 acme.sh cron 负责）"
else
  /root/.acme.sh/acme.sh --issue -d "$DOMAIN" -w "$WEBROOT" --keylength ec-256 --force
fi
# install 无论新签与否都执行（幂等；也会把 --reloadcmd 注册进续期钩子）
/root/.acme.sh/acme.sh --install-cert -d "$DOMAIN" --ecc \
  --key-file       "$CERT_DIR/privkey.pem" \
  --fullchain-file "$CERT_DIR/fullchain.pem" \
  --reloadcmd      "nginx -s reload"

say "3/6 写 443 vhost"
[[ $DRY -eq 1 ]] && { echo "[dry-run] 将写 $VHOST_DIR/$DOMAIN-ssl.conf"; exit 0; }
cat > "$VHOST_DIR/$DOMAIN-ssl.conf" <<CONF
# wenxin-service HTTPS 入口（与 baidu.1task.cn 同一服务实例；key = $KEY）
# 证书：acme.sh（Let's Encrypt），HTTP-01，自动续期并 reload nginx。
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name $DOMAIN;

    ssl_certificate     $CERT_DIR/fullchain.pem;
    ssl_certificate_key $CERT_DIR/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 10m;

    access_log /www/wwwlogs/$DOMAIN.log;
    error_log  /www/wwwlogs/$DOMAIN.error.log;

    auth_basic off;   # 鉴权在服务侧 BAIDU_API_KEYS

    # 运维面挡在入口层（面板只能走 SSH 隧道）
    location ^~ /panel { return 404; }
    location = /stats { return 404; }

    location / { include $VHOST_DIR/baidu-proxy.inc; }
}
CONF

say "4/6 nginx 校验 + reload"
nginx -t
nginx -s reload

say "5/6 证书信息"
openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -subject -dates | sed 's/^/  /'

say "6/6 公网自检"
sleep 2
hc=$(curl -s --noproxy '*' --max-time 15 "https://$DOMAIN/healthz")
echo "  /healthz: $hc"
# 用**需鉴权**的 /capabilities 验（/v1/models 按设计免鉴权，测它永远是 200）
echo "  无 key   : $(curl -s -o /dev/null -w '%{http_code}' --noproxy '*' --max-time 15 "https://$DOMAIN/capabilities") （期望 401）"
echo "  错误 key : $(curl -s -o /dev/null -w '%{http_code}' --noproxy '*' --max-time 15 "https://$DOMAIN/capabilities" -H 'Authorization: Bearer wrong') （期望 401）"
echo "  $KEY: $(curl -s -o /dev/null -w '%{http_code}' --noproxy '*' --max-time 15 "https://$DOMAIN/v1/models" -H "Authorization: Bearer $KEY") （期望 200）"
echo "  /panel   : $(curl -s -o /dev/null -w '%{http_code}' --noproxy '*' --max-time 15 "https://$DOMAIN/panel") （期望 404）"
echo
echo "✓ 完成：https://$DOMAIN （与 baidu.1task.cn 同实例，key=sk-wenxin）"
