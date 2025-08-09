import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="YouTube Sentiment Trader", layout="wide")
st.title("😀 Automated YouTube Sentiment Trader")

# Your repo info (change only if you rename things)
GH_USER = "dumplingdood"
GH_REPO = "robotbeatsdad103"
FEED_PATH = "data/feed.json"

# 1) Prefer GitHub Pages (pretty URL), 2) fallback to raw file (always works)
FEED_URLS = [
    f"https://{GH_USER}.github.io/{GH_REPO}/{FEED_PATH}",
    f"https://raw.githubusercontent.com/{GH_USER}/{GH_REPO}/main/{FEED_PATH}",
]

def _try_fetch(url: str):
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return r.json()

def _normalize_payload(payload):
    """Make sure we always have the keys we expect."""
    if not isinstance(payload, dict):
        return {"last_updated": "", "rows": [], "error": "Invalid JSON structure"}
    payload.setdefault("last_updated", "")
    payload.setdefault("rows", [])
    return payload

@st.cache_data(ttl=900)
def load_feed():
    errors = []
    for url in FEED_URLS:
        try:
            data = _try_fetch(url)
            return _normalize_payload(data), url, None
        except Exception as e:
            errors.append(f"{url} → {e}")
    return {"last_updated": "", "rows": []}, None, "\n".join(errors)

# ------- UI -------
col1, _ = st.columns([1, 2])
with col1:
    if st.button("🔁 Refresh now"):
        load_feed.clear()
        st.success("Cache cleared — reloading…")
        st.rerun()

with st.spinner("Loading feed…"):
    payload, used_url, err = load_feed()

if used_url:
    st.caption(f"Last updated (UTC): {payload.get('last_updated','unknown')}  ·  Source: {used_url}")
else:
    st.error(
        "Couldn't download **data/feed.json** from GitHub.\n\n"
        "I tried both your GitHub Pages URL and the raw file URL.\n\n"
        f"Errors:\n{err or 'Unknown error'}\n\n"
        "Quick checks:\n"
        "• Make sure the file exists in your repo at **data/feed.json**.\n"
        "• If GitHub Pages is disabled, the app will still use the raw URL."
    )

rows = payload.get("rows", [])
if not rows:
    st.warning(
        "No data found in the feed yet.\n\n"
        "If you just set things up, run the **Build feed.json** workflow in GitHub → Actions.\n"
        "You can also create a placeholder file at **data/feed.json** with `{}` to remove this message."
    )
else:
    df = pd.DataFrame(rows)
    for _, row in df.iterrows():
        st.markdown(f"## {row.get('Name','(Unknown Channel)')}")
        title = row.get("Video Title", "Unavailable")
        url = row.get("URL", "")
        pub = row.get("Published", "")
        if url:
            st.markdown(f"**Video:** [{title}]({url})  |  **Published:** {pub}")
        else:
            st.markdown(f"**Video:** {title}  |  **Published:** {pub}")

        st.markdown(f"**Sentiment:** {row.get('Sentiment','🟣 Unknown')}")
        st.markdown(f"**Summary:** {row.get('Summary','') or '—'}")

        note = row.get("TranscriptNote")
        if note:
            st.caption(note)

        # Key Points
        keypoints = row.get("KeyPoints") or []
        if keypoints:
            st.markdown("**Key Points (trading):**")
            for p in keypoints:
                st.markdown(f"- {p}")

        # Entities
        ents = row.get("Entities") or {}
        chips = []
        if ents.get("tickers"): chips.append("**Tickers:** " + ", ".join(ents["tickers"]))
        if ents.get("macro"):   chips.append("**Macro:** " + ", ".join(ents["macro"]))
        if ents.get("actions"): chips.append("**Actions:** " + ", ".join(ents["actions"]))
        if ents.get("levels"):  chips.append("**Levels/Calls:** " + " | ".join(ents["levels"]))
        if chips:
            st.markdown(" <br/> ".join(chips), unsafe_allow_html=True)

        st.markdown("---")
