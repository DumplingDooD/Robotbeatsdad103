import re
import time
import requests
import xmltodict
import pandas as pd
import streamlit as st
from datetime import datetime, timezone
from youtube_transcript_api import (
    YouTubeTranscriptApi,
    NoTranscriptFound,
    TranscriptsDisabled,
    CouldNotRetrieveTranscript,
)

# -------------------------------------------------
# App settings
# -------------------------------------------------
st.set_page_config(page_title="Automated YouTube Sentiment Trader", layout="wide")
st.title("😀 Automated YouTube Sentiment Trader")

DEBUG = False  # turn True to show a small diagnostics panel per video

# -------------------------------------------------
# Fixed YouTuber Channel IDs
# -------------------------------------------------
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
USER_AGENT = {"User-Agent": "yt-daily-cache/1.0"}

# -------------------------------------------------
# Trading heuristics
# -------------------------------------------------
CRYPTO = {"BTC","ETH","SOL","ADA","XRP","DOT","LINK","AVAX","MATIC","DOGE","ARB","OP","ATOM","BNB"}
MACRO_TERMS = {"cpi","inflation","jobs","nonfarm","payrolls","pce","core","fomc","fed","rate","hike","cut","ecb","boe","gdp","recession","etf","halving","halvening","treasury","yields","bond"}
ACTIONS = {"buy","sell","accumulate","take profit","tp","stop","stop loss","short","long","hedge","entry","target"}
LEVEL_WORDS = {"support","resistance","target","entry","stop","stoploss","stop-loss"}

TICKER_DOLLAR = re.compile(r"\$[A-Z]{1,5}\b")
PLAIN_TICKER  = re.compile(r"\b[A-Z]{2,5}\b")
PCT   = re.compile(r"\b-?\d+(?:\.\d+)?%")
PRICE = re.compile(r"(?:\$|£|€)\s?\d{1,3}(?:,\d{3})*(?:\.\d+)?")
LEVEL_NEAR = re.compile(r"(support|resistance|target|entry|stop)[^.\n]{0,80}", re.I)

def split_sentences(text: str):
    return re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text).strip())

def score_sentence(s: str) -> int:
    s_low = s.lower()
    score = 0
    score += 3 * len(TICKER_DOLLAR.findall(s))
    score += 2 * len(PCT.findall(s))
    score += 2 * len(PRICE.findall(s))
    if any(w in s_low for w in LEVEL_WORDS): score += 2
    if any(w in s_low for w in ACTIONS): score += 2
    if any(w in s_low for w in MACRO_TERMS): score += 1
    score += sum(1 for c in CRYPTO if c in s)
    return score

def extract_entities(text: str):
    tickers = set(x[1:] for x in TICKER_DOLLAR.findall(text))
    for sym in CRYPTO:
        if re.search(rf"\b{sym}\b", text): tickers.add(sym)
    for m in PLAIN_TICKER.findall(text):
        if m in CRYPTO: tickers.add(m)
    macro   = sorted({w for w in MACRO_TERMS if re.search(rf"\b{w}\b", text.lower())})
    actions = sorted({w for w in ACTIONS if re.search(rf"\b{w}\b", text.lower())})
    levels = []
    for sent in split_sentences(text):
        if re.search(LEVEL_NEAR, sent):
            price_hits = PRICE.findall(sent); pct_hits = PCT.findall(sent)
            pieces = []
            if price_hits: pieces.append(" ".join(price_hits[:3]))
            if pct_hits:   pieces.append(" ".join(pct_hits[:3]))
            levels.append(f"{sent.strip()}" + (f"  ➜ {', '.join(pieces)}" if pieces else ""))
    return {"tickers": sorted(tickers), "macro": macro, "actions": actions, "levels": levels[:5]}

def pick_key_points(text: str, k: int = 5):
    sents = split_sentences(text)
    scored = sorted(((score_sentence(s), s) for s in sents if len(s) > 30), reverse=True)
    seen, out = set(), []
    for _, s in scored:
        ss = s.strip().lower()
        if ss in seen: continue
        seen.add(ss); out.append(s.strip())
        if len(out) >= k: break
    return out

# -------------------------------------------------
# RSS helper
# -------------------------------------------------
def rss_latest_video(channel_id: str):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    r = requests.get(url, timeout=20, headers=USER_AGENT); r.raise_for_status()
    feed = xmltodict.parse(r.text)
    entry = feed.get("feed", {}).get("entry")
    if isinstance(entry, list): entry = entry[0]
    if not entry: raise RuntimeError("No entries found in RSS feed")
    vid = entry.get("yt:videoId")
    title = entry.get("title", "Untitled")
    link  = entry.get("link", {})
    href  = link.get("@href") if isinstance(link, dict) else f"https://www.youtube.com/watch?v={vid}"
    pub   = entry.get("published", ""); pub_date = pub[:10] if pub else ""
    return vid, title, href, pub_date

def tidy_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()

def quick_summary(text: str, max_sentences: int = 5) -> str:
    text = tidy_text(text)
    if not text: return ""
    parts = split_sentences(text)
    summary = " ".join(parts[:max_sentences])
    return summary if summary.endswith((".", "!", "?")) else (summary + ".")

def label_to_icon(label: str) -> str:
    if label == "POSITIVE": return "🟢 Bullish"
    if label == "NEGATIVE": return "🔴 Bearish"
    return "🟡 Neutral"

# -------------------------------------------------
# Caption/Transcript helpers
# 1) Try youtube-transcript-api (human/en → auto/en → translate→en)
# 2) Fallback to yt-dlp: fetch caption URL(s) and download VTT, parse to text
# -------------------------------------------------
def segments_to_text(segments):
    return " ".join(s.get("text","") for s in (segments or []) if s.get("text"))

def vtt_to_text(vtt: str) -> str:
    # Strip WebVTT timestamps and cues; keep text lines
    lines = []
    for line in vtt.splitlines():
        if not line or "-->" in line or line.startswith("WEBVTT"): continue
        if re.match(r"^\d+$", line.strip()): continue  # cue number
        lines.append(line.strip())
    return tidy_text(" ".join(lines))

def fetch_with_youtube_transcript_api(video_id: str):
    """Return (text, lang_code, translated, debug_info) or (None, None, False, info)."""
    langs_en = ["en","en-US","en-GB"]
    info = {"lib":"yta","steps":[]}
    try:
        if hasattr(YouTubeTranscriptApi, "list_transcripts"):
            transcripts = YouTubeTranscriptApi.list_transcripts(video_id)
            info["steps"].append("list_transcripts_ok")

            try:
                t = transcripts.find_transcript(langs_en)
                segs = t.fetch()
                info["steps"].append(f"human:{t.language_code}")
                return segments_to_text(segs), t.language_code, False, info
            except NoTranscriptFound:
                info["steps"].append("no_human_en")

            try:
                t = transcripts.find_generated_transcript(langs_en)
                segs = t.fetch()
                info["steps"].append(f"auto:{t.language_code}")
                return segments_to_text(segs), t.language_code, False, info
            except NoTranscriptFound:
                info["steps"].append("no_auto_en")

            for t in transcripts:
                try:
                    if hasattr(t, "translate"):
                        t_en = t.translate("en")
                        segs = t_en.fetch()
                        info["steps"].append(f"translated_from:{getattr(t,'language_code',None)}")
                        return segments_to_text(segs), getattr(t,"language_code",None), True, info
                except Exception:
                    continue
            info["steps"].append("no_translatable_tracks")
            return None, None, False, info

        if hasattr(YouTubeTranscriptApi, "get_transcript"):
            try:
                segs = YouTubeTranscriptApi.get_transcript(video_id, languages=langs_en)
                info["steps"].append("legacy_get_transcript_ok")
                return segments_to_text(segs), "en", False, info
            except Exception as e:
                info["steps"].append(f"legacy_error:{e}")
                return None, None, False, info

        info["steps"].append("api_missing")
        return None, None, False, info

    except (NoTranscriptFound, TranscriptsDisabled, CouldNotRetrieveTranscript) as e:
        info["steps"].append(f"yta_exception:{type(e).__name__}")
        return None, None, False, info
    except Exception as e:
        info["steps"].append(f"yta_generic:{e}")
        return None, None, False, info

def fetch_with_ytdlp(video_id: str):
    """Return (text, lang_code, translatedFalse, debug_info) or (None, None, False, info)."""
    info = {"lib":"yt-dlp","steps":[]}
    try:
        from yt_dlp import YoutubeDL
        ydl_opts = {
            "quiet": True,
            "skip_download": True,
            "listsubtitles": True,
        }
        with YoutubeDL(ydl_opts) as ydl:
            data = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        subs = (data.get("subtitles") or {})  # human
        autos = (data.get("automatic_captions") or {})  # auto
        info["steps"].append(f"subs_keys:{list(subs.keys())[:5]}")
        info["steps"].append(f"auto_keys:{list(autos.keys())[:5]}")

        # prefer English human, then English auto, else first available (we won't translate here)
        ordered_keys = []
        for keyset in (subs, autos):
            for k in ("en","en-US","en-GB"):
                if k in keyset: ordered_keys.append(("human" if keyset is subs else "auto", k))
        if not ordered_keys:
            # any human then auto
            if subs:
                ordered_keys = [("human", next(iter(subs.keys())))]
            elif autos:
                ordered_keys = [("auto", next(iter(autos.keys())))]
            else:
                info["steps"].append("no_caption_tracks")
                return None, None, False, info

        kind, lang = ordered_keys[0]
        lst = (subs if kind=="human" else autos)[lang]
        if not lst:
            info["steps"].append("empty_track_list")
            return None, None, False, info

        # pick first URL and fetch VTT
        url = lst[0].get("url")
        if not url:
            info["steps"].append("no_url_in_track")
            return None, None, False, info
        r = requests.get(url, timeout=30, headers=USER_AGENT)
        if not r.ok:
            info["steps"].append(f"http_error:{r.status_code}")
            return None, None, False, info
        text = vtt_to_text(r.text)
        info["steps"].append(f"fetched_vtt:{lang}:{kind}")
        return text, lang, False, info

    except Exception as e:
        info["steps"].append(f"ytdlp_exception:{e}")
        return None, None, False, info

def fetch_transcript_text(video_id: str):
    """
    Unified entry:
      1) youtube-transcript-api (incl. auto-translate to en)
      2) yt-dlp fallback (fetch VTT URL and parse)
    Returns: (text, src_lang, was_translated, debug_info)
    """
    txt, lang, trans, info1 = fetch_with_youtube_transcript_api(video_id)
    if txt: return txt, lang, trans, info1
    txt, lang, trans, info2 = fetch_with_ytdlp(video_id)
    info = {"yta": info1, "ytdlp": info2}
    return txt, lang, trans, info
# -------------------------------------------------

@st.cache_resource
def get_sentiment_pipeline():
    from transformers import pipeline
    return pipeline("sentiment-analysis", model="distilbert-base-uncased-finetuned-sst-2-english")

@st.cache_data(ttl=86400)
def fetch_and_analyze_daily():
    sentiment = get_sentiment_pipeline()
    rows = []
    debugs = {}  # id -> info (only if DEBUG)

    for channel_id, name in YOUTUBERS.items():
        try:
            video_id, video_title, video_url, published = rss_latest_video(channel_id)
            tx, src_lang, was_translated, dbg = fetch_transcript_text(video_id)
            if DEBUG: debugs[video_id] = dbg

            if tx:
                full = tidy_text(tx)
                sample = full[:1024]
                result = sentiment(sample[:512])[0]
                sentiment_icon = label_to_icon(result["label"])
                summary = quick_summary(sample)
                ents = extract_entities(full)
                bullets = pick_key_points(full, k=5)
                transcript_note = f"(auto-translated from {src_lang})" if was_translated and src_lang else f"(lang: {src_lang})" if src_lang else ""
            else:
                sentiment_icon = "🟣 Unknown"
                summary = "Transcript unavailable."
                ents = {"tickers": [], "macro": [], "actions": [], "levels": []}
                bullets = []
                transcript_note = ""

            rows.append({
                "Name": name,
                "Video Title": video_title,
                "Published": published,
                "URL": video_url,
                "Summary": summary,
                "Sentiment": sentiment_icon,
                "KeyPoints": bullets,
                "Entities": ents,
                "TranscriptNote": transcript_note,
                "VideoID": video_id
            })

        except Exception as e:
            rows.append({
                "Name": name,
                "Video Title": "Unavailable",
                "Published": "",
                "URL": "",
                "Summary": f"Error fetching latest video: {e}",
                "Sentiment": "🟣 Unknown",
                "KeyPoints": [],
                "Entities": {"tickers": [], "macro": [], "actions": [], "levels": []},
                "TranscriptNote": "",
                "VideoID": None
            })
            time.sleep(0.4)

    df = pd.DataFrame(rows)
    last_updated = datetime.now(timezone.utc).isoformat()
    return df, last_updated, debugs

# ----------------------------
# UI
# ----------------------------
col1, _ = st.columns([1, 2])
with col1:
    if st.button("🔁 Refresh now (clear daily cache)"):
        fetch_and_analyze_daily.clear()
        st.success("Cache cleared — reloading…")
        st.rerun()

st.subheader("🎥 Latest YouTuber Sentiment Dashboard")
with st.spinner("Fetching…"):
    df, last_updated_iso, debugs = fetch_and_analyze_daily()

st.caption(f"Last updated (UTC): {last_updated_iso}")

if df.empty:
    st.warning("No data found.")
else:
    for idx, row in df.iterrows():
        st.markdown(f"## {row['Name']}")
        st.markdown(f"**Video:** [{row['Video Title']}]({row['URL']})  |  **Published:** {row['Published']}")
        st.markdown(f"**Sentiment:** {row['Sentiment']}")
        st.markdown(f"**Summary:** {row['Summary']}")
        if row.get("TranscriptNote"):
            st.caption(row["TranscriptNote"])

        if row["KeyPoints"]:
            st.markdown("**Key Points (trading):**")
            for p in row["KeyPoints"]:
                st.markdown(f"- {p}")

        ents = row["Entities"]
        chips = []
        if ents["tickers"]: chips.append("**Tickers:** " + ", ".join(ents["tickers"]))
        if ents["macro"]:   chips.append("**Macro:** " + ", ".join(ents["macro"]))
        if ents["actions"]: chips.append("**Actions:** " + ", ".join(ents["actions"]))
        if ents["levels"]:
            chips.append("**Levels/Calls:** " + " | ".join(ents["levels"]))
        if chips:
            st.markdown(" <br/> ".join(chips), unsafe_allow_html=True)

        if DEBUG and row.get("VideoID") and row["VideoID"] in debugs:
            with st.expander("Debug: caption discovery"):
                st.code(debugs[row["VideoID"]], language="json")

        st.markdown("---")
