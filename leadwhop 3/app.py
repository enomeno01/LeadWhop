"""LeadWhop — Streamlit UI.

Run with:  streamlit run app.py
"""
import io
import os
import tempfile

import pandas as pd
import streamlit as st

from leadwhop.pipeline import Pipeline, STAGES

st.set_page_config(page_title="LeadWhop", page_icon="🎯", layout="wide")

st.title("🎯 LeadWhop")
st.caption("Company list in → CRM-ready qualified leads out")

# ---------------- Sidebar: keys & stage selection ----------------
with st.sidebar:
    st.header("API keys")
    st.caption("Keys stay in this session only — they are never written to disk.")
    for env_var, label in [("OPENAI_API_KEY", "OpenAI"),
                           ("SERPER_API_KEY", "Serper.dev"),
                           ("LUSHA_API_KEY", "Lusha")]:
        current = os.environ.get(env_var, "")
        value = st.text_input(label, value=current, type="password")
        if value:
            os.environ[env_var] = value

    st.header("Stages")
    stage_labels = {
        "websites": "1 — Find websites",
        "qualify": "2 — Qualify against ICP",
        "contacts": "3 — Find contacts (Lusha)",
        "phones": "4 — Enrich phones",
        "export": "5 — CRM export",
    }
    selected = [s for s in STAGES if st.checkbox(stage_labels[s], value=s in
                                                 ("websites", "qualify"))]

    event_name = ""
    if "export" in selected:
        event_name = st.text_input("Event name (e.g. PLMA 2026)")

# ---------------- Main: upload & run ----------------
uploaded = st.file_uploader(
    "Upload an Excel file — first column must be the company name "
    "(optional columns: Country, Website)",
    type=["xlsx"],
)

if uploaded:
    preview = pd.read_excel(uploaded)
    st.subheader("Input preview")
    st.dataframe(preview.head(10), use_container_width=True)
    st.caption(f"{len(preview)} rows loaded")

    missing_keys = [k for k in ("OPENAI_API_KEY", "SERPER_API_KEY")
                    if not os.environ.get(k)]
    if "contacts" in selected or "phones" in selected:
        if not os.environ.get("LUSHA_API_KEY"):
            missing_keys.append("LUSHA_API_KEY")

    if missing_keys:
        st.warning("Add these keys in the sidebar to run: " + ", ".join(missing_keys))
    elif not selected:
        st.info("Select at least one stage in the sidebar.")
    elif st.button("Run pipeline", type="primary"):
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp.write(uploaded.getvalue())
            tmp_path = tmp.name

        progress = st.progress(0.0, text="Starting…")

        def cb(stage, i, n):
            progress.progress(i / n, text=f"{stage_labels[stage]} — {i}/{n}")

        pipe = Pipeline.from_config("config/settings.yaml")
        result = pipe.run(tmp_path, stages=selected,
                          event_name=event_name, progress_cb=cb)
        progress.progress(1.0, text="Done")

        st.subheader("Output")

        # One-click run, but full transparency: what to trust, what to check.
        if "Needs_Review" in result.columns:
            flagged = result[result["Needs_Review"] == True]  # noqa: E712
            c1, c2, c3 = st.columns(3)
            c1.metric("Rows", len(result))
            c2.metric("Auto-verified", len(result) - len(flagged))
            c3.metric("Needs review", len(flagged))
            if len(flagged):
                with st.expander(f"⚠️ {len(flagged)} rows to double-check "
                                 "(low-confidence website, fuzzy company match, "
                                 "or no contact found)"):
                    st.dataframe(flagged, use_container_width=True)

        st.dataframe(result.head(50), use_container_width=True)

        buf = io.BytesIO()
        result.to_excel(buf, index=False)
        st.download_button("⬇️ Download Excel", buf.getvalue(),
                           file_name="leadwhop_output.xlsx")
        if "export" in selected:
            st.download_button("⬇️ Download CRM CSV",
                               result.to_csv(index=False).encode("utf-8-sig"),
                               file_name="crm_import.csv")
else:
    st.info("No file yet? Try the bundled demo: `data/demo_companies.xlsx`")
