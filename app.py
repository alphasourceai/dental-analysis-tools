import streamlit as st
import pandas as pd
import openai
import requests
from PIL import Image
import fitz
from pdf2image import convert_from_path
import pytesseract
import tempfile
import os
import sendgrid
from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition
import base64

# ---- API Keys ----
openai.api_key = st.secrets["OPENAI_API_KEY"]
XAI_API_KEY = st.secrets["XAI_API_KEY"]  # Replace with actual API key for xAI
ANTHROPIC_API_KEY = st.secrets["ANTHROPIC_API_KEY"]  # Replace with actual API key for AnthropicAI

# ---- Page Config ----
st.set_page_config(page_title="Dental Tools", page_icon="🦷", layout="centered")

# ---- Style ----
st.markdown("""
<style>
    .stApp {
        background-color: #252a34;
        color: #f0f0f0;
    }
    label, .stTextInput label, .stSelectbox label {
        color: #ffffff !important;
        font-weight: 500;
    }
    .stButton > button, .stForm button {
        color: #00cfc8 !important;
        background-color: #1f77b4;
        font-weight: bold;
        border-radius: 5px;
        padding: 0.5rem 1rem;
    }
    .stButton > button:hover, .stForm button:hover {
        background-color: #155a8a;
    }
    .stAlert {
        background-color: #39414f !important;
        border-left: 0.5rem solid #00cfc8 !important;
        color: white !important;
        padding: 0.5rem 1rem;
        border-radius: 4px;
    }
</style>
""", unsafe_allow_html=True)

# ---- Logo ----
logo = Image.open("logo.png")
st.image(logo, use_container_width=True)

# ---- Page Title ----
st.markdown("<h1 style='text-align: center;'>Dental Operations Analysis Tools</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center;'>Upload files for each section below. Your insights will be reviewed and sent to our team for deeper analysis.</p>", unsafe_allow_html=True)
st.divider()

# ---- Global User Info Form ----
with st.form("user_info_form"):
    st.subheader("📇 User Information")
    first_name = st.text_input("First Name")
    last_name = st.text_input("Last Name")
    office_name = st.text_input("Office/Group Name")
    email = st.text_input("Email Address")
    org_type = st.selectbox("Type", ["Location", "Group"])
    submit_user_info = st.form_submit_button("Save Info")

user_info_complete = all([first_name, last_name, office_name, email, org_type])

# ---- Helper Functions ----
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

# ---- API Analysis Functions ----
# OpenAI Analysis
def openai_analysis(data_input):
    """Run the data through OpenAI's chat completion API."""
    response = openai.ChatCompletion.create(
        model="gpt-4o",  # Adjust if needed
        messages=[{"role": "user", "content": data_input}],
        temperature=0.3,
        max_tokens=500,
    )
    return response["choices"][0]["message"]["content"]

# xAI Analysis
def xai_analysis(data_input):
    url = "https://api.xai.com/analyze"  # Replace with actual xAI endpoint
    headers = {"Authorization": f"Bearer {XAI_API_KEY}"}
    payload = {"data": data_input}
    
    response = requests.post(url, json=payload, headers=headers)
    return response.json()["analysis"]

# AnthropicAI Analysis
def anthropic_analysis(data_input):
    url = "https://api.anthropic.com/v1/complete"  # Replace with actual AnthropicAI endpoint
    headers = {"Authorization": f"Bearer {ANTHROPIC_API_KEY}"}
    payload = {
        "input": data_input,
        "model": "anthropic-1.0",  # Adjust model if needed
    }
    
    response = requests.post(url, json=payload, headers=headers)
    return response.json()["result"]

# ---- Combine Results ----
def analyze_with_all_models(data_input):
    openai_result = openai_analysis(data_input)
    xai_result = xai_analysis(data_input)
    anthropic_result = anthropic_analysis(data_input)
    
    # Combine results into a dictionary
    combined_results = {
        "OpenAI Analysis": openai_result,
        "xAI Analysis": xai_result,
        "AnthropicAI Analysis": anthropic_result,
    }
    
    return combined_results

# ---- Send Follow-Up Email to User ----
def send_followup_email(user_info, tool_name, results):
    sg = sendgrid.SendGridAPIClient(api_key=st.secrets["SENDGRID_API_KEY"])
    
    # Dynamic email content
    subject = f"Your Dental Analysis Summary – Let’s Talk Strategy"
    body = f"""
    Hi {user_info['first_name']},
    
    Thank you for submitting your dental operations file to our analysis platform.
    
    Here’s a quick summary of your submission:
    - Tool Used: {tool_name}
    - Office/Group: {user_info['office_name']}
    
    We’ve reviewed the data and identified key areas where improvements can drive profitability, streamline workflows, or reduce claims risk.
    
    📞 If you'd like a personalized review of this analysis or want to explore how we can help optimize your practice, reply to this email or book a time here: [Schedule with Us](https://outlook.office365.com/owa/calendar/alphaSourceBookingPage@alphasourceai.com/bookings/)
    
    We look forward to helping you grow.
    
    – Jason  
    alphaSource AI  
    info@alphasourceai.com
    """
    
    sg.send(Mail(
        from_email="noreply@alphasourceai.com",
        to_emails=user_info['email'],
        subject=subject,
        plain_text_content=body
    ))

# ---- Send Full Analysis Email to Info ----
def send_email(user_info, file, results, tool_name):
    sg = sendgrid.SendGridAPIClient(api_key=st.secrets["SENDGRID_API_KEY"])
    
    subject = f"[{tool_name}] {user_info['office_name']} ({user_info['email']})"
    body = f"""New file submitted for analysis.

Tool: {tool_name}
File Name: {file.name}
File Type: {file.type}

Submitted by:
First Name: {user_info['first_name']}
Last Name: {user_info['last_name']}
Office/Group: {user_info['office_name']}
Email: {user_info['email']}
Type: {user_info['org_type']}

--- AI Analysis ---

OpenAI Analysis:
{results["OpenAI Analysis"]}

xAI Analysis:
{results["xAI Analysis"]}

AnthropicAI Analysis:
{results["AnthropicAI Analysis"]}
"""
    
    # Create a unified report file (text file)
    with open("/tmp/unified_analysis.txt", "w") as f:
        f.write(f"Analysis Results for {user_info['office_name']}\n")
        f.write(f"\nTool: {tool_name}\n")
        for model, analysis in results.items():
            f.write(f"\n{model}:\n{analysis}\n")
    
    # Attach the unified report as a text file
    with open("/tmp/unified_analysis.txt", "rb") as f:
        file_data = f.read()
        encoded = base64.b64encode(file_data).decode()
        attachment = Attachment(
            FileContent(encoded),
            FileName("unified_analysis.txt"),
            FileType("text/plain"),
            Disposition("attachment")
        )
    
    message = Mail(
        from_email="noreply@alphasourceai.com",
        to_emails="info@alphasourceai.com",  # Your email
        subject=subject,
        plain_text_content=body
    )
    message.add_attachment(attachment)
    sg.send(message)

# ---- Tool Analysis Sections ----
def analyze_and_send(file, user_info, tool_name):
    with st.spinner("Analyzing..."):
        if file.name.endswith(".pdf"):
            raw_text = extract_text_from_pdf(file)
            data_input = raw_text
        else:
            df = pd.read_csv(file) if file.name.endswith(".csv") else pd.read_excel(file)
            data_input = df.to_string(index=False)
        
        # Run the analysis with all models
        results = analyze_with_all_models(data_input)
        
        # Send the email with individual analyses and attach the unified report
        send_followup_email(user_info, tool_name, results)
        send_email(user_info, file, results, tool_name)
        
        st.success("✅ Analysis Complete! The results have been emailed.")
        st.markdown("### Analysis Results:")
        st.markdown(f"**OpenAI Analysis**: {results['OpenAI Analysis']}")
        st.markdown(f"**xAI Analysis**: {results['xAI Analysis']}")
        st.markdown(f"**AnthropicAI Analysis**: {results['AnthropicAI Analysis']}")

# ---- Tool: P&L Analyzer ----
st.subheader("📊 P&L Analyzer")

if not user_info_complete:
    st.markdown("<div class='stAlert'>⚠️ Please complete the user info form above before uploading.</div>", unsafe_allow_html=True)
else:
    pnl_file = st.file_uploader("Upload your P&L file (Excel, CSV, or PDF)", type=["xlsx", "csv", "pdf"], key="pnl")
    if pnl_file and st.button("🔍 Analyze P&L"):
        analyze_and_send(
            file=pnl_file,
            user_info={
                "first_name": first_name,
                "last_name": last_name,
                "office_name": office_name,
                "email": email,
                "org_type": org_type,
            },
            tool_name="P&L Analyzer"
        )

# ---- Tool: AR Analyzer ----
st.subheader("💰 Accounts Receivable Analyzer")

if not user_info_complete:
    st.markdown("<div class='stAlert'>⚠️ Please complete the user info form above before uploading.</div>", unsafe_allow_html=True)
else:
    ar_file = st.file_uploader("Upload AR Report (CSV or Excel)", type=["csv", "xlsx"], key="ar")
    if ar_file and st.button("🔍 Analyze AR"):
        analyze_and_send(
            file=ar_file,
            user_info={
                "first_name": first_name,
                "last_name": last_name,
                "office_name": office_name,
                "email": email,
                "org_type": org_type,
            },
            tool_name="AR Analyzer"
        )

# ---- Tool: Insurance Claim Analyzer ----
st.subheader("📄 Insurance Claim Analyzer")

if not user_info_complete:
    st.markdown("<div class='stAlert'>⚠️ Please complete the user info form above before uploading.</div>", unsafe_allow_html=True)
else:
    claim_file = st.file_uploader("Upload Claim Report (CSV, Excel, or PDF)", type=["csv", "xlsx", "pdf"], key="claim")
    if claim_file and st.button("🔍 Analyze Claims"):
        analyze_and_send(
            file=claim_file,
            user_info={
                "first_name": first_name,
                "last_name": last_name,
                "office_name": office_name,
                "email": email,
                "org_type": org_type,
            },
            tool_name="Insurance Claim Analyzer"
        )

# ---- Tool: SOP Analyzer ----
st.subheader("📝 SOP Analyzer")

if not user_info_complete:
    st.markdown("<div class='stAlert'>⚠️ Please complete the user info form above before uploading.</div>", unsafe_allow_html=True)
else:
    sop_file = st.file_uploader("Upload SOP Document (PDF)", type=["pdf"], key="sop")
    if sop_file and st.button("🔍 Analyze SOPs"):
        analyze_and_send(
            file=sop_file,
            user_info={
                "first_name": first_name,
                "last_name": last_name,
                "office_name": office_name,
                "email": email,
                "org_type": org_type,
            },
            tool_name="SOP Analyzer"
        )

# ---- Footer ----
st.markdown("""<hr style="margin-top: 3rem;">""", unsafe_allow_html=True)
st.markdown("<p style='text-align: center;'>Built by <a href='https://alphasourceai.com' target='_blank'>AlphaSource AI</a></p>", unsafe_allow_html=True)
