import streamlit as st
import pandas as pd
import openai
from PIL import Image

# Set up OpenAI
openai.api_key = st.secrets["OPENAI_API_KEY"]

# ---- Page Configuration ----
st.set_page_config(
    page_title="Dental P&L Analyzer",
    page_icon="🦷",
    layout="centered"
)

# ---- Load and Display Logo ----
logo = Image.open("logo.png")
st.image(logo, use_container_width=True)

# ---- App Header ----
# ---- Custom Dark Theme Styling ----
st.markdown(
    """
    <style>
        body {
            background-color: #252a34;
            color: #ffffff;
        }
        .stApp {
            background-color: #252a34;
        }
        h1, h2, h3, h4, h5, h6, p, label, .stMarkdown {
            color: #ffffff !important;
        }
        .stButton>button {
            background-color: #08d9d6;
            color: black;
            border-radius: 6px;
            padding: 0.5rem 1rem;
            border: none;
        }
        .stButton>button:hover {
            background-color: #00adb5;
            color: white;
        }
        .stDownloadButton>button {
            background-color: #ff2e63;
            color: white;
            border-radius: 6px;
            padding: 0.5rem 1rem;
            border: none;
        }
        .stDownloadButton>button:hover {
            background-color: #e3004f;
        }
        .css-1kyxreq, .css-1r6slb0 {
            background-color: #393e46 !important;
        }
    </style>
    """,
    unsafe_allow_html=True
)

st.markdown(
    "<p style='text-align: center; font-size: 1.1rem;'>Upload your P&L report to receive expert AI-driven analysis.</p>",
    unsafe_allow_html=True
)

st.divider()

# ---- File Upload ----
uploaded_file = st.file_uploader("Upload your P&L file (Excel or CSV)", type=["xlsx", "csv"])

# ---- Process File ----
if uploaded_file:
    if uploaded_file.name.endswith('.csv'):
        df = pd.read_csv(uploaded_file)
    else:
        df = pd.read_excel(uploaded_file)

    st.subheader("📊 P&L File Preview")
    st.dataframe(df, use_container_width=True)

    # ---- Run Analysis ----
    if st.button("🔍 Run AI Analysis"):
        with st.spinner("Analyzing..."):
            data_str = df.to_string(index=False)

            prompt = f"""
You are a dental operations consultant. Review this P&L and provide:
- Key observations and trends
- Any cost categories that appear too high
- Suggestions for improving profitability
- Benchmarks to compare against industry norms (if possible)

P&L Data:
{data_str}
"""

            response = openai.ChatCompletion.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )

            insights = response["choices"][0]["message"]["content"]

        st.subheader("📈 AI-Powered Insights")
        st.markdown(insights)

        st.download_button(
            label="📥 Download Insights",
            data=insights,
            file_name="pnl_analysis.txt",
            mime="text/plain"
        )

else:
    st.info("Please upload a P&L file to begin.")

# ---- Footer ----
st.markdown("""<hr style="margin-top: 3rem;">""", unsafe_allow_html=True)
st.markdown(
    "<p style='text-align: center; font-size: 0.9rem;'>Built by <a href='https://alphasourceai.com' target='_blank'>AlphaSource AI</a></p>",
    unsafe_allow_html=True
)

