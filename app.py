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
st.set_page_config(page_title="YouTube Sentiment Trader", layout="wide")
st.title("🤖 Automated YouTube Sentiment Trader (Daily Cached • No API Key)")

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
# Helpers
# ----------------------------
def rss_latest_video(channel_id: str):
    """Return (video_id, title, link, published_date) for the most recent upload via RSS."""
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    r = requests.get(url, timeout=20, headers=USER_AGENT)
    r.raise_for_status()
    feed = xmltodict.parse(r.text)
    entry = feed.get("feed", {}).get("entry")

    # If there are multiple entries, the first is the latest
    if isinstance(entry, list):
        entry = entry[0]
    if not entry:
        raise RuntimeError("No entries found in RSS feed")

    video_id = entry.get("yt:videoId")
    title = entry.get("title", "Untitled")
    link = entry.get("link", {})
    link_href = link.get("@href") if isinstance(link, dict) else f"https://www.youtube.com/watch?v={video_id}"
    published = entry.get("published", "")

    # Normalize date to YYYY-MM-DD if available
    published_date = published[:10] if published else ""

    return video_id, title, link_href, published_date


def tidy_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text


def quick_summary(text: str, max_sentences: int = 5) -> str:
    text = tidy_text(text)
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    summary = " ".join(parts[:max_sentences])
    return summary if summary.endswith((".", "!", "?")) else (summary + ".")


def label_to_icon(label: str) -> str:
    if label == "POSITIVE":
        return "🟢 Bullish"
    if label == "NEGATIVE":
        return "🔴 Bearish"
    return "🟡 Neutral"


# ----------------------------
# Cached resources (Model)
# ----------------------------
@st.cache_resource
def get_sentiment_pipeline():
    # Load once per session, not every call
    from transformers import pipeline
    return pipeline("sentiment-analysis", model="distilbert-base-uncased-finetuned-sst-2-english")


# ----------------------------
# Daily cached fetch + analysis
# ----------------------------
@st.cache_data(ttl=86400)  # 24 hours
def fetch_and_analyze_daily():
    """
    For each channel:
      - Get latest video via RSS (no API key)
      - Try transcripts
      - Single short sentiment pass (no chunking)
    Returns: (DataFrame, last_updated_iso)
    """
    sentiment = get_sentiment_pipeline()
    rows = []

    for channel_id, name in YOUTUBERS.items():
        try:
            video_id, video_title, video_url, published = rss_latest_video(channel_id)

            # Try transcript (graceful error handling)
            transcript_text = None
            try:
                transcript = YouTubeTranscriptApi.get_transcript(video_id)
                transcript_text = " ".join(seg.get("text", "") for seg in transcript if seg.get("text"))
                transcript_text = tidy_text(transcript_text)
            except NoTranscriptFound:
                transcript_text = None
                summary = "Transcript unavailable (not provided by channel)."
            except TranscriptsDisabled:
                transcript_text = None
                summary = "Transcript disabled by uploader."
            except CouldNotRetrieveTranscript:
                transcript_text = None
                summary = "Transcript could not be retrieved."
            except Exception as e:
                transcript_text = None
                summary = f"Transcript error: {e}"

            # Sentiment (single short pass; avoid heavy inference)
            if transcript_text:
                sample = transcript_text[:1024]           # sample text to limit work
                result = sentiment(sample[:512])[0]        # single pass
                sentiment_icon = label_to_icon(result["label"])
                summary = quick_summary(sample)
            else:
                sentiment_icon = "⚪️ Unknown"

            rows.append(
                {
                    "Name": name,
                    "Video Title": video_title,
                    "Published": published,
                    "URL": video_url,
                    "Summary": summary,
                    "Sentiment": sentiment_icon,
                }
            )

        except Exception as e:
            # RSS or other failure: keep the channel row visible with a clear message
            rows.append(
                {
                    "Name": name,
                    "Video Title": "Unavailable",
                    "Published": "",
                    "URL": "",
                    "Summary": f"Error fetching latest video: {e}",
                    "Sentiment": "⚪️ Unknown",
                }
            )
            # brief polite backoff so we don't hammer RSS if there are many errors
            time.sleep(0.5)

    df = pd.DataFrame(rows)
    last_updated = datetime.now(timezone.utc).isoformat()
    return df, last_updated


# ----------------------------
# UI: Refresh + Display
# ----------------------------
col1, col2 = st.columns([1, 2])
with col1:
    if st.button("🔄 Refresh now (clear daily cache)"):
        fetch_and_analyze_daily.clear()
        st.success("Cache cleared. Re-running…")
        st.rerun()

with st.spinner("🚀 Fetching daily-cached sentiments…"):
    df, last_updated_iso = fetch_and_analyze_daily()

st.subheader("🎥 Latest YouTuber Sentiment Dashboard")
st.caption(f"Last updated (UTC): {last_updated_iso}")

if df.empty:
    st.warning("No data found.")
else:
    for _, row in df.iterrows():
        with st.container():
            st.markdown(f"### {row['Name']}")
            st.markdown(
                f"**Video:** [{row['Video Title']}]({row['URL']}) &nbsp; | &nbsp; **Published:** {row['Published']}"
            )
            st.markdown(f"**Sentiment:** {row['Sentiment']}")
            st.markdown(f"**Summary:** {row['Summary']}")
            st.markdown("---")

st.markdown(
    """
    This page **fetches the most recent video from each preset channel via RSS**, then **tries to read transcripts** for a quick
    **one-pass sentiment** and **brief summary**.  
    Data is cached for **24 hours**. Use **Refresh** to update immediately.
    """
)
