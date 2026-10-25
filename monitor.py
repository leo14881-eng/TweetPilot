"""TweetPilot: watch X accounts via self-hosted RSSHub, translate to Chinese, push to Telegram.

Standard library only.
- Secrets (X auth token, Telegram bot token) come from the environment, set once at install.
- Everything else (accounts, replies on/off, interval) is changed from Telegram with bot
  commands and saved in /data/config.json, so the server never needs to be touched again.
- The owner chat is whoever first sends /start to the bot (or TELEGRAM_CHAT_ID if set).
"""
import html
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

RSSHUB_URL = os.environ.get("RSSHUB_URL", "http://rsshub:1200").rstrip("/")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "state.db")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
ALERT_AFTER_FAIL_MINUTES = int(os.environ.get("ALERT_AFTER_FAIL_MINUTES", "30"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

DEFAULT_CONFIG = {
    "owner_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    "accounts": {"elonmusk": "马斯克", "cz_binance": "赵长鹏", "justinsuntron": "孙宇晨"},
    "exclude_replies": True,
    "poll_seconds": 60,
    "paused": False,
}

VN_TZ = timezone(timedelta(hours=7))
log = logging.getLogger("tweetpilot")
cfg_lock = threading.Lock()


# ---------- config ----------

def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.error("config.json unreadable, using defaults: %s", e)
    return cfg


def save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


CONFIG = load_config()


def get_cfg():
    with cfg_lock:
        return json.loads(json.dumps(CONFIG))


def update_cfg(fn):
    with cfg_lock:
        fn(CONFIG)
        save_config(CONFIG)


# ---------- storage ----------

def open_db(path=DB_PATH):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, account TEXT, seen_at TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    db.commit()
    return db


def is_seen(db, tid):
    return db.execute("SELECT 1 FROM seen WHERE id=?", (tid,)).fetchone() is not None


def mark_seen(db, tid, account):
    db.execute("INSERT OR IGNORE INTO seen VALUES (?,?,?)", (tid, account, datetime.now(timezone.utc).isoformat()))
    db.commit()


def account_initialized(db, account):
    return db.execute("SELECT 1 FROM meta WHERE k=?", ("init:" + account.lower(),)).fetchone() is not None


def set_initialized(db, account):
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("init:" + account.lower(), "1"))
    db.commit()


# ---------- fetching ----------

def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "TweetPilot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def feed_url(account, exclude_replies):
    base = f"{RSSHUB_URL}/twitter/user/{urllib.parse.quote(account)}"
    return f"{base}/excludeReplies=1" if exclude_replies else base


STATUS_RE = re.compile(r"/status/(\d+)")


def to_x_link(link):
    if not STATUS_RE.search(link or ""):
        return link
    return "https://x.com" + urllib.parse.urlparse(link).path


def html_to_text(s):
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        m = STATUS_RE.search(link)
        if not m:
            continue
        items.append({
            "id": m.group(1),
            "link": to_x_link(link),
            "text": html_to_text(it.findtext("description") or it.findtext("title") or ""),
            "pub": (it.findtext("pubDate") or "").strip(),
        })
    return items


# ---------- translation (free Google endpoint, no key) ----------

def translate_zh(text):
    if not text:
        return ""
    han = len(re.findall(r"[一-鿿]", text))
    if han > len(text) * 0.3:
        return text  # already mostly Chinese
    q = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text})
    try:
        data = json.loads(http_get("https://translate.googleapis.com/translate_a/single?" + q, timeout=20))
        return "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()
    except Exception as e:  # translation is best-effort
        log.warning("translate failed: %s", e)
        return "（翻译暂不可用）"


# ---------- telegram ----------

def tg_api(method, params, timeout=20):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    body = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=timeout) as r:
        return json.loads(r.read())


def send_telegram(text, chat_id=None):
    chat_id = chat_id or get_cfg()["owner_chat_id"]
    if DRY_RUN or not TELEGRAM_BOT_TOKEN:
        print("---- PUSH ----\n" + text + "\n--------------")
        return True
    if not chat_id:
        log.info("no owner chat yet; send /start to the bot")
        return False
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "false"}
    for _ in range(3):
        try:
            return tg_api("sendMessage", params).get("ok", False)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry = json.loads(e.read() or b"{}").get("parameters", {}).get("retry_after", 5)
                time.sleep(int(retry))
                continue
            log.error("telegram http %s", e.code)
            return False
        except Exception as e:
            log.error("telegram error: %s", e)
            time.sleep(3)
    return False


def format_pub(pub):
    try:
        dt = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
        return dt.astimezone(VN_TZ).strftime("%m-%d %H:%M")
    except Exception:
        return ""


def build_message(account, name, item, zh):
    when = format_pub(item["pub"])
    orig = item["text"][:1500] + ("…" if len(item["text"]) > 1500 else "")
    zh = zh[:1500] + ("…" if len(zh) > 1500 else "")
    parts = [f"🐦 <b>{html.escape(name)}</b> @{html.escape(account)}" + (f"  {when}（越南时间）" if when else "")]
    if zh and zh != orig:
        parts.append("🇨🇳 " + html.escape(zh))
    parts.append("📝 " + html.escape(orig))
    parts.append(f'🔗 <a href="{html.escape(item["link"])}">打开原帖</a>')
    return "\n\n".join(parts)


# ---------- bot commands ----------

HELP = (
    "可用命令：\n"
    "/list 查看监听名单和设置\n"
    "/add 用户名 中文名 增加监听，例如 /add saylor 塞勒\n"
    "/remove 用户名 删除监听\n"
    "/replies on|off 是否推送回复别人的推文\n"
    "/interval 秒数 检查间隔（30–3600）\n"
    "/pause 暂停推送　/resume 恢复推送\n"
    "/status 运行状态"
)

USER_RE = re.compile(r"^@?([A-Za-z0-9_]{1,15})$")
STATS = {"started": time.time(), "last_ok": None, "pushed": 0}


def describe(cfg):
    names = "\n".join(f"• {n}  @{a}" for a, n in cfg["accounts"].items()) or "（空）"
    return (f"监听名单：\n{names}\n\n"
            f"回复推文：{'不推送' if cfg['exclude_replies'] else '推送'}\n"
            f"检查间隔：{cfg['poll_seconds']} 秒\n"
            f"状态：{'已暂停' if cfg['paused'] else '运行中'}")


def handle_command(chat_id, text):
    """Returns the reply text. Only the owner chat may change settings."""
    cfg = get_cfg()
    parts = text.strip().split()
    cmd = parts[0].split("@")[0].lower() if parts else ""
    args = parts[1:]

    if not cfg["owner_chat_id"]:
        if cmd == "/start":
            update_cfg(lambda c: c.__setitem__("owner_chat_id", str(chat_id)))
            return "✅ 已绑定，之后的推文都会推送到这里。\n\n" + HELP + "\n\n" + describe(get_cfg())
        return "请先发送 /start 完成绑定。"
    if str(chat_id) != str(cfg["owner_chat_id"]):
        return None  # ignore strangers silently

    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/list":
        return describe(cfg)
    if cmd == "/add":
        if not args or not USER_RE.match(args[0]):
            return "用法：/add 用户名 中文名，例如 /add saylor 塞勒"
        user = USER_RE.match(args[0]).group(1)
        name = " ".join(args[1:]) or user
        update_cfg(lambda c: c["accounts"].__setitem__(user, name))
        return f"✅ 已增加 {name} @{user}（只推之后的新推文）"
    if cmd == "/remove":
        if not args:
            return "用法：/remove 用户名"
        user = args[0].lstrip("@")
        key = next((a for a in cfg["accounts"] if a.lower() == user.lower()), None)
        if not key:
            return f"名单里没有 @{user}"
        update_cfg(lambda c: c["accounts"].pop(key, None))
        return f"✅ 已删除 @{key}"
    if cmd == "/replies":
        if not args or args[0].lower() not in ("on", "off"):
            return "用法：/replies on（推送回复）或 /replies off（只推主帖）"
        on = args[0].lower() == "on"
        update_cfg(lambda c: c.__setitem__("exclude_replies", not on))
        return "✅ 之后回复别人的推文也会推送" if on else "✅ 之后只推主帖"
    if cmd == "/interval":
        if not args or not args[0].isdigit() or not 30 <= int(args[0]) <= 3600:
            return "用法：/interval 秒数（30–3600），例如 /interval 60"
        update_cfg(lambda c: c.__setitem__("poll_seconds", int(args[0])))
        return f"✅ 检查间隔改为 {args[0]} 秒"
    if cmd == "/pause":
        update_cfg(lambda c: c.__setitem__("paused", True))
        return "⏸ 已暂停推送（发送 /resume 恢复）"
    if cmd == "/resume":
        update_cfg(lambda c: c.__setitem__("paused", False))
        return "▶️ 已恢复推送"
    if cmd == "/status":
        last = STATS["last_ok"]
        last_s = datetime.fromtimestamp(last, VN_TZ).strftime("%m-%d %H:%M:%S") if last else "还没有成功读取"
        up = int((time.time() - STATS["started"]) / 3600)
        return f"运行 {up} 小时，最近一次成功读取：{last_s}，已推送 {STATS['pushed']} 条\n\n" + describe(cfg)
    return HELP


def command_loop():
    offset = 0
    while True:
        try:
            r = tg_api("getUpdates", {"offset": offset, "timeout": 50, "allowed_updates": '["message"]'}, timeout=70)
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                text = msg.get("text") or ""
                if not text.startswith("/"):
                    continue
                reply = handle_command(msg["chat"]["id"], text)
                if reply:
                    send_telegram(html.escape(reply), chat_id=msg["chat"]["id"])
        except Exception as e:
            log.warning("command loop: %s", e)
            time.sleep(10)


# ---------- main loop ----------

def poll_once(db, state):
    cfg = get_cfg()
    if cfg["paused"] or not cfg["accounts"]:
        return
    any_ok = False
    for account, name in cfg["accounts"].items():
        try:
            items = parse_feed(http_get(feed_url(account, cfg["exclude_replies"])))
        except Exception as e:
            log.warning("fetch %s failed: %s", account, e)
            continue
        any_ok = True
        if not account_initialized(db, account):
            # first time we see this account: remember current tweets silently, no flood
            for it in items:
                mark_seen(db, it["id"], account)
            set_initialized(db, account)
            log.info("initialized %s with %d existing tweets", account, len(items))
            continue
        for it in sorted(items, key=lambda x: int(x["id"])):
            if is_seen(db, it["id"]):
                continue
            if send_telegram(build_message(account, name, it, translate_zh(it["text"]))):
                mark_seen(db, it["id"], account)
                STATS["pushed"] += 1
                log.info("pushed %s %s", account, it["id"])

    now = time.time()
    if any_ok:
        STATS["last_ok"] = now
        if state.get("alerted"):
            send_telegram("✅ 推特监听已恢复正常。")
        state.update(last_ok=now, alerted=False)
    elif not state.get("alerted") and now - state["last_ok"] > ALERT_AFTER_FAIL_MINUTES * 60:
        send_telegram(f"⚠️ 推特监听已经 {ALERT_AFTER_FAIL_MINUTES} 分钟读不到推文，"
                      "可能是 X 小号登录过期或被限制，需要更新 auth_token。")
        state["alerted"] = True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = open_db()
    state = {"last_ok": time.time(), "alerted": False}
    if TELEGRAM_BOT_TOKEN and not DRY_RUN:
        threading.Thread(target=command_loop, daemon=True).start()
    cfg = get_cfg()
    if cfg["owner_chat_id"]:
        send_telegram("🐦 推特监听已启动。\n\n" + html.escape(describe(cfg)) + "\n\n发送 /help 查看可用命令。")
    log.info("started; accounts=%s", list(cfg["accounts"]))
    while True:
        try:
            poll_once(db, state)
        except Exception as e:
            log.exception("poll error: %s", e)
        time.sleep(get_cfg()["poll_seconds"])


if __name__ == "__main__":
    main()
