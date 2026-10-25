"""TweetPilot news: only the news most likely to move crypto prices (Fed, key US data, macro shocks, crypto policy), picked by ChatGPT, pushed to Telegram.

Sources (all free):
- RSS feeds (Federal Reserve, BLS, CNBC economy, ...)   NEWS_RSS
- fast-headline X accounts via RSSHub (@DeItaone, ...)  NEWS_X_ACCOUNTS
Every few minutes the new items are sent in one batch to ChatGPT, which keeps only what really
matters and drops repeats of events already pushed. Once a day a 24-hour digest is pushed.
Needs OPENAI_API_KEY; without it the module stays off.
"""
import hashlib
import html
import json
import logging
import os
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import monitor as m

log = logging.getLogger("tweetpilot.news")

DEFAULT_RSS = ",".join([
    "美联储公告|https://www.federalreserve.gov/feeds/press_all.xml",
    "美联储讲话|https://www.federalreserve.gov/feeds/speeches.xml",
    "美国劳工统计局|https://www.bls.gov/feed/bls_latest.rss",
    "CNBC 经济|https://www.cnbc.com/id/20910258/device/rss/rss.html",
    "CNBC 科技|https://www.cnbc.com/id/19854910/device/rss/rss.html",
])
NEWS_ENABLED = os.environ.get("NEWS_ENABLED", "1") == "1"
NEWS_POLL_SECONDS = int(os.environ.get("NEWS_POLL_SECONDS", "180"))
NEWS_X_ACCOUNTS = [a.strip().lstrip("@") for a in os.environ.get("NEWS_X_ACCOUNTS", "DeItaone,FirstSquawk").split(",") if a.strip()]
NEWS_RSS = []
for _part in os.environ.get("NEWS_RSS", DEFAULT_RSS).split(","):
    _name, _, _url = _part.strip().partition("|")
    if _url.strip():
        NEWS_RSS.append((_name.strip() or _url.strip(), _url.strip()))
WATCH_HOURS = 2  # 快讯等待可靠来源证实的最长时间
DAILY_DIGEST_HOUR = int(os.environ.get("DAILY_DIGEST_HOUR", "14"))  # Vietnam time; -1 = off
NEWS_MODEL = os.environ.get("NEWS_MODEL", "").strip() or m.OPENAI_MODEL

EDITOR_PROMPT = """你是给一位加密货币投资者筛选新闻的编辑。他只想收到**可能明显影响比特币和加密市场价格**的最重要消息，其余一律丢弃。

只有这些才推送：
- 美联储：利率决议、会议纪要，或主席关于降息/加息/缩表的新表态（普通理事的例行讲话不推，除非明显改变市场预期）
- 关键美国数据刚公布且明显超出或低于预期：CPI、PCE、非农、失业率（写出实际值、预期值、前值，候选里有就写）
- 突发的重大关税、贸易战、制裁、政府停摆、债务危机等宏观冲击
- 美股或美债剧烈异动（纳指/标普单日超过约 2%、10 年期美债收益率大幅跳动）
- 加密行业重大事件：SEC/CFTC/白宫的重大监管或立法、比特币/以太坊等 ETF 的批准或异常大额资金流、头部交易所被盗/暂停提币/倒闭、USDT/USDC 脱锚、国家或大型机构大额买卖比特币
- 只有会牵动整个市场的科技公司事件才推（例如英伟达财报大幅超预期或暴雷），普通产品发布、个股消息不推

不推：例行新闻、分析评论和预测、中小公司消息、与加密市场关系不大的新闻，以及“已推送过的事件”里已经报过的同一件事（不同来源或换个说法也算重复）。

准确性（最重要，宁可不推也不能推错）：
- 每条候选都标了来源类型。「可靠来源」是官方机构或主流媒体，「快讯」是速度快但可能有误的快讯账号。
- 「快讯」的消息，只有在「可靠来源」（候选里的或“近期可靠来源报道”里的）报道了同一件事、关键事实和数字一致时，才能推送；推送时 summary 以可靠来源为准。
- 「快讯」里看起来很重要、但还没有可靠来源证实的，不要推送，放进 watch 列表等待证实。
- 传闻、“据知情人士/据报道/可能/考虑中”之类未经证实的说法、预测、评论，一律不推送，也不放进 watch。
- 不要编造或推算数字，所有数字必须和来源原文一致。

非常严格：一天通常只有几条甚至没有。拿不准就不推。

只输出 JSON：{"push":[{"i":候选序号,"title":"中文一句话标题（不超过 30 字）","summary":"中文 1-2 句要点","impact":"对比特币和加密市场的可能影响，一句话","confirmed_by":"证实它的可靠来源名称"}],"watch":[快讯候选序号]}。没有就输出 {"push":[],"watch":[]}。"""

DIGEST_PROMPT = """下面是过去 24 小时推送给用户的重要新闻（按时间顺序）。请写一份中文汇总，给一位加密货币投资者起床后快速浏览：
1. 开头一句话总结今天对加密市场最重要的事
2. 分两段：宏观（美联储、经济数据、政策）/ 加密行业（某段没有内容就写“无重大消息”）
3. 每段用短句列出要点，总共不超过 300 字
只根据给出的内容写，不要编造。纯文本输出，不要用 Markdown 符号。"""


# ---------- storage ----------

def init_db(db):
    db.execute("CREATE TABLE IF NOT EXISTS news_pushed (ts REAL, title TEXT, summary TEXT, source TEXT, link TEXT)")
    # 最近收到的所有消息（用来核实快讯）和等待证实的快讯
    db.execute("CREATE TABLE IF NOT EXISTS news_recent (id TEXT PRIMARY KEY, ts REAL, item TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS news_watch (id TEXT PRIMARY KEY, ts REAL, item TEXT)")
    db.commit()


def _dump(it):
    d = dict(it)
    d["pub_dt"] = it["pub_dt"].isoformat() if it["pub_dt"] else None
    return json.dumps(d, ensure_ascii=False)


def _load(txt):
    d = json.loads(txt)
    d["pub_dt"] = datetime.fromisoformat(d["pub_dt"]) if d.get("pub_dt") else None
    return d


def remember(db, items):
    now = time.time()
    for it in items:
        db.execute("INSERT OR REPLACE INTO news_recent VALUES (?,?,?)", (it["id"], now, _dump(it)))
    db.execute("DELETE FROM news_recent WHERE ts < ?", (now - 3 * 3600,))
    db.execute("DELETE FROM news_watch WHERE ts < ?", (now - WATCH_HOURS * 3600,))
    db.commit()


def reliable_context(db, exclude_ids):
    rows = db.execute("SELECT id, item FROM news_recent ORDER BY ts DESC LIMIT 120").fetchall()
    out = []
    for rid, txt in rows:
        it = _load(txt)
        if rid not in exclude_ids and not is_quick(it["source"]):
            out.append(it)
    return out[:60]


def watched(db):
    return [_load(r[0]) for r in db.execute("SELECT item FROM news_watch ORDER BY ts").fetchall()]


def recent_pushed(db, hours=24, limit=40):
    since = time.time() - hours * 3600
    return db.execute("SELECT ts, title, summary, source, link FROM news_pushed WHERE ts > ? ORDER BY ts DESC LIMIT ?",
                      (since, limit)).fetchall()


# ---------- fetching ----------

def _text(el, *tags):
    for t in tags:
        v = el.findtext(t)
        if v:
            return v.strip()
    return ""


def parse_generic_feed(xml_bytes, source):
    """RSS 2.0 or Atom → [{id, source, title, text, link, pub_dt}]"""
    root = ET.fromstring(xml_bytes)
    out = []
    atom = "{http://www.w3.org/2005/Atom}"
    entries = list(root.iter("item")) or list(root.iter(atom + "entry"))
    for it in entries:
        title = m.html_to_text(_text(it, "title", atom + "title"))
        desc = m.html_to_text(_text(it, "description", atom + "summary", atom + "content"))
        link = _text(it, "link")
        if not link:
            le = it.find(atom + "link")
            link = le.get("href", "") if le is not None else ""
        guid = _text(it, "guid", atom + "id") or link or title
        pub = _text(it, "pubDate", atom + "updated", atom + "published")
        out.append({
            "id": "news:" + hashlib.sha1(f"{source}|{guid}".encode()).hexdigest()[:20],
            "source": source,
            "title": title,
            "text": desc[:600],
            "link": m.to_x_link(link) if "/status/" in link else link,
            "pub_dt": parse_date(pub),
        })
    return out


def parse_date(s):
    if not s:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(s.strip(), fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except Exception:
        return None


def sources():
    out = [(name, url) for name, url in NEWS_RSS]
    for a in NEWS_X_ACCOUNTS:
        out.append((f"X @{a}", f"{m.RSSHUB_URL}/twitter/user/{urllib.parse.quote(a)}/excludeReplies=1&includeRts=false"))
    return out


def is_quick(source):
    """快讯账号：快但可能有传闻，必须有可靠来源证实才推送"""
    return source.startswith("X @")


def fetch_new(db):
    """New items from all sources (first sight of a source = remember silently)."""
    fresh = []
    now = datetime.now(timezone.utc)
    for name, url in sources():
        try:
            items = parse_generic_feed(m._http(url, timeout=30), name)
        except Exception as e:
            log.warning("news fetch %s failed: %s", name, e)
            continue
        key = "news:" + name
        first = not m.account_initialized(db, key)
        for it in items:
            if m.is_seen(db, it["id"]):
                continue
            m.mark_seen(db, it["id"], key)
            too_old = it["pub_dt"] and (now - it["pub_dt"]).total_seconds() > m.MAX_AGE_MINUTES * 60
            if not first and not too_old and (it["title"] or it["text"]):
                fresh.append(it)
        if first:
            m.set_initialized(db, key)
            log.info("news source %s initialized with %d items", name, len(items))
    return fresh


# ---------- ChatGPT ----------

def chat(system, user, json_mode=False, timeout=90):
    body = {"model": NEWS_MODEL, "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    data = json.loads(m._http(m.OPENAI_BASE_URL + "/chat/completions", json.dumps(body).encode(),
                              {"Authorization": "Bearer " + m.OPENAI_API_KEY, "Content-Type": "application/json"},
                              timeout=timeout))
    return data["choices"][0]["message"]["content"]


def vn_time(dt):
    return dt.astimezone(m.VN_TZ).strftime("%m-%d %H:%M") if dt else ""


def pick_important(db, items):
    """items: new items + quick items waiting for confirmation. Returns (pushes, watch_items)."""
    done = recent_pushed(db)
    ctx = reliable_context(db, {it["id"] for it in items})
    lines = ["已推送过的事件（过去 24 小时）："] + ([f"- {r[1]}" for r in done] or ["（无）"])
    lines.append("\n近期可靠来源报道（过去 3 小时，只用来核实快讯，不要推送这些）：")
    lines += [f"- [{it['source']} {vn_time(it['pub_dt'])}] {it['title']}：{it['text'][:200]}" for it in ctx] or ["（无）"]
    lines.append("\n候选消息：")
    for i, it in enumerate(items):
        kind = "快讯" if is_quick(it["source"]) else "可靠来源"
        lines.append(f"[{i}] 来源类型：{kind}  来源：{it['source']}  时间：{vn_time(it['pub_dt'])}\n"
                     f"标题：{it['title']}\n内容：{it['text'][:400]}")
    reply = json.loads(chat(EDITOR_PROMPT, "\n".join(lines), json_mode=True))
    pushes = []
    for p in reply.get("push", []):
        i = p.get("i")
        if not isinstance(i, int) or not 0 <= i < len(items):
            continue
        it = items[i]
        if is_quick(it["source"]) and not str(p.get("confirmed_by", "")).strip():
            continue  # 快讯没有写明证实来源：不推
        pushes.append((it, p))
    watch = [items[i] for i in reply.get("watch", []) if isinstance(i, int) and 0 <= i < len(items)
             and is_quick(items[i]["source"])]
    return pushes, watch


def build_news_message(it, p):
    parts = [f"📰 <b>{html.escape(p.get('title', '').strip())}</b>"]
    if p.get("summary"):
        parts.append(html.escape(p["summary"].strip()))
    if p.get("impact"):
        parts.append("📈 可能影响：" + html.escape(p["impact"].strip()) + "（判断，不是确定结论）")
    src = f"来源：{html.escape(it['source'])}"
    if p.get("confirmed_by") and is_quick(it["source"]):
        src += f"（已由 {html.escape(str(p['confirmed_by']))} 证实）"
    src = src + (f"  {vn_time(it['pub_dt'])}（越南时间）" if it["pub_dt"] else "")
    if it["link"]:
        src += f'\n🔗 <a href="{html.escape(it["link"])}">打开原文</a>'
    parts.append(src)
    return "\n\n".join(parts)


# ---------- loop step (called from monitor.main) ----------

def enabled():
    return NEWS_ENABLED and bool(m.OPENAI_API_KEY)


def poll(db, state):
    if not enabled() or time.time() - state.get("news_last", 0) < NEWS_POLL_SECONDS:
        return
    state["news_last"] = time.time()
    new = fetch_new(db)
    remember(db, new)
    waiting = [w for w in watched(db) if w["id"] not in {n["id"] for n in new}]
    # 没有新消息时，只有在有新的可靠来源报道时才需要重新核实观察中的快讯
    if not new:
        daily_digest(db)
        return
    candidates = new + waiting
    for start in range(0, len(candidates), 40):   # keep each ChatGPT call small
        batch = candidates[start:start + 40]
        try:
            picked, watch = pick_important(db, batch)
        except Exception as e:
            log.warning("news editor failed: %s", e)
            continue
        log.info("news: %d candidates, %d picked, %d watching", len(batch), len(picked), len(watch))
        for it in watch:
            db.execute("INSERT OR IGNORE INTO news_watch VALUES (?,?,?)", (it["id"], time.time(), _dump(it)))
        for it, p in picked:
            db.execute("DELETE FROM news_watch WHERE id = ?", (it["id"],))
            if m.send_telegram(build_news_message(it, p)):
                db.execute("INSERT INTO news_pushed VALUES (?,?,?,?,?)",
                           (time.time(), p.get("title", ""), p.get("summary", ""), it["source"], it["link"]))
        db.commit()
    daily_digest(db)


def daily_digest(db):
    if DAILY_DIGEST_HOUR < 0:
        return
    now = datetime.now(m.VN_TZ)
    today = now.strftime("%Y-%m-%d")
    if now.hour < DAILY_DIGEST_HOUR:
        return
    row = db.execute("SELECT v FROM meta WHERE k='digest_day'").fetchone()
    if row and row[0] == today:
        return
    db.execute("INSERT OR REPLACE INTO meta VALUES ('digest_day', ?)", (today,))
    db.commit()
    rows = list(reversed(recent_pushed(db, hours=24, limit=80)))
    if not rows:
        m.send_telegram("🗞 过去 24 小时没有重大经济、科技或加密新闻。")
        return
    text = "\n".join(f"{datetime.fromtimestamp(r[0], m.VN_TZ).strftime('%m-%d %H:%M')} {r[1]}：{r[2]}" for r in rows)
    try:
        digest = chat(DIGEST_PROMPT, text)
    except Exception as e:
        log.warning("digest failed: %s", e)
        digest = "\n".join(f"• {r[1]}" for r in rows)
    m.send_telegram("🗞 <b>过去 24 小时要闻汇总</b>\n\n" + html.escape(digest.strip()))
