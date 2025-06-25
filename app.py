import streamlit as st
import pandas as pd
import openai
import os
from dotenv import load_dotenv

load_dotenv()
import streamlit as st
openai.api_key = st.secrets["OPENAI_API_KEY"]

st.set_page_config(page_title="Dental P&L Analyzer")
st.title("🦷 Dental Practice P&L Analyzer")

uploaded_file = st.file_uploader("Upload your P&L file (Excel or CSV)", type=["xlsx", "csv"])

if uploaded_file:
    # Load file into dataframe
    if uploaded_file.name.endswith('.csv'):
        df = pd.read_csv(uploaded_file)
    else:
        df = pd.read_excel(uploaded_file)

    st.write("📊 Here's a preview of your data:")
    st.dataframe(df)

    if st.button("Analyze with AI"):
        st.write("Generating insights...")

        # Create a string summary of the file
        file_summary = df.to_string(index=False)

        # Send to OpenAI for analysis
        prompt = f"""
You are a dental operations expert. Analyze the following Profit & Loss statement and provide:
- Key observations
- Any red flags (e.g. high supply or lab costs)
- Suggestions for improvement
- Comparison to typical industry benchmarks if possible

P&L Data:
{file_summary}
        """

        response = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )

        insights = response["choices"][0]["message"]["content"]
        st.markdown("### 📈 AI-Powered Analysis:")
        st.write(insights)

