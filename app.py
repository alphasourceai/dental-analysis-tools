import streamlit as st
import pandas as pd
import openai
from PIL import Image
import fitz
from pdf2image import convert_from_path
import pytesseract
import tempfile
import os
import sendgrid
from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition
import base64

# ---- Page Config ----
st.set_page_config(page_title="Dental P&L Analyzer", page_icon="🦷", layout="centered")

# ---- Styles ----
st.markdown("""
<style>
    .stApp, body {
        background-color: #252a34;
        color: #f0f0f0;
    }
    .main-container {
        background-color: transparent;
        color: #f0f0f0;
        max-width: 800px;
        margin: 0 auto;
        padding: 2rem;
    }
    .stButton>button {
        background-color: #1f77b4;
        color: #00cfc8 !important;
        border-radius: 4px;
        padding: 0.5rem 1rem;
        font-weight: bold;
    }
    .stButton>button:hover {
        background-color: #155a8a;
    }
    label, .stTextInput label, .stSelectbox label {
        color: #ffffff !important;
        font-weight: 500;
    }
</style>
""", unsafe_allow_html=True)

st.markdown("<div class='main-container'>", unsafe_allow_html=True)

# ---- API Keys ----
openai.api_key = st.secrets["OPENAI_API_KEY"]
SENDGRID_API_KEY = st.secrets["SENDGRID_API_KEY"]
TO_EMAIL = "info@alphasourceai.com"

# ---- Logo ----
logo = Image.open("logo.png")
st.image(logo, width=300)

# ---- Title and Subtitle ----
st.markdown("<h1 style='text-align: center;'>🦷 Dental Practice P&L Analyzer</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center;'>Upload your P&L report for high-level AI insights. We'll conduct a deeper analysis and reach out directly with the results.</p>", unsafe_allow_html=True)
st.divider()

# ---- User Info Form ----
with st.form("user_info_form"):
    first_name = st.text_input("First Name")
    last_name = st.text_input("Last Name")
    office_name = st.text_input("Office/Group Name")
    email = st.text_input("Email Address")
    org_type = st.selectbox("Type", ["Location", "Group"])
    uploaded_file = st.file_uploader("Upload your P&L file (Excel, CSV, or PDF)", type=["xlsx", "csv", "pdf"])
    submitted = st.form_submit_button("🔍 Analyze and Send")

# ---- PDF + OCR ----
def extract_text_from_pdf(uploaded_file):
    text = ""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
        tmp_file.write(uploaded_file.read())
        tmp_pdf_path = tmp_file.name
    try:
        with fitz.open(tmp_pdf_path) as doc:
            for page in doc:
                text += page.get_text()
        if not text.strip():
            images = convert_from_path(tmp_pdf_path)
            for image in images:
                text += pytesseract.image_to_string(image)
    finally:
        os.remove(tmp_pdf_path)
    return text.strip()

# ---- Send Email ----
def send_email(user_info, file, analysis_text):
    sg = sendgrid.SendGridAPIClient(api_key=SENDGRID_API_KEY)
    subject = f"New P&L Submission - {user_info['office_name']} ({user_info['email']})"
    body = f"""New P&L upload received:

First Name: {user_info['first_name']}
Last Name: {user_info['last_name']}
Office/Group: {user_info['office_name']}
Email: {user_info['email']}
Type: {user_info['org_type']}

Full AI Analysis:
{analysis_text}
"""
    message = Mail(
        from_email='noreply@alphasourceai.com',
        to_emails=TO_EMAIL,
        subject=subject,
        plain_text_content=body
    )
    if file:
        file_data = file.read()
        encoded = base64.b64encode(file_data).decode()
        attachment = Attachment(
            FileContent(encoded),
            FileName(file.name),
            FileType(file.type),
            Disposition("attachment")
        )
        message.attachment = attachment
    sg.send(message)

# ---- Submission ----
if submitted:
    if not all([first_name, last_name, office_name, email, org_type, uploaded_file]):
        st.warning("All fields and file upload are required.")
    else:
        with st.spinner("Analyzing..."):
            if uploaded_file.name.endswith(".pdf"):
                raw_text = extract_text_from_pdf(uploaded_file)
                data_input = raw_text
            else:
                df = pd.read_csv(uploaded_file) if uploaded_file.name.endswith(".csv") else pd.read_excel(uploaded_file)
                data_input = df.to_string(index=False)

            prompt = f"""
You are a dental consultant. Review the P&L data and provide:
- Summary of 3–5 key issues to improve profitability
- Number of issues showing decline
- Number of issues showing improvement
- Total number of improvement opportunities

End with a brief call-to-action encouraging the user to request a full review.

P&L Data:
{data_input}
"""
            response = openai.ChatCompletion.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
            full_analysis = response["choices"][0]["message"]["content"]

            # Generate summary only for user
            summary_prompt = f"""
From the following analysis, extract:
- Number of improvement opportunities
- Number of trends improving
- Number of trends declining

Use bullet points. Then add a short call-to-action for consulting:

{full_analysis}
"""
            summary_response = openai.ChatCompletion.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": summary_prompt}],
                temperature=0.2,
            )
            summary_output = summary_response["choices"][0]["message"]["content"]

            # Send email
            send_email(
                user_info={
                    "first_name": first_name,
                    "last_name": last_name,
                    "office_name": office_name,
                    "email": email,
                    "org_type": org_type,
                },
                file=uploaded_file,
                analysis_text=full_analysis
            )

        st.success("✅ AI Summary Generated")
        st.markdown(summary_output)

# ---- Footer ----
st.markdown("""<hr style="margin-top: 3rem;">""", unsafe_allow_html=True)
st.markdown("<p style='text-align: center;'>Built by <a href='https://alphasourceai.com' target='_blank'>AlphaSource AI</a></p>", unsafe_allow_html=True)
st.markdown("</div>", unsafe_allow_html=True)
