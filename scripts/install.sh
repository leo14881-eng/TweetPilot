#!/usr/bin/env bash
# TweetPilot 服务器安装 / 更新（Ubuntu，root 运行，可重复运行）。
# 配置由电脑上的同步任务上传（scripts/windows/sync.ps1），这里不需要手动改任何东西：
#   /opt/tweetpilot/secret/tweetpilot.env    全部设置和 Telegram 机器人
#   /opt/tweetpilot/secret/x-auth-token.txt  X 小号凭证（也可以直接写在 tweetpilot.env 里）
# 不输出任何凭证。
# 用法：bash install.sh          安装或更新并启动（配置变了会自动重启）
#       bash install.sh --auto   定时自动更新代码（cron 调用）
#       bash install.sh --status 查看运行状态和最近日志
set -euo pipefail

APP_DIR=/opt/tweetpilot
REPO_URL=https://github.com/leo14881-eng/TweetPilot.git
SECRET="$APP_DIR/secret"
CONF="$SECRET/tweetpilot.env"
TOKEN_FILE="$SECRET/x-auth-token.txt"
CHAT_FILE="$SECRET/telegram-chat-id.txt"
MODE="${1:-}"

[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行"; exit 1; }

say() { [ "$MODE" = "--auto" ] || echo "$@"; }

conf_value() {  # 读 tweetpilot.env 里一个键的值（不输出）
    [ -f "$CONF" ] || return 0
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$CONF" | tail -1 | tr -d '\r' | sed 's/[[:space:]]*$//; s/^"\(.*\)"$/\1/'
}

if [ "$MODE" = "--status" ]; then
    cd "$APP_DIR" 2>/dev/null || { echo "TweetPilot 还没有安装"; exit 0; }
    echo "版本：$(git rev-parse --short HEAD 2>/dev/null || echo 未知)"
    echo "配置文件：$([ -s "$CONF" ] && echo 已上传 || echo 未上传)"
    echo "监听账号：$(conf_value ACCOUNTS)"
    df -h / | tail -1
    docker compose ps
    echo "---- 读取测试（每个账号最近的推文数）----"
    docker compose exec -T monitor python - <<'PY' 2>&1 | grep -v -i -E "auth_token|bot[0-9]+:" || true
import monitor as m
for a in m.ACCOUNTS:
    try:
        items = m.parse_feed(m.http_get(m.feed_url(a), timeout=60))
        print(f"{a}: 读到 {len(items)} 条" + (f"，最新 {m.format_pub(items[0]['pub'])} {items[0]['link']}" if items else ""))
    except Exception as e:
        body = getattr(e, "read", lambda: b"")()[:300].decode("utf-8", "replace")
        print(f"{a}: 失败 {e} {body}")
PY
    echo "---- 监听日志 ----"
    docker compose logs --tail 30 monitor 2>&1 | grep -v -i -E "auth_token|bot[0-9]+:" || true
    echo "---- RSSHub 日志 ----"
    docker compose logs --tail 20 rsshub 2>&1 | grep -v -i -E "auth_token|bot[0-9]+:" || true
    exit 0
fi

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
    say "安装 Docker…"
    curl -fsSL https://get.docker.com | sh >/dev/null
    systemctl enable --now docker
fi

OLD_HEAD=$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || echo none)
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch -q --depth 1 origin main
    git -C "$APP_DIR" reset -q --hard origin/main
else
    mkdir -p "$APP_DIR"
    rm -rf "$APP_DIR.tmp"
    git clone -q --depth 1 "$REPO_URL" "$APP_DIR.tmp"
    cp -a "$APP_DIR.tmp/." "$APP_DIR/" && rm -rf "$APP_DIR.tmp"
fi
mkdir -p "$SECRET" "$APP_DIR/data"
chmod 700 "$SECRET"
say "代码已更新到 $(git -C "$APP_DIR" rev-parse --short HEAD)"

if [ ! -s "$CONF" ]; then
    say "暂不启动：还没有收到配置文件（电脑上的同步任务会自动上传）"
    exit 0
fi

X_TOKEN=$(conf_value TWITTER_AUTH_TOKEN)
[ -n "$X_TOKEN" ] || { [ -s "$TOKEN_FILE" ] && X_TOKEN=$(tr -d '\r\n ' < "$TOKEN_FILE"); } || true
BOT=$(conf_value TELEGRAM_BOT_TOKEN)
CHAT=$(conf_value TELEGRAM_CHAT_ID)

# 没填聊天 ID 时，从机器人最近收到的消息里自动取（需要先给机器人发过一条消息）
if [ -z "$CHAT" ] && [ -s "$CHAT_FILE" ]; then CHAT=$(tr -d '\r\n ' < "$CHAT_FILE"); fi
if [ -z "$CHAT" ] && [ -n "$BOT" ]; then
    CHAT=$(curl -fsS -m 20 "https://api.telegram.org/bot${BOT}/getUpdates" 2>/dev/null | python3 -c '
import json,sys
try: r=json.load(sys.stdin).get("result",[])
except Exception: r=[]
ids=[str((u.get("message") or u.get("channel_post") or {}).get("chat",{}).get("id","")) for u in r]
ids=[i for i in ids if i]
print(ids[-1] if ids else "")' 2>/dev/null || true)
    if [ -n "$CHAT" ]; then (umask 077; printf '%s' "$CHAT" > "$CHAT_FILE"); say "已自动获取 Telegram 聊天 ID"; fi
fi

missing=""
[ -n "$X_TOKEN" ] || missing="$missing X 小号凭证"
[ -n "$BOT" ] || missing="$missing Telegram 机器人 token"
[ -n "$CHAT" ] || missing="$missing Telegram 聊天 ID（先在 Telegram 里给机器人发一条消息）"
if [ -n "$missing" ]; then
    say "暂不启动，缺少：$missing"
    exit 0
fi

NEW_ENV=$(mktemp)
chmod 600 "$NEW_ENV"
{
    grep -v -E '^[[:space:]]*(#|$)' "$CONF" | tr -d '\r' \
        | grep -v -E '^[[:space:]]*(TWITTER_AUTH_TOKEN|TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|RSSHUB_URL|DB_PATH)[[:space:]]*=' || true
    echo "TWITTER_AUTH_TOKEN=$X_TOKEN"
    echo "TELEGRAM_BOT_TOKEN=$BOT"
    echo "TELEGRAM_CHAT_ID=$CHAT"
} > "$NEW_ENV"
changed=1
cmp -s "$NEW_ENV" "$APP_DIR/.env" && changed=0
mv "$NEW_ENV" "$APP_DIR/.env"

cd "$APP_DIR"
if [ "$MODE" = "--auto" ]; then
    docker compose up -d --remove-orphans >/dev/null 2>&1
    if [ "$OLD_HEAD" != "$(git rev-parse HEAD)" ]; then
        docker compose restart monitor >/dev/null && echo "$(date '+%F %T') 已更新到 $(git rev-parse --short HEAD) 并重启"
    fi
    exit 0
fi

docker compose pull -q
# 凭证或设置变了要重建容器才会生效
if [ "$changed" = 1 ]; then docker compose up -d --force-recreate --remove-orphans; else docker compose up -d --remove-orphans; fi

cat > /etc/cron.d/tweetpilot <<CRON
*/15 * * * * root bash $APP_DIR/scripts/install.sh --auto >> $APP_DIR/data/update.log 2>&1
CRON
echo "TweetPilot 已启动（每 15 分钟自动更新代码）"
