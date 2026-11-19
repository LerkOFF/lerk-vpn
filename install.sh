#!/usr/bin/env bash
# Установка веб-панели lerk-vpn одной командой (Debian/Ubuntu, от root):
#   curl -fsSL https://raw.githubusercontent.com/LerkOFF/lerk-vpn/main/install.sh | bash -s -- --domain vpn.example.com
# Без --domain панель слушает только 127.0.0.1:8780 (доступ через ssh -L).
set -euo pipefail

REPO="https://github.com/LerkOFF/lerk-vpn.git"
BRANCH="main"
BASE="/opt/lerk-vpn"
USER_NAME="lerkvpn"
PORT="8780"
DOMAIN=""
LOGIN="admin"
NGINX=1
TLS=1
EMAIL=""

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --login) LOGIN="$2"; shift 2 ;;
    --email) EMAIL="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --dir) BASE="$2"; shift 2 ;;
    --no-nginx) NGINX=0; shift ;;
    --no-tls) TLS=0; shift ;;
    -h|--help) sed -n 2,5p "$0"; exit 0 ;;
    *) echo "неизвестный параметр: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
[ "$(id -u)" = 0 ] || { echo "запустите от root" >&2; exit 1; }
command -v apt-get >/dev/null || { echo "нужен Debian или Ubuntu (apt-get)" >&2; exit 1; }

say "пакеты"
export DEBIAN_FRONTEND=noninteractive
PKGS="python3 git curl openssh-client openssl ca-certificates"
[ -n "$DOMAIN" ] && [ "$NGINX" = 1 ] && PKGS="$PKGS nginx"
[ -n "$DOMAIN" ] && [ "$NGINX" = 1 ] && [ "$TLS" = 1 ] && PKGS="$PKGS certbot"
apt-get update -qq
apt-get install -y -qq $PKGS >/dev/null
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "нужен Python 3.11 или новее" >&2; exit 1; }

say "пользователь и папки"
id "$USER_NAME" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$BASE/home" --shell /usr/sbin/nologin "$USER_NAME"
mkdir -p "$BASE/data"
chown -R "$USER_NAME": "$BASE/home" "$BASE/data"
chmod 700 "$BASE/data"

say "код"
if [ -d "$BASE/app/.git" ]; then
  git -C "$BASE/app" fetch -q origin "$BRANCH"
  git -C "$BASE/app" merge -q --ff-only "origin/$BRANCH" || true
else
  git clone -q --branch "$BRANCH" "$REPO" "$BASE/app"
fi
chown -R "$USER_NAME": "$BASE/app"

ENV_FILE="/etc/lerk-vpn.env"
PASS_FILE="/root/lerk-vpn-admin.txt"
NEW_PASS=""
if [ ! -f "$ENV_FILE" ] || ! grep -q '^LERK_VPN_ADMIN_HASH=.' "$ENV_FILE"; then
  say "пароль администратора"
  NEW_PASS="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)"
  HASH="$(PASSWORD="$NEW_PASS" PYTHONPATH="$BASE/app/src" python3 -c 'import os; from lerk_vpn.web.auth import hash_password; print(hash_password(os.environ["PASSWORD"]))')"
  umask 077
  {
    echo "LERK_VPN_ADMIN_LOGIN=$LOGIN"
    echo "LERK_VPN_ADMIN_HASH=$HASH"
    echo "LERK_VPN_HOST=127.0.0.1"
    echo "LERK_VPN_PORT=$PORT"
    echo "LERK_VPN_SECURE=$([ -n "$DOMAIN" ] && echo 1 || echo 0)"
  } > "$ENV_FILE"
  printf 'логин: %s\nпароль: %s\n' "$LOGIN" "$NEW_PASS" > "$PASS_FILE"
  chmod 600 "$ENV_FILE" "$PASS_FILE"
  chown root:"$USER_NAME" "$ENV_FILE"; chmod 640 "$ENV_FILE"
fi

say "служба"
cat > /etc/systemd/system/lerk-vpn.service <<UNIT
[Unit]
Description=lerk-vpn web panel
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
Group=$USER_NAME
EnvironmentFile=$ENV_FILE
Environment=PYTHONPATH=$BASE/app/src
Environment=HOME=$BASE/home
WorkingDirectory=$BASE/app
ExecStart=/usr/bin/python3 -m lerk_vpn -C $BASE/data web run
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now lerk-vpn >/dev/null
systemctl restart lerk-vpn
for _ in 1 2 3 4 5 6 7 8 9 10; do curl -fs "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break; sleep 1; done
curl -fs "http://127.0.0.1:$PORT/health" >/dev/null || { echo "панель не отвечает, смотрите: journalctl -u lerk-vpn" >&2; exit 1; }

URL="http://127.0.0.1:$PORT (через ssh -L $PORT:127.0.0.1:$PORT)"
if [ -n "$DOMAIN" ] && [ "$NGINX" = 1 ]; then
  say "nginx для $DOMAIN"
  mkdir -p /var/www/acme
  VHOST="/etc/nginx/sites-available/lerk-vpn.conf"
  proxy_block() { cat <<PROXY
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
PROXY
  }
  CERT="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
  write_http() { cat > "$VHOST" <<CONF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/acme; }
$(proxy_block)
}
CONF
  }
  write_https() { cat > "$VHOST" <<CONF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/acme; }
    location / { return 301 https://\$host\$request_uri; }
}
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name $DOMAIN;
    ssl_certificate $CERT;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    add_header Strict-Transport-Security "max-age=31536000" always;
    client_max_body_size 1m;
$(proxy_block)
}
CONF
  }
  if [ -f "$CERT" ]; then write_https; else write_http; fi
  ln -sf "$VHOST" /etc/nginx/sites-enabled/lerk-vpn.conf
  nginx -t >/dev/null && systemctl reload nginx
  URL="http://$DOMAIN"
  if [ "$TLS" = 1 ] && [ ! -f "$CERT" ]; then
    MYIP="$(curl -fs4 -m 8 https://api.ipify.org || true)"
    DNSIP="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}' || true)"
    if [ -n "$MYIP" ] && [ "$MYIP" = "$DNSIP" ]; then
      say "сертификат Let's Encrypt"
      CERTBOT_ARGS=(certonly --webroot -w /var/www/acme -d "$DOMAIN" --non-interactive --agree-tos)
      if [ -n "$EMAIL" ]; then CERTBOT_ARGS+=(-m "$EMAIL"); else CERTBOT_ARGS+=(--register-unsafely-without-email); fi
      if certbot "${CERTBOT_ARGS[@]}" >/dev/null 2>&1; then write_https; nginx -t >/dev/null && systemctl reload nginx; URL="https://$DOMAIN"; else echo "сертификат не выпущен, повторите: bash install.sh --domain $DOMAIN"; fi
    else
      echo "DNS $DOMAIN пока смотрит на ${DNSIP:-пусто}, а сервер ${MYIP:-неизвестен}: укажите A-запись и запустите установку ещё раз, сертификат выпустится сам."
    fi
  elif [ -f "$CERT" ]; then
    URL="https://$DOMAIN"
  fi
fi

say "готово"
echo "Панель: $URL"
if [ -n "$NEW_PASS" ]; then
  echo "Логин: $LOGIN"
  echo "Пароль: $NEW_PASS   (сохранён в $PASS_FILE, показан один раз)"
else
  echo "Пароль не менялся (см. $PASS_FILE). Сменить: lerk-vpn web set-password --env-file $ENV_FILE"
fi
echo "Публичный SSH-ключ панели (его кладёт на добавляемые серверы сама панель):"
sudo -u "$USER_NAME" env HOME="$BASE/home" PYTHONPATH="$BASE/app/src" python3 -c 'from lerk_vpn.web.app import ensure_ssh_key; print(ensure_ssh_key().read_text().strip())' 2>/dev/null || true
