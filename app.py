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

# ---- API Keys ----
openai.api_key = st.secrets["OPENAI_API_KEY"]
SENDGRID_API_KEY = st.secrets["SENDGRID_API_KEY"]
TO_EMAIL = "info@alphasourceai.com"

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

def send_followup_email(user_info, tool_name):
    sg = sendgrid.SendGridAPIClient(api_key=SENDGRID_API_KEY)
    
    # Dynamic email content
    if tool_name == "P&L Analyzer":
        improvement_type = "profitability"
    elif tool_name == "AR Analyzer":
        improvement_type = "streamline workflows"
    elif tool_name == "Insurance Claim Analyzer":
        improvement_type = "reduce claims risk"
    elif tool_name == "SOP Analyzer":
        improvement_type = "improve operational consistency"
    
    subject = f"Your Dental Analysis Summary – Let’s Talk Strategy"
    body = f"""
    Hi {user_info['first_name']},
    
    Thank you for submitting your dental operations file to our analysis platform.
    
    Here’s a quick summary of your submission:
    - Tool Used: {tool_name}
    - Office/Group: {user_info['office_name']}
    
    We’ve reviewed the data and identified key areas where improvements can drive {improvement_type}.
    
    📞 If you'd like a personalized review of this analysis or want to explore how we can help optimize your practice, reply to this email or book a time here: [Schedule with Us](https://outlook.office365.com/owa/calendar/alphaSourceBookingPage@alphasourceai.com/bookings/)
    
    We look forward to helping you grow.
    
    – Jason  
    alphaSource AI  
    info@alphasourceai.com
    """
    
    message = Mail(
        from_email="noreply@alphasourceai.com",
        to_emails=user_info['email'],
        subject=subject,
        plain_text_content=body
    )
    
    sg.send(message)

def send_email(user_info, file, analysis_text, tool_name):
    sg = sendgrid.SendGridAPIClient(api_key=SENDGRID_API_KEY)
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

{analysis_text}
"""
    message = Mail(
        from_email="noreply@alphasourceai.com",
        to_emails=TO_EMAIL,
        subject=subject,
        plain_text_content=body
    )
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

def analyze_and_send(file, user_info, prompt, summary_prompt, tool_name):
    with st.spinner("Analyzing..."):
        if file.name.endswith(".pdf"):
            raw_text = extract_text_from_pdf(file)
            data_input = raw_text
        else:
            df = pd.read_csv(file) if file.name.endswith(".csv") else pd.read_excel(file)
            data_input = df.to_string(index=False)

        # Full analysis
        response = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt.replace("{data}", data_input)}],
            temperature=0.3,
        )
        full_analysis = response["choices"][0]["message"]["content"]

        # Summary
        summary_response = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": summary_prompt.replace("{analysis}", full_analysis)}],
            temperature=0.2,
        )
        summary_output = summary_response["choices"][0]["message"]["content"]

        send_email(user_info, file, full_analysis, tool_name)
        send_followup_email(user_info, tool_name)

        st.success("✅ Summary Ready")
        st.markdown(summary_output)

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
            prompt="""You are a dental consultant. Review the P&L data below and provide:
- 3–5 key issues affecting profitability
- Number of issues showing decline
- Number of issues showing improvement
- Total improvement opportunities
End with a call-to-action for full consulting review.

{data}""",
            summary_prompt="""From the analysis below, extract:
- Number of improvement opportunities
- Number of trends improving
- Number of trends declining

Use bullet points. End with a short consulting pitch.

{analysis}""",
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
            prompt="""You're a dental RCM specialist. Review the AR report below and identify aging concerns, risk areas, and collection opportunities.

{data}""",
            summary_prompt="""Summarize the AR insights below into key risks and opportunities.

{analysis}""",
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
            prompt="""You are a dental insurance audit expert. Review the claim data below. Identify denials, delays, and appeal opportunities.

{data}""",
            summary_prompt="""Summarize the claims analysis into denial trends and improvement areas.

{analysis}""",
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
            prompt="""You are a dental operations consultant. Review the SOP below. Identify gaps, redundancies, and suggestions for operational efficiency.

{data}""",
            summary_prompt="""Summarize the main gaps and suggested improvements from this SOP analysis.

{analysis}""",
            tool_name="SOP Analyzer"
        )

# ---- Footer ----
st.markdown("""<hr style="margin-top: 3rem;">""", unsafe_allow_html=True)
st.markdown("<p style='text-align: center;'>Built by <a href='https://alphasourceai.com' target='_blank'>AlphaSource AI</a></p>", unsafe_allow_html=True)
