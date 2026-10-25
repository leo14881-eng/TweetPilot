"""TweetPilot: watch X accounts via self-hosted RSSHub, translate to Chinese, push to Telegram.

Standard library only. Configuration comes from environment variables (see config.example.env).
"""
import html
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

RSSHUB_URL = os.environ.get("RSSHUB_URL", "http://rsshub:1200").rstrip("/")
# ACCOUNTS 格式：用户名:中文名，用英文逗号分隔，例如 elonmusk:马斯克,cz_binance:赵长鹏（中文名可省略）
DISPLAY_NAMES = {}
for _part in os.environ.get("ACCOUNTS", "elonmusk:马斯克,cz_binance:赵长鹏,justinsuntron:孙宇晨").replace("，", ",").split(","):
    _user, _, _name = _part.strip().lstrip("@").partition(":")
    if _user.strip():
        DISPLAY_NAMES[_user.strip()] = _name.strip() or _user.strip()
ACCOUNTS = list(DISPLAY_NAMES)
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
EXCLUDE_REPLIES = os.environ.get("EXCLUDE_REPLIES", "1") == "1"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DB_PATH = os.environ.get("DB_PATH", "/data/state.db")
ALERT_AFTER_FAIL_MINUTES = int(os.environ.get("ALERT_AFTER_FAIL_MINUTES", "30"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

VN_TZ = timezone(timedelta(hours=7))
log = logging.getLogger("tweetpilot")


# ---------- storage ----------

def open_db(path):
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
    return db.execute("SELECT 1 FROM meta WHERE k=?", ("init:" + account,)).fetchone() is not None


def set_initialized(db, account):
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("init:" + account, "1"))
    db.commit()


# ---------- fetching ----------

def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "TweetPilot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def feed_url(account):
    params = "excludeReplies=1" if EXCLUDE_REPLIES else ""
    base = f"{RSSHUB_URL}/twitter/user/{urllib.parse.quote(account)}"
    return f"{base}/{params}" if params else base


STATUS_RE = re.compile(r"/status/(\d+)")


def to_x_link(link):
    m = STATUS_RE.search(link or "")
    if not m:
        return link
    path = urllib.parse.urlparse(link).path
    return "https://x.com" + path.split("?")[0]


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
    if re.search(r"[一-鿿]", text) and len(re.findall(r"[一-鿿]", text)) > len(text) * 0.3:
        return text  # already mostly Chinese
    q = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text})
    try:
        data = json.loads(http_get("https://translate.googleapis.com/translate_a/single?" + q, timeout=20))
        return "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()
    except Exception as e:  # translation is best-effort
        log.warning("translate failed: %s", e)
        return "（翻译暂不可用）"


# ---------- telegram ----------

def send_telegram(text):
    if DRY_RUN or not TELEGRAM_BOT_TOKEN:
        print("---- PUSH ----\n" + text + "\n--------------")
        return True
    body = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    }).encode()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read()).get("ok", False)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry = int(json.loads(e.read() or b"{}").get("parameters", {}).get("retry_after", 5))
                time.sleep(retry)
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


def build_message(account, item, zh):
    name = DISPLAY_NAMES.get(account, account)
    when = format_pub(item["pub"])
    orig = item["text"]
    if len(orig) > 1500:
        orig = orig[:1500] + "…"
    if len(zh) > 1500:
        zh = zh[:1500] + "…"
    parts = [f"🐦 <b>{html.escape(name)}</b> @{html.escape(account)}" + (f"  {when}（越南时间）" if when else "")]
    if zh and zh != orig:
        parts.append("🇨🇳 " + html.escape(zh))
    parts.append("📝 " + html.escape(orig))
    parts.append(f'🔗 <a href="{html.escape(item["link"])}">打开原帖</a>')
    return "\n\n".join(parts)


# ---------- main loop ----------

def poll_once(db, state):
    any_ok = False
    for account in ACCOUNTS:
        try:
            items = parse_feed(http_get(feed_url(account)))
        except Exception as e:
            log.warning("fetch %s failed: %s", account, e)
            continue
        any_ok = True
        if not account_initialized(db, account):
            # first run: remember current tweets silently so we don't flood the chat
            for it in items:
                mark_seen(db, it["id"], account)
            set_initialized(db, account)
            log.info("initialized %s with %d existing tweets", account, len(items))
            continue
        for it in sorted(items, key=lambda x: int(x["id"])):
            if is_seen(db, it["id"]):
                continue
            msg = build_message(account, it, translate_zh(it["text"]))
            if send_telegram(msg):
                mark_seen(db, it["id"], account)
                log.info("pushed %s %s", account, it["id"])

    now = time.time()
    if any_ok:
        if state.get("alerted"):
            send_telegram("✅ 推特监听已恢复正常。")
        state.update(last_ok=now, alerted=False)
    elif not state.get("alerted") and now - state["last_ok"] > ALERT_AFTER_FAIL_MINUTES * 60:
        send_telegram(f"⚠️ 推特监听已经 {ALERT_AFTER_FAIL_MINUTES} 分钟读不到推文，可能是 X 小号登录过期或被限制，需要更新 auth_token。")
        state["alerted"] = True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = open_db(DB_PATH)
    state = {"last_ok": time.time(), "alerted": False}
    log.info("watching %s every %ss", ACCOUNTS, POLL_SECONDS)
    names = "、".join(DISPLAY_NAMES.get(a, a) for a in ACCOUNTS)
    send_telegram(f"🐦 推特监听已启动：{names}，每 {POLL_SECONDS} 秒检查一次新推文。")
    while True:
        try:
            poll_once(db, state)
        except Exception as e:
            log.exception("poll error: %s", e)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
