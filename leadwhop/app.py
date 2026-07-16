"""LeadWhop — Streamlit UI.

Deploy: push to GitHub → connect on share.streamlit.io → add secrets.
Local:  streamlit run app.py
"""
import io
import os
import tempfile

import pandas as pd
import streamlit as st

from leadwhop.pipeline import Pipeline, STAGES

# ── Streamlit Cloud Secrets → env vars ──────────────────────────────────────
for _key in ("OPENAI_API_KEY", "SERPER_API_KEY", "LUSHA_API_KEY"):
    if not os.environ.get(_key):
        try:
            val = st.secrets.get(_key)
            if val:
                os.environ[_key] = val
        except Exception:
            pass

# ── Password gate ────────────────────────────────────────────────────────────
_APP_PASSWORD = os.environ.get("APP_PASSWORD") or (
    st.secrets.get("APP_PASSWORD") if hasattr(st, "secrets") else None
) or "WhoIsJohnGalt"

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    st.markdown("# 🎯 LeadWhop")
    st.markdown("Enter the password to continue.")
    pwd = st.text_input("Password", type="password", key="pwd_input")
    if st.button("Enter", type="primary"):
        if pwd == _APP_PASSWORD:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Wrong password. Who is John Galt?")
    st.stop()

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="LeadWhop",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ───────────────────────────────────────────────────────────────
st.markdown("""
<style>
[data-testid="stSidebar"] { background-color: #0a2f38; }
.block-container { padding-top: 2rem; }
.metric-card {
    background: #124E5B;
    border-radius: 12px;
    padding: 1.2rem 1.5rem;
    text-align: center;
}
.metric-card .value { font-size: 2.2rem; font-weight: 800; color: #F2B33D; }
.metric-card .label { font-size: 0.85rem; color: #9CC3CB; margin-top: 0.2rem; }
.stage-badge {
    display: inline-block;
    background: #1E6674;
    color: #F2B33D;
    border-radius: 20px;
    padding: 0.2rem 0.8rem;
    font-size: 0.8rem;
    font-weight: 700;
    margin-bottom: 0.3rem;
}
</style>
""", unsafe_allow_html=True)

# ── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.divider()
    # API keys — only shown when not already injected from Secrets
    missing = [k for k in ("OPENAI_API_KEY", "SERPER_API_KEY", "LUSHA_API_KEY")
               if not os.environ.get(k)]
    if missing:
        st.markdown("### 🔑 API Keys")
        st.caption("Keys stay in this session only — never written to disk.")
        label_map = {"OPENAI_API_KEY": "OpenAI",
                     "SERPER_API_KEY": "Serper.dev",
                     "LUSHA_API_KEY":  "Lusha"}
        for key in missing:
            val = st.text_input(label_map[key], type="password", key=key)
            if val:
                os.environ[key] = val
        st.divider()

    st.markdown("### ⚙️ Pipeline stages")
    stage_meta = {
        "websites":  ("1", "Find websites",        True),
        "qualify":   ("2", "Check product fit",     True),
        "contacts":  ("3", "Find buyers",           False),
        "phones":    ("4", "Enrich phones",         False),
        "export":    ("5", "Export to CRM",         False),
    }
    selected = []
    for stage, (num, label, default) in stage_meta.items():
        if st.checkbox(f"**{num}** — {label}", value=default, key=f"stage_{stage}"):
            selected.append(stage)

    event_name = ""
    if "export" in selected:
        st.divider()
        event_name = st.text_input("📅 Event name", placeholder="e.g. PLMA 2026")

# ── Main ─────────────────────────────────────────────────────────────────────
col_title, col_sub = st.columns([3, 2])
with col_title:
    st.markdown("# 🎯 LeadWhop")
    st.markdown("**Company list in → CRM-ready qualified leads out.**  \n"
                "Upload your Excel, pick your stages, hit Run.")

st.divider()

uploaded = st.file_uploader(
    "📂 Upload company list (.xlsx)",
    type=["xlsx"],
    help="Required column: **Company**. Optional: Country, Website.",
)

if uploaded:
    preview = pd.read_excel(uploaded)
    st.markdown(f"**{len(preview)} companies loaded** — preview:")
    st.dataframe(preview.head(8), use_container_width=True, height=220)

    # Validation
    missing_keys = []
    if not os.environ.get("OPENAI_API_KEY"):  missing_keys.append("OpenAI")
    if not os.environ.get("SERPER_API_KEY"):  missing_keys.append("Serper")
    if ("contacts" in selected or "phones" in selected) and not os.environ.get("LUSHA_API_KEY"):
        missing_keys.append("Lusha")

    if missing_keys:
        st.warning(f"⚠️ Add missing API keys in the sidebar: {', '.join(missing_keys)}")
    elif not selected:
        st.info("Select at least one stage in the sidebar.")
    else:
        st.divider()
        run_col, _ = st.columns([1, 3])
        with run_col:
            run = st.button("▶️ Run pipeline", type="primary", use_container_width=True)

        if run:
            with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                tmp.write(uploaded.getvalue())
                tmp_path = tmp.name

            status   = st.empty()
            progress = st.progress(0.0)

            def cb(stage, i, n):
                _, label, _ = stage_meta[stage]
                progress.progress(i / n, text=f"**{label}** — {i} / {n}")

            try:
                pipe   = Pipeline.from_config("config/settings.yaml")
                result = pipe.run(tmp_path, stages=selected,
                                  event_name=event_name, progress_cb=cb)
                progress.progress(1.0, text="✅ Done")
                status.success("Pipeline complete!")
            except Exception as exc:
                st.error(f"Pipeline error: {exc}")
                st.stop()

            st.divider()

            # ── Summary metrics ──────────────────────────────────────────
            total    = len(result)
            reviewed = int(result["Needs_Review"].sum()) if "Needs_Review" in result.columns else 0
            verified = total - reviewed
            with_email = int(result["Email"].notna().sum() & (result["Email"] != "")) \
                if "Email" in result.columns else "-"

            m1, m2, m3, m4 = st.columns(4)
            m1.markdown(f'<div class="metric-card"><div class="value">{total}</div>'
                        f'<div class="label">Total rows</div></div>', unsafe_allow_html=True)
            m2.markdown(f'<div class="metric-card"><div class="value">{verified}</div>'
                        f'<div class="label">Auto-verified</div></div>', unsafe_allow_html=True)
            m3.markdown(f'<div class="metric-card"><div class="value">{reviewed}</div>'
                        f'<div class="label">Needs review</div></div>', unsafe_allow_html=True)
            m4.markdown(f'<div class="metric-card"><div class="value">{with_email}</div>'
                        f'<div class="label">Emails found</div></div>', unsafe_allow_html=True)

            st.markdown("###")

            # ── Review expander ──────────────────────────────────────────
            if reviewed and "Needs_Review" in result.columns:
                flagged = result[result["Needs_Review"] == True]  # noqa
                with st.expander(f"⚠️ {reviewed} rows to double-check"):
                    st.dataframe(flagged, use_container_width=True)

            # ── Full results ─────────────────────────────────────────────
            st.markdown("### Results")
            st.dataframe(result, use_container_width=True, height=350)

            # ── Downloads ────────────────────────────────────────────────
            st.divider()
            dl1, dl2 = st.columns(2)
            buf = io.BytesIO()
            result.to_excel(buf, index=False)
            dl1.download_button(
                "⬇️ Download Excel",
                buf.getvalue(),
                file_name="leadwhop_output.xlsx",
                use_container_width=True,
            )
            if "export" in selected:
                dl2.download_button(
                    "⬇️ Download CRM CSV",
                    result.to_csv(index=False).encode("utf-8-sig"),
                    file_name="leadwhop_crm_import.csv",
                    use_container_width=True,
                )

else:
    # Empty state
    st.markdown("### 👋 Getting started")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**1. Add API keys**  \nIn the sidebar — OpenAI + Serper "
                    "for stages 1-2. Lusha only for contacts.")
    with c2:
        st.markdown("**2. Upload your list**  \nAn Excel file with a Company column. "
                    "Country and Website are optional but improve results.")
    with c3:
        st.markdown("**3. Pick stages & run**  \nSelect which steps to run. "
                    "Start with stages 1-2 to test without spending Lusha credits.")
    st.info("No file yet? Try `data/demo_companies.xlsx` from the repo.")
