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

# ----------------------------
# App & Page
# ----------------------------
st.set_page_config(page_title="Automated YouTube Sentiment Trader", layout="wide")
st.title("😀 Automated YouTube Sentiment Trader")

# ----------------------------
# Fixed YouTuber Channel IDs
# ----------------------------
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

# ----------------------------
# Trading heuristics (tweak as you like)
# ----------------------------
CRYPTO = {"BTC","ETH","SOL","ADA","XRP","DOT","LINK","AVAX","MATIC","DOGE","ARB","OP","ATOM","BNB"}
MACRO_TERMS = {
    "cpi","inflation","jobs","nonfarm","payrolls","pce","core","fomc","fed","rate","hike","cut",
    "ecb","boe","gdp","recession","etf","halving","halvening","treasury","yields","bond"
}
ACTIONS = {"buy","sell","accumulate","take profit","tp","stop","stop loss","short","long","hedge","entry","target"}
LEVEL_WORDS = {"support","resistance","target","entry","stop","stoploss","stop-loss"}

TICKER_DOLLAR = re.compile(r"\$[A-Z]{1,5}\b")  # $TSLA style
PLAIN_TICKER = re.compile(r"\b[A-Z]{2,5}\b")   # crude fallback (we'll filter)
PCT = re.compile(r"\b-?\d+(?:\.\d+)?%")
PRICE = re.compile(r"(?:\$|£|€)\s?\d{1,3}(?:,\d{3})*(?:\.\d+)?")
LEVEL_NEAR = re.compile(r"(support|resistance|target|entry|stop)[^.\n]{0,80}", re.I)

def split_sentences(text: str):
    # Simple sentence splitter
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
    # Crypto/asset mentions
    score += sum(1 for c in CRYPTO if c in s)
    return score

def extract_entities(text: str):
    tickers = set(x[1:] for x in TICKER_DOLLAR.findall(text))  # strip leading $
    # Add crypto symbols that appear plainly
    for sym in CRYPTO:
        if re.search(rf"\b{sym}\b", text):
            tickers.add(sym)
    # Very conservative plain-ticker capture (avoid full caps normal words)
    for m in PLAIN_TICKER.findall(text):
        if m in CRYPTO: tickers.add(m)
    macro = sorted({w for w in MACRO_TERMS if re.search(rf"\b{w}\b", text.lower())})
    actions = sorted({w for w in ACTIONS if re.search(rf"\b{w}\b", text.lower())})
    levels = []
    for sent in split_sentences(text):
        if re.search(LEVEL_NEAR, sent):
            price_hits = PRICE.findall(sent)
            pieces = []
            if price_hits: pieces.append(" ".join(price_hits[:3]))
            pct_hits = PCT.findall(sent)
            if pct_hits: pieces.append(" ".join(pct_hits[:3]))
            if pieces:
                levels.append(f"{sent.strip()}  ➜ {', '.join(pieces)}")
            else:
                levels.append(sent.strip())
    return {
        "tickers": sorted(tickers),
        "macro": macro,
        "actions": actions,
        "levels": levels[:5]
    }

def pick_key_points(text: str, k: int = 5):
    sents = split_sentences(text)
    scored = sorted(((score_sentence(s), s) for s in sents if len(s) > 30), reverse=True)
    points = []
    used = set()
    for _, s in scored:
        s_norm = s.strip()
        if s_norm.lower() in used: 
            continue
        used.add(s_norm.lower())
        points.append(s_norm)
        if len(points) >= k: break
    return points

# ----------------------------
# Helpers
# ----------------------------
def rss_latest_video(channel_id: str):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    r = requests.get(url, timeout=20, headers=USER_AGENT)
    r.raise_for_status()
    feed = xmltodict.parse(r.text)
    entry = feed.get("feed", {}).get("entry")
    if isinstance(entry, list):
        entry = entry[0]
    if not entry:
        raise RuntimeError("No entries found in RSS feed")
    video_id = entry.get("yt:videoId")
    title = entry.get("title", "Untitled")
    link = entry.get("link", {})
    link_href = link.get("@href") if isinstance(link, dict) else f"https://www.youtube.com/watch?v={video_id}"
    published = entry.get("published", "")
    published_date = published[:10] if published else ""
    return video_id, title, link_href, published_date

def tidy_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()

def quick_summary(text: str, max_sentences: int = 5) -> str:
    text = tidy_text(text)
    if not text:
        return ""
    parts = split_sentences(text)
    summary = " ".join(parts[:max_sentences])
    return summary if summary.endswith((".", "!", "?")) else (summary + ".")

def label_to_icon(label: str) -> str:
    if label == "POSITIVE": return "🟢 Bullish"
    if label == "NEGATIVE": return "🔴 Bearish"
    return "🟡 Neutral"

def fetch_transcript_text(video_id: str):
    """Prefer human English transcript; fall back to auto-generated English."""
    langs = ["en","en-US","en-GB"]
    try:
        transcripts = YouTubeTranscriptApi.list_transcripts(video_id)
        try:
            t = transcripts.find_transcript(langs)      # human
        except NoTranscriptFound:
            t = transcripts.find_generated_transcript(langs)  # auto
        segments = t.fetch()
        return " ".join(seg.get("text","") for seg in segments if seg.get("text"))
    except (NoTranscriptFound, TranscriptsDisabled, CouldNotRetrieveTranscript):
        return None

# ----------------------------
# Cached resources (Model)
# ----------------------------
@st.cache_resource
def get_sentiment_pipeline():
    from transformers import pipeline
    return pipeline("sentiment-analysis", model="distilbert-base-uncased-finetuned-sst-2-english")

# ----------------------------
# Daily cached fetch + analysis
# ----------------------------
@st.cache_data(ttl=86400)  # 24h cache
def fetch_and_analyze_daily():
    """
    For each channel:
      - Get latest video via RSS (no API key)
      - Try transcript via youtube-transcript-api
      - Single, short sentiment pass
      - Extract trading key points/entities
    Returns: (DataFrame, last_updated_iso)
    """
    sentiment = get_sentiment_pipeline()
    rows = []

    for channel_id, name in YOUTUBERS.items():
        try:
            video_id, video_title, video_url, published = rss_latest_video(channel_id)
            tx = fetch_transcript_text(video_id)

            if tx:
                full = tidy_text(tx)
                sample = full[:1024]                  # quick compute
                result = sentiment(sample[:512])[0]   # one pass
                sentiment_icon = label_to_icon(result["label"])
                summary = quick_summary(sample)

                ents = extract_entities(full)
                bullets = pick_key_points(full, k=5)
            else:
                sentiment_icon = "🟣 Unknown"
                summary = "Transcript unavailable."
                ents = {"tickers": [], "macro": [], "actions": [], "levels": []}
                bullets = []

            rows.append({
                "Name": name,
                "Video Title": video_title,
                "Published": published,
                "URL": video_url,
                "Summary": summary,
                "Sentiment": sentiment_icon,
                "KeyPoints": bullets,
                "Entities": ents
            })

        except Exception as e:
            rows.append({
                "Name": name,
                "Video Title": "Unavailable",
                "Published": "",
                "URL": "",
                "Summary"
           })
