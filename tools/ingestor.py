import os, re, time, json
import requests, xmltodict
from datetime import datetime, timezone
from pathlib import Path
from youtube_transcript_api import (
    YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled, CouldNotRetrieveTranscript
)

USER_AGENT = {"User-Agent": "yt-ingestor/1.0"}
REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUT_PATH = DATA_DIR / "feed.json"

YOUTUBERS = {
    "UClgJyzwGs-GyaNxUHcLZrkg": "InvestAnswers",
    "UCqK_GSMbpiV8spgD3ZGloSw": "Coin Bureau",
    "UC9ZM3N0ybRtp44-WLqsW3iQ": "Mark Moss",
    "UCFU-BE5HRJoudqIz1VDKlhQ": "CTO Larsson",
    "UCRvqjQPSeaWn-uEx-w0XOIg": "Benjamin Cowen",
    "UCtOV5M-T3GcsJAq8QKaf0lg": "Bitcoin Magazine",
    "UCpvyOqtEc86X8w8_Se0t4-w": "George Gammon",
    "UCK-zlnUfoDHzUwXcbddtnkg": "ArkInvest",
}

CRYPTO = {"BTC","ETH","SOL","ADA","XRP","DOT","LINK","AVAX","MATIC","DOGE","ARB","OP","ATOM","BNB"}
MACRO_TERMS = {"cpi","inflation","jobs","nonfarm","payrolls","pce","core","fomc","fed","rate","hike","cut","ecb","boe","gdp","recession","etf","halving","halvening","treasury","yields","bond"}
ACTIONS = {"buy","sell","accumulate","take profit","tp","stop","stop loss","short","long","hedge","entry","target"}
LEVEL_WORDS = {"support","resistance","target","entry","stop","stoploss","stop-loss"}

TICKER_DOLLAR = re.compile(r"\$[A-Z]{1,5}\b")
PLAIN_TICKER  = re.compile(r"\b[A-Z]{2,5}\b")
PCT   = re.compile(r"\b-?\d+(?:\.\d+)?%")
PRICE = re.compile(r"(?:\$|£|€)\s?\d{1,3}(?:,\d{3})*(?:\.\d+)?")
LEVEL_NEAR = re.compile(r"(support|resistance|target|entry|stop)[^.\n]{0,80}", re.I)

def tidy_text(t): return re.sub(r"\s+", " ", t or "").strip()
def split_sents(t): return re.split(r"(?<=[.!?])\s+", tidy_text(t))

def score_sentence(s):
    s_low = s.lower(); score = 0
    score += 3 * len(TICKER_DOLLAR.findall(s))
    score += 2 * len(PCT.findall(s))
    score += 2 * len(PRICE.findall(s))
    if any(w in s_low for w in LEVEL_WORDS): score += 2
    if any(w in s_low for w in ACTIONS): score += 2
    if any(w in s_low for w in MACRO_TERMS): score += 1
    score += sum(1 for c in CRYPTO if c in s)
    return score

def extract_entities(text):
    tickers = set(x[1:] for x in TICKER_DOLLAR.findall(text))
    for sym in CRYPTO:
        if re.search(rf"\b{sym}\b", text): tickers.add(sym)
    for m in PLAIN_TICKER.findall(text):
        if m in CRYPTO: tickers.add(m)
    macro   = sorted({w for w in MACRO_TERMS if re.search(rf"\b{w}\b", text.lower())})
    actions = sorted({w for w in ACTIONS if re.search(rf"\b{w}\b", text.lower())})
    levels = []
    for sent in split_sents(text):
        if re.search(LEVEL_NEAR, sent):
            price_hits = PRICE.findall(sent); pct_hits = PCT.findall(sent)
            pieces = []
            if price_hits: pieces.append(" ".join(price_hits[:3]))
            if pct_hits:   pieces.append(" ".join(pct_hits[:3]))
            levels.append(f"{sent.strip()}" + (f"  ➜ {', '.join(pieces)}" if pieces else ""))
    return {"tickers": sorted(tickers), "macro": macro, "actions": actions, "levels": levels[:5]}

def pick_key_points(text, k=5):
    sents = split_sents(text)
    scored = sorted(((score_sentence(s), s) for s in sents if len(s) > 30), reverse=True)
    out, seen = [], set()
    for _, s in scored:
        ss = s.strip().lower()
        if ss in seen: continue
        seen.add(ss); out.append(s.strip())
        if len(out) >= k: break
    return out

def summary(text, n=5):
    parts = split_sents(text)
    if not parts: return ""
    s = " ".join(parts[:n])
    return s if s.endswith((".", "!", "?")) else s + "."

def rss_latest_video(channel_id):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    r = requests.get(url, timeout=20, headers=USER_AGENT); r.raise_for_status()
    feed = xmltodict.parse(r.text)
    entry = feed.get("feed", {}).get("entry")
    if isinstance(entry, list): entry = entry[0]
    if not entry: raise RuntimeError("No entries")
    vid = entry.get("yt:videoId")
    title = entry.get("title", "Untitled")
    link  = entry.get("link", {}); href = link.get("@href") if isinstance(link, dict) else f"https://www.youtube.com/watch?v={vid}"
    pub   = entry.get("published", ""); pub_date = pub[:10] if pub else ""
    return vid, title, href, pub_date

def segments_to_text(segs): return " ".join(s.get("text","") for s in (segs or []) if s.get("text"))

def vtt_to_text(vtt):
    lines = []
    for line in vtt.splitlines():
        if not line or "-->" in line or line.startswith("WEBVTT"): continue
        if re.match(r"^\d+$", line.strip()): continue
        lines.append(line.strip())
    return tidy_text(" ".join(lines))

def try_yta(video_id):
    langs = ["en","en-US","en-GB"]
    try:
        tr = YouTubeTranscriptApi.list_transcripts(video_id)
        try:
            t = tr.find_transcript(langs); return segments_to_text(t.fetch()), "en", False
        except NoTranscriptFound: pass
        try:
            t = tr.find_generated_transcript(langs); return segments_to_text(t.fetch()), "en", False
        except NoTranscriptFound: pass
        for t in tr:
            try:
                if hasattr(t, "translate"):
                    t_en = t.translate("en"); return segments_to_text(t_en.fetch()), getattr(t,"language_code",None), True
            except Exception: continue
        return None, None, False
    except (NoTranscriptFound, TranscriptsDisabled, CouldNotRetrieveTranscript):
        return None, None, False
    except Exception:
        return None, None, False

def try_ytdlp(video_id, cookies_txt=None):
    try:
        from yt_dlp import YoutubeDL
        opts = {
            "quiet": True, "skip_download": True, "noplaylist": True,
            "writesubtitles": True, "writeautomaticsub": True,
            "subtitlesformat": "vtt", "subtitleslangs": ["en","en.*","en-US","en-GB"],
            "extractor_retries": 3, "sleep_interval_requests": 0.5,
            "http_headers": {"User-Agent": "Mozilla/5.0"},
            "extractor_args": {"youtube": {"player_client": ["android"]}},
        }
        if cookies_txt and Path(cookies_txt).exists(): opts["cookiefile"] = cookies_txt
        with YoutubeDL(opts) as ydl:
            data = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        subs  = data.get("subtitles") or {}
        autos = data.get("automatic_captions") or {}
        def pick(tracks):
            for lang in ("en","en-US","en-GB"):
                if lang in tracks and tracks[lang] and tracks[lang][0].get("url"):
                    return tracks[lang][0]["url"], lang
            for lang, lst in tracks.items():
                if lst and lst[0].get("url"): return lst[0]["url"], lang
            return None, None
        url, lang = pick(subs)
        if not url: url, lang = pick(autos)
        if not url: return None, None, False
        r = requests.get(url, timeout=30, headers=USER_AGENT)
        if not r.ok: return None, None, False
        return vtt_to_text(r.text), lang, False
    except Exception:
        return None, None, False

def rule_sentiment(text):
    t = text.lower(); pos = ["breakout","bullish","rally","accumulate","buy","upside","surge","support holds"]
    neg = ["sell","bearish","breakdown","dump","downside","reject","resistance fails","risk-off"]
    score = sum(t.count(w) for w in pos) - sum(t.count(w) for w in neg)
    return "🟢 Bullish" if score > 0 else "🔴 Bearish" if score < 0 else "🟡 Neutral"

def main():
    cookies_path = os.environ.get("COOKIES_TXT", "")  # GitHub Action can provide this
    rows = []
    for cid, name in YOUTUBERS.items():
        try:
            vid, title, url, pub = rss_latest_video(cid)
            text, lang, translated = try_yta(vid)
            if not text:
                text, lang, translated = try_ytdlp(vid, cookies_path)
            if text:
                full = tidy_text(text); sample = full[:1024]
                sentiment = rule_sentiment(sample)
                summ = summary(sample); ents = extract_entities(full); bullets = pick_key_points(full)
                note = f"(auto-translated from {lang})" if translated and lang else (f"(lang: {lang})" if lang else "")
            else:
                sentiment = "🟣 Unknown"; summ = "Transcript unavailable."
                ents = {"tickers": [], "macro": [], "actions": [], "levels": []}; bullets = []; note = ""
            rows.append({
                "Name": name, "Video Title": title, "Published": pub, "URL": url,
                "Summary": summ, "Sentiment": sentiment, "KeyPoints": bullets,
                "Entities": ents, "TranscriptNote": note, "VideoID": vid
            })
            time.sleep(0.3)
        except Exception as e:
            rows.append({
                "Name": name, "Video Title": "Unavailable", "Published": "", "URL": "",
                "Summary": f"Error: {e}", "Sentiment": "🟣 Unknown",
                "KeyPoints": [], "Entities": {"tickers": [], "macro": [], "actions": [], "levels": []},
                "TranscriptNote": "", "VideoID": None
            })
    payload = {"last_updated": datetime.now(timezone.utc).isoformat(), "rows": rows}
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Wrote {OUT_PATH}")

if __name__ == "__main__":
    main()
