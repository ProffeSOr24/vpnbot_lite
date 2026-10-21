#!/usr/bin/env bash
# VPN Bot Lite — установка и обслуживание.
#   ./vpn.sh install [BOT_TOKEN] [ADMIN_ID]   установка (Docker, настройки ядра, автообновление)
#   ./vpn.sh update                           обновить бота из git и пересобрать
#   ./vpn.sh restart | stop | status | logs
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
UNIT=vpnbot-lite-update

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mОшибка:\033[0m %s\n' "$*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "запустите от root (sudo $0 $*)"; }
env_get() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true; }
env_port() { local v; v="$(env_get "$1")"; echo "${v:-$2}"; }  # env_port VAR default
env_set() {
    if grep -qE "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
}
ask() {  # ask VAR "prompt" — читает из терминала, даже если скрипт запущен через pipe
    local value=""
    while [ -z "$value" ]; do read -r -p "$2: " value < /dev/tty; done
    env_set "$1" "$value"
}

install_docker() {
    if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then return; fi
    log "Устанавливаю Docker"
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
}

tune_kernel() {
    log "Включаю BBR, форвардинг (AmneziaWG) и увеличиваю UDP-буферы (Hysteria2)"
    cat > /etc/sysctl.d/99-vpnbot-lite.conf <<'EOF'
net.ipv4.ip_forward=1
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
net.core.rmem_max=16777216
net.core.wmem_max=16777216
net.ipv4.tcp_fastopen=3
EOF
    sysctl --system >/dev/null || true
}

open_firewall() {
    command -v ufw >/dev/null && ufw status | grep -q "Status: active" || return 0
    log "Открываю порты в ufw"
    ufw allow "$(env_port XRAY_PORT 443)/tcp"
    [ "$(env_port XHTTP_PORT 8443)" = 0 ] || ufw allow "$(env_port XHTTP_PORT 8443)/tcp"
    ufw allow "$(env_port HY2_PORT 443)/udp"
    ufw allow "$(env_port AWG_PORT 51820)/udp"
    ufw allow "$(env_port SUB_PORT 2096)/tcp"
}

version_url() {  # raw-ссылка на VERSION в GitHub, чтобы бот видел новые версии
    local remote branch prefix slug
    remote="$(git remote get-url origin 2>/dev/null || true)"
    [[ "$remote" == *github.com* ]] || return 0
    branch="$(git rev-parse --abbrev-ref HEAD)"
    prefix="$(git rev-parse --show-prefix)"
    slug="$(echo "$remote" | sed -E 's#^(git@github.com:|https://github.com/)##; s#\.git$##')"
    echo "https://raw.githubusercontent.com/$slug/$branch/${prefix}VERSION"
}

install_update_unit() {
    log "Включаю обновление бота по кнопке в Telegram"
    cat > /etc/systemd/system/$UNIT.path <<EOF
[Unit]
Description=VPN Bot Lite: update requested from Telegram

[Path]
PathExists=$DIR/data/update.request

[Install]
WantedBy=multi-user.target
EOF
    cat > /etc/systemd/system/$UNIT.service <<EOF
[Unit]
Description=VPN Bot Lite: update

[Service]
Type=oneshot
ExecStart=$DIR/vpn.sh update
EOF
    systemctl daemon-reload
    systemctl enable --now $UNIT.path
}

check_ports() {
    local port
    for port in "$(env_port XRAY_PORT 443)" "$(env_port XHTTP_PORT 8443)" "$(env_port SUB_PORT 2096)"; do
        [ "$port" = 0 ] && continue
        if ss -ltnH "sport = :$port" 2>/dev/null | grep -q . && ! docker ps --format '{{.Names}}' | grep -q '^vpnbot-lite$'; then
            echo "⚠️  Порт $port/tcp уже занят другим процессом:"; ss -ltnpH "sport = :$port" || true
            echo "   Освободите его или поменяйте порт в .env"
        fi
    done
}

cmd_install() {
    need_root "$@"
    [ -f .env ] || cp .env.example .env
    [ -n "$(env_get AWG_PORT)" ] || env_set AWG_PORT 51820  # .env от версии без AmneziaWG
    [ -n "${1:-}" ] && env_set BOT_TOKEN "$1"
    [ -n "${2:-}" ] && env_set ADMIN_IDS "$2"
    [ -n "$(env_get BOT_TOKEN)" ] || ask BOT_TOKEN "Токен бота от @BotFather"
    [ -n "$(env_get ADMIN_IDS)" ] || ask ADMIN_IDS "Ваш Telegram ID (узнать: @userinfobot)"
    [ -n "$(env_get BOT_VERSION_URL)" ] || env_set BOT_VERSION_URL "$(version_url)"
    mkdir -p data
    install_docker
    tune_kernel
    open_firewall
    check_ports
    install_update_unit
    log "Собираю и запускаю"
    docker compose up -d --build
    log "Готово! Откройте бота в Telegram и нажмите /start"
}

cmd_update() {
    rm -f data/update.request
    if [ -d .git ] || git rev-parse --git-dir >/dev/null 2>&1; then
        log "Скачиваю новую версию"
        git pull --ff-only
    fi
    if [ "$(id -u)" -eq 0 ]; then  # новые версии могут требовать новых портов/настроек ядра
        [ -n "$(env_get AWG_PORT)" ] || env_set AWG_PORT 51820
        tune_kernel
        open_firewall
    fi
    log "Пересобираю"
    docker compose build --pull
    docker compose up -d
    docker image prune -f >/dev/null || true
    log "Обновлено до $(cat VERSION)"
}

case "${1:-}" in
    install) shift; cmd_install "$@" ;;
    update) cmd_update ;;
    restart) docker compose restart ;;
    stop) docker compose down ;;
    status) docker compose ps ;;
    logs) docker compose logs -f --tail=200 ;;
    *) sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
