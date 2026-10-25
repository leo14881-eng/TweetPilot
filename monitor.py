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
INCLUDE_RETWEETS = os.environ.get("INCLUDE_RETWEETS", "0") == "1"
MAX_AGE_MINUTES = int(os.environ.get("MAX_AGE_MINUTES", "120"))
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
    params = []
    if EXCLUDE_REPLIES:
        params.append("excludeReplies=1")
    if not INCLUDE_RETWEETS:
        params.append("includeRts=false")
    base = f"{RSSHUB_URL}/twitter/user/{urllib.parse.quote(account)}"
    return f"{base}/{'&'.join(params)}" if params else base


RT_RE = re.compile(r"^\s*RT\b")


def is_retweet(item):
    return bool(RT_RE.match(item.get("title", "")) or RT_RE.match(item.get("text", "")))


def age_minutes(item):
    try:
        dt = datetime.strptime(item["pub"], "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60
    except Exception:
        return 0


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
            "title": (it.findtext("title") or "").strip(),
        })
    return items


# ---------- translation (free, no key; tried in order) ----------

def _http(url, data=None, headers=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _google(text):
    body = urllib.parse.urlencode({"q": text}).encode()
    url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=zh-CN&dt=t"
    data = json.loads(_http(url, body, {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}))
    return "".join(seg[0] for seg in data[0] if seg and seg[0])


_ms_token = {"value": "", "at": 0}


def _microsoft(text):
    if time.time() - _ms_token["at"] > 8 * 60:
        _ms_token["value"] = _http("https://edge.microsoft.com/translate/auth").decode().strip()
        _ms_token["at"] = time.time()
    url = "https://api-edge.cognitive.microsofttranslator.com/translate?to=zh-Hans&api-version=3.0"
    data = json.loads(_http(url, json.dumps([{"Text": text}]).encode(),
                            {"Authorization": "Bearer " + _ms_token["value"], "Content-Type": "application/json"}))
    return data[0]["translations"][0]["text"]


def _mymemory(text):
    out = []
    for i in range(0, len(text), 450):  # this service takes ~500 chars per request
        q = urllib.parse.urlencode({"q": text[i:i + 450], "langpair": "en|zh-CN"})
        data = json.loads(_http("https://api.mymemory.translated.net/get?" + q))
        out.append(data["responseData"]["translatedText"])
    return "".join(out)


OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()


def _chatgpt(text):
    """Paid fallback, only used when every free service failed."""
    if not OPENAI_API_KEY:
        raise RuntimeError("未配置 OPENAI_API_KEY")
    body = json.dumps({
        "model": OPENAI_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "把用户发来的推文翻译成简体中文。只输出译文，保留人名、币种代码和链接原样，不要解释。"},
            {"role": "user", "content": text},
        ],
    }).encode()
    data = json.loads(_http(OPENAI_BASE_URL + "/chat/completions", body,
                            {"Authorization": "Bearer " + OPENAI_API_KEY, "Content-Type": "application/json"},
                            timeout=60))
    return data["choices"][0]["message"]["content"]


# 免费的在前，ChatGPT 最后（付费，只在前面全部失败时才用）
TRANSLATORS = [("google", _google), ("microsoft", _microsoft), ("mymemory", _mymemory), ("chatgpt", _chatgpt)]


def translate_zh(text):
    """Returns Chinese text, or "" when every free service failed (the push then shows the original only)."""
    if not text:
        return ""
    if len(re.findall(r"[\u4e00-\u9fff]", text)) > len(text) * 0.3:
        return text  # already mostly Chinese
    for name, fn in TRANSLATORS:
        try:
            zh = (fn(text) or "").strip()
            if zh and zh != text:
                return zh
        except Exception as e:
            log.warning("translate via %s failed: %s", name, e)
    return ""


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
    else:
        parts.append("🇨🇳 （翻译暂时失败，以下是原文）")
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
        state["fetches"] = state.get("fetches", 0) + 1
        if state["fetches"] % 30 == 1:
            log.info("fetched %s: %d items", account, len(items))
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
            if (not INCLUDE_RETWEETS and is_retweet(it)) or age_minutes(it) > MAX_AGE_MINUTES:
                mark_seen(db, it["id"], account)  # retweet or old tweet: skip silently
                log.info("skipped %s %s (%s)", account, it["id"], "retweet" if is_retweet(it) else "old")
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
    # 启动通知只在监听名单变化时发一次，平时重启（自动更新）不打扰
    names = "、".join(DISPLAY_NAMES.get(a, a) for a in ACCOUNTS)
    row = db.execute("SELECT v FROM meta WHERE k='notified_accounts'").fetchone()
    if not row and db.execute("SELECT 1 FROM seen LIMIT 1").fetchone():
        # 已经在运行的老安装：记下当前名单，不再重复发启动消息
        db.execute("INSERT OR REPLACE INTO meta VALUES ('notified_accounts', ?)", (names,))
        db.commit()
        row = (names,)
    if not row or row[0] != names:
        if send_telegram(f"🐦 推特监听名单：{names}。有新推文才会推送，平时不打扰。"):
            db.execute("INSERT OR REPLACE INTO meta VALUES ('notified_accounts', ?)", (names,))
            db.commit()
    while True:
        try:
            poll_once(db, state)
        except Exception as e:
            log.exception("poll error: %s", e)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
