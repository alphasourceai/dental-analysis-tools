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
st.image(logo, use_column_width=False, width=200)

# ---- App Header ----
st.markdown(
    "<h1 style='text-align: center; color: #1f77b4;'>🦷 Dental Practice P&L Analyzer</h1>",
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

