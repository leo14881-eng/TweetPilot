#!/usr/bin/env bash
# TweetPilot 服务器安装 / 更新（Ubuntu，root 运行，可重复运行）。
#   1. 没有 Docker 就安装 Docker。
#   2. 拉取 GitHub 最新代码到 /opt/tweetpilot。
#   3. 生成 .env：Telegram 机器人复用 TrendPilot 的配置（不输出内容）；
#      X 小号凭证读 /opt/tweetpilot/secret/x-auth-token.txt（由电脑上传，不输出内容）。
#   4. 凭证齐全才启动；否则只说明缺什么。
# 用法：bash install.sh          安装或更新并启动
#       bash install.sh --auto   定时自动更新（cron 调用）
#       bash install.sh --status 查看运行状态和最近日志（不含任何凭证）
set -euo pipefail

APP_DIR=/opt/tweetpilot
REPO_URL=https://github.com/leo14881-eng/TweetPilot.git
TP_CONFIG=/opt/trendpilot/config/application-local.yml
TOKEN_FILE="$APP_DIR/secret/x-auth-token.txt"

[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行"; exit 1; }

status() {
    cd "$APP_DIR" 2>/dev/null || { echo "TweetPilot 还没有安装"; exit 0; }
    echo "版本：$(git rev-parse --short HEAD 2>/dev/null || echo 未知)"
    [ -s "$TOKEN_FILE" ] && echo "X 小号凭证：已上传" || echo "X 小号凭证：未上传"
    docker compose ps
    echo "---- 最近日志 ----"
    docker compose logs --tail 30 monitor 2>&1 | grep -v -i "token" || true
}

if [ "${1:-}" = "--status" ]; then status; exit 0; fi

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
    echo "安装 Docker…"
    curl -fsSL https://get.docker.com | sh >/dev/null
    systemctl enable --now docker
fi

OLD_HEAD=$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || echo none)
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch -q --depth 1 origin main
    git -C "$APP_DIR" reset -q --hard origin/main
else
    mkdir -p "$APP_DIR"
    git clone -q --depth 1 "$REPO_URL" "$APP_DIR.tmp"
    cp -a "$APP_DIR.tmp/." "$APP_DIR/" && rm -rf "$APP_DIR.tmp"
fi
mkdir -p "$APP_DIR/secret" "$APP_DIR/data"
chmod 700 "$APP_DIR/secret"
[ "${1:-}" = "--auto" ] || echo "代码已更新到 $(git -C "$APP_DIR" rev-parse --short HEAD)"

yml_value() {  # 从 TrendPilot 配置读一个键的值，不输出到日志
    sed -n "s/^[[:space:]]*$1:[[:space:]]*[\"']\{0,1\}\([^\"'[:space:]#]*\).*/\1/p" "$TP_CONFIG" | head -1
}

missing=""
[ -f "$TP_CONFIG" ] || missing="$missing TrendPilot 配置（Telegram 机器人）"
[ -s "$TOKEN_FILE" ] || missing="$missing X 小号凭证"

if [ -n "$missing" ]; then
    [ "${1:-}" = "--auto" ] || echo "暂不启动，缺少：$missing"
    exit 0
fi

umask 077
{
    grep -v -E '^(TWITTER_AUTH_TOKEN|TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID)=' "$APP_DIR/.env.example" | grep -v '^#' | grep -v '^$'
    echo "TWITTER_AUTH_TOKEN=$(tr -d '\r\n ' < "$TOKEN_FILE")"
    echo "TELEGRAM_BOT_TOKEN=$(yml_value bot-token)"
    echo "TELEGRAM_CHAT_ID=$(yml_value chat-id)"
} > "$APP_DIR/.env"

cd "$APP_DIR"
if [ "${1:-}" = "--auto" ]; then
    # 定时自动更新：只有代码变了才重启监听，不重复拉镜像
    docker compose up -d --remove-orphans >/dev/null 2>&1
    if [ "$OLD_HEAD" != "$(git rev-parse HEAD)" ]; then
        docker compose restart monitor >/dev/null && echo "$(date '+%F %T') 已更新到 $(git rev-parse --short HEAD) 并重启"
    fi
    exit 0
fi
docker compose pull -q
docker compose up -d --remove-orphans
docker compose restart monitor >/dev/null

# 每 15 分钟自动从 GitHub 更新
cat > /etc/cron.d/tweetpilot <<CRON
*/15 * * * * root bash $APP_DIR/scripts/install.sh --auto >> $APP_DIR/data/update.log 2>&1
CRON
echo "TweetPilot 已启动，已设置每 15 分钟自动更新"
