#!/usr/bin/env bash
# TweetPilot 服务器安装 / 更新（Ubuntu，root 运行，可重复运行）。
#   1. 没有 Docker 就安装 Docker。
#   2. 拉取 GitHub 最新代码到 /opt/tweetpilot。
#   3. 生成 .env（不输出任何凭证）：
#      X 小号凭证      /opt/tweetpilot/secret/x-auth-token.txt
#      Telegram 机器人  /opt/tweetpilot/secret/telegram-bot-token.txt（TweetPilot 专用机器人）
#      Telegram 聊天 ID /opt/tweetpilot/secret/telegram-chat-id.txt（没有就自动获取：先给机器人发一条消息）
#   4. 凭证齐全才启动；否则只说明缺什么。
# 用法：bash install.sh          安装或更新并启动
#       bash install.sh --auto   定时自动更新（cron 调用）
#       bash install.sh --status 查看运行状态和最近日志（不含任何凭证）
set -euo pipefail

APP_DIR=/opt/tweetpilot
REPO_URL=https://github.com/leo14881-eng/TweetPilot.git
TOKEN_FILE="$APP_DIR/secret/x-auth-token.txt"
BOT_FILE="$APP_DIR/secret/telegram-bot-token.txt"
CHAT_FILE="$APP_DIR/secret/telegram-chat-id.txt"

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

read_secret() { tr -d '\r\n ' < "$1"; }

# 没有聊天 ID 时，从机器人最近收到的消息里自动取（需要先给机器人发过一条消息）
if [ -s "$BOT_FILE" ] && [ ! -s "$CHAT_FILE" ]; then
    curl -fsS -m 20 "https://api.telegram.org/bot$(read_secret "$BOT_FILE")/getUpdates" 2>/dev/null \
        | python3 -c 'import json,sys
r=json.load(sys.stdin).get("result",[])
ids=[str((u.get("message") or u.get("channel_post") or {}).get("chat",{}).get("id","")) for u in r]
ids=[i for i in ids if i]
print(ids[-1] if ids else "")' > "$CHAT_FILE.tmp" 2>/dev/null || true
    if [ -s "$CHAT_FILE.tmp" ]; then mv "$CHAT_FILE.tmp" "$CHAT_FILE"; echo "已自动获取 Telegram 聊天 ID"
    else rm -f "$CHAT_FILE.tmp"; fi
fi

missing=""
[ -s "$TOKEN_FILE" ] || missing="$missing X 小号凭证"
[ -s "$BOT_FILE" ] || missing="$missing Telegram 机器人 token"
[ -s "$CHAT_FILE" ] || missing="$missing Telegram 聊天 ID（请先在 Telegram 里给新机器人发一条消息，再重新运行）"

if [ -n "$missing" ]; then
    [ "${1:-}" = "--auto" ] || echo "暂不启动，缺少：$missing"
    exit 0
fi

umask 077
{
    grep -v -E '^(TWITTER_AUTH_TOKEN|TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID)=' "$APP_DIR/.env.example" | grep -v '^#' | grep -v '^$'
    echo "TWITTER_AUTH_TOKEN=$(read_secret "$TOKEN_FILE")"
    echo "TELEGRAM_BOT_TOKEN=$(read_secret "$BOT_FILE")"
    echo "TELEGRAM_CHAT_ID=$(read_secret "$CHAT_FILE")"
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
