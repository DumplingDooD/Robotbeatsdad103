import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="YouTube Sentiment Trader", layout="wide")
st.title("😀 Automated YouTube Sentiment Trader")

# ✅ CHANGE THIS after Step 4 to your real GitHub Pages URL
FEED_URL = "https://<YOUR_GITHUB_USERNAME>.github.io/<YOUR_REPO_NAME>/data/feed.json"

@st.cache_data(ttl=900)  # 15 min cache
def load_feed():
    r = requests.get(FEED_URL, timeout=20)
    r.raise_for_status()
    return r.json()

col1, _ = st.columns([1, 2])
with col1:
    if st.button("🔁 Refresh now"):
        load_feed.clear()
        st.success("Cache cleared — reloading…")
        st.rerun()

with st.spinner("Loading feed…"):
    payload = load_feed()

st.caption(f"Last updated (UTC): {payload.get('last_updated','unknown')}")

rows = payload.get("rows", [])
if not rows:
    st.warning("No data found yet. The GitHub Action will generate data/feed.json for you.")
else:
    df = pd.DataFrame(rows)
    for _, row in df.iterrows():
        st.markdown(f"## {row['Name']}")
        st.markdown(f"**Video:** [{row['Video Title']}]({row['URL']})  |  **Published:** {row['Published']}")
        st.markdown(f"**Sentiment:** {row['Sentiment']}")
        st.markdown(f"**Summary:** {row['Summary']}")
        if row.get("TranscriptNote"):
            st.caption(row["TranscriptNote"])

        if row.get("KeyPoints"):
            st.markdown("**Key Points (trading):**")
            for p in row["KeyPoints"]:
                st.markdown(f"- {p}")

        ents = row.get("Entities", {})
        chips = []
        if ents.get("tickers"): chips.append("**Tickers:** " + ", ".join(ents["tickers"]))
        if ents.get("macro"):   chips.append("**Macro:** " + ", ".join(ents["macro"]))
        if ents.get("actions"): chips.append("**Actions:** " + ", ".join(ents["actions"]))
        if ents.get("levels"):  chips.append("**Levels/Calls:** " + " | ".join(ents["levels"]))
        if chips:
            st.markdown(" <br/> ".join(chips), unsafe_allow_html=True)

        st.markdown("---")
