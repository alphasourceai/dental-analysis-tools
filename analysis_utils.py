"""
Shared analysis utilities for dental operations analysis.
Used by both app.py (public page) and admin_dashboard.py (admin page).
"""

import os
import tempfile
import base64
import textwrap
import sendgrid
from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition, TrackingSettings, ClickTracking
from PIL import Image
from io import BytesIO
import pymupdf as fitz
import pytesseract


def extract_text_from_pdf(uploaded_file):
    """Extract text from PDF file, using OCR for image-based pages"""
    text = ""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
        tmp_file.write(uploaded_file.read())
        tmp_pdf_path = tmp_file.name
    try:
        with fitz.open(tmp_pdf_path) as doc:
            for page in doc:
                page_text = page.get_text()
                
                if not page_text.strip():
                    image_list = page.get_images()
                    for img_index, img in enumerate(image_list):
                        xref = img[0]
                        base_image = doc.extract_image(xref)
                        image_bytes = base_image["image"]
                        image = Image.open(BytesIO(image_bytes))
                        page_text += pytesseract.image_to_string(image)
                
                text += page_text
                if page_text.strip():
                    text += "\n"
    finally:
        os.remove(tmp_pdf_path)
    return text.strip()


def get_analysis_prompt(doc_type="general"):
    """Generate a detailed prompt based on document type"""
    base_prompt = """You are an expert dental operations consultant with deep knowledge of practice management, revenue cycle, and operational efficiency.

IMPORTANT FORMATTING RULES:
- Use PLAIN TEXT only - no LaTeX, no math formatting, no special markup
- Write dollar amounts as plain text: $10,000 not $10,000$ or \$10,000
- Do not use asterisks for emphasis or formatting
- Keep all text on single lines without special characters

Analyze the provided data and identify improvement opportunities AND key trends.

SECTION 1 - IMPROVEMENT OPPORTUNITIES:
Identify AT LEAST 3-5 high-level strategic areas for improvement. Format each as:
ISSUE: [Brief, strategic title - keep it high-level, not overly specific]
IMPACT: [Why this matters - financial, operational, or compliance impact]
RECOMMENDATION: [General recommended action]

Focus on strategic opportunities in:
- Revenue cycle optimization
- Operational efficiency
- Cost management
- Patient experience
- Compliance and risk management
- Technology utilization
- Staff productivity

SECTION 2 - KEY TRENDS:
After the improvement opportunities, add a separator line "---TRENDS---" and then identify 3-5 quantitative trends from the data. Format each as:
TREND: [Specific trend with numbers/percentages/timeframes]

Examples of trends to look for:
- Cost increases/decreases over time (e.g., "Dental supplies increased 5% over past 90 days")
- Payment timing changes (e.g., "Average days to payment extended from 38 to 44 days over last 12 months")
- Volume trends (e.g., "Patient visits declined 12% quarter-over-quarter")
- Revenue patterns (e.g., "Collections rate dropped from 94% to 89% in Q3")
- Expense patterns (e.g., "Lab costs up 15% year-over-year")

Be specific with numbers, percentages, and timeframes when identifying trends."""
    
    return base_prompt


def openai_analysis(data_input, doc_type="general"):
    """Run analysis using OpenAI GPT-4"""
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    
    prompt = get_analysis_prompt(doc_type)
    
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Analyze this dental practice data:\n\n{data_input[:6000]}"}
        ],
        temperature=0.3,
        max_tokens=1500
    )
    return response.choices[0].message.content


def xai_analysis(data_input, doc_type="general"):
    """Use xAI's Grok model for analysis"""
    from openai import OpenAI
    
    client = OpenAI(
        api_key=os.getenv("XAI_API_KEY"),
        base_url="https://api.x.ai/v1"
    )
    
    prompt = get_analysis_prompt(doc_type)
    
    response = client.chat.completions.create(
        model="grok-4-1-fast-reasoning",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Analyze this dental practice data:\n\n{data_input[:6000]}"}
        ],
        temperature=0.3,
        max_tokens=1500
    )
    return response.choices[0].message.content


def anthropic_analysis(data_input, doc_type="general"):
    """Use Anthropic's Claude model for analysis"""
    import anthropic
    
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    
    prompt = get_analysis_prompt(doc_type)
    
    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1500,
        temperature=0.3,
        system=prompt,
        messages=[
            {"role": "user", "content": f"Analyze this dental practice data:\n\n{data_input[:6000]}"}
        ]
    )
    return message.content[0].text


def parse_issues_from_analysis(analysis_text, source_model):
    """Extract individual issues from AI analysis text"""
    issues = []
    
    if '---TRENDS---' in analysis_text:
        improvements_section = analysis_text.split('---TRENDS---')[0]
    else:
        improvements_section = analysis_text
    
    lines = improvements_section.strip().split('\n')
    current_issue = {}
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        if line.upper().startswith('ISSUE:') or (len(line) > 0 and line[0].isdigit() and '.' in line[:3]):
            if current_issue:
                issues.append(current_issue)
            
            issue_title = line.split(':', 1)[-1].strip() if ':' in line else line.split('.', 1)[-1].strip()
            current_issue = {
                'title': issue_title,
                'impact': '',
                'recommendation': '',
                'source': source_model,
                'full_text': line
            }
        elif line.upper().startswith('IMPACT:'):
            if current_issue:
                current_issue['impact'] = line.split(':', 1)[-1].strip()
                current_issue['full_text'] += '\n' + line
        elif line.upper().startswith('RECOMMENDATION:'):
            if current_issue:
                current_issue['recommendation'] = line.split(':', 1)[-1].strip()
                current_issue['full_text'] += '\n' + line
        elif current_issue:
            current_issue['full_text'] += '\n' + line
    
    if current_issue:
        issues.append(current_issue)
    
    return issues


def parse_trends_from_analysis(analysis_text, source_model):
    """Extract trends from AI analysis text"""
    trends = []
    
    if '---TRENDS---' not in analysis_text:
        return trends
    
    trends_section = analysis_text.split('---TRENDS---')[1]
    lines = trends_section.strip().split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        if 'TREND:' in line.upper():
            trend_text = line.split(':', 1)[-1].strip() if ':' in line else line
            if len(trend_text) > 0 and trend_text[0].isdigit():
                trend_text = trend_text.split('.', 1)[-1].strip()
            
            if trend_text:
                trends.append({
                    'text': trend_text,
                    'source': source_model
                })
    
    return trends


def deduplicate_issues(all_issues):
    """Deduplicate similar issues across models using simple text similarity"""
    from difflib import SequenceMatcher
    
    def similar(a, b):
        """Check if two strings are similar (>70% match)"""
        return SequenceMatcher(None, a.lower(), b.lower()).ratio() > 0.7
    
    deduplicated = []
    used_indices = set()
    
    for i, issue in enumerate(all_issues):
        if i in used_indices:
            continue
        
        similar_issues = [issue]
        sources = [issue['source']]
        
        for j, other_issue in enumerate(all_issues[i+1:], start=i+1):
            if j in used_indices:
                continue
            
            if similar(issue['title'], other_issue['title']):
                similar_issues.append(other_issue)
                sources.append(other_issue['source'])
                used_indices.add(j)
        
        dedup_issue = {
            'title': issue['title'],
            'impact': issue['impact'],
            'recommendation': issue['recommendation'],
            'sources': sources,
            'count': len(sources),
            'all_versions': similar_issues
        }
        deduplicated.append(dedup_issue)
        used_indices.add(i)
    
    return deduplicated


def analyze_with_all_models(data_input):
    """Run analysis with all 3 models and return both raw and processed results"""
    openai_result = openai_analysis(data_input)
    xai_result = xai_analysis(data_input)
    anthropic_result = anthropic_analysis(data_input)
    
    openai_issues = parse_issues_from_analysis(openai_result, "OpenAI GPT-4")
    xai_issues = parse_issues_from_analysis(xai_result, "xAI Grok")
    anthropic_issues = parse_issues_from_analysis(anthropic_result, "Anthropic Claude")
    
    openai_trends = parse_trends_from_analysis(openai_result, "OpenAI GPT-4")
    xai_trends = parse_trends_from_analysis(xai_result, "xAI Grok")
    anthropic_trends = parse_trends_from_analysis(anthropic_result, "Anthropic Claude")
    
    all_issues = openai_issues + xai_issues + anthropic_issues
    all_trends = openai_trends + xai_trends + anthropic_trends
    
    deduplicated_issues = deduplicate_issues(all_issues)
    
    return {
        "raw_analyses": {
            "OpenAI Analysis": openai_result,
            "xAI Analysis": xai_result,
            "AnthropicAI Analysis": anthropic_result,
        },
        "parsed_issues": {
            "openai": openai_issues,
            "xai": xai_issues,
            "anthropic": anthropic_issues,
        },
        "parsed_trends": {
            "openai": openai_trends,
            "xai": xai_trends,
            "anthropic": anthropic_trends,
        },
        "all_trends": all_trends,
        "deduplicated_issues": deduplicated_issues,
        "total_issue_count": len(deduplicated_issues)
    }


def categorize_issue(title):
    """Categorize an issue title into a strategic category"""
    if any(keyword in title.lower() for keyword in ['revenue', 'collection', 'payment', 'billing', 'ar', 'receivable']):
        return "Revenue Cycle Optimization"
    elif any(keyword in title.lower() for keyword in ['cost', 'expense', 'supply', 'overhead', 'lab']):
        return "Cost Management Opportunities"
    elif any(keyword in title.lower() for keyword in ['claim', 'insurance', 'denial', 'reimbursement']):
        return "Claims Process Enhancement"
    elif any(keyword in title.lower() for keyword in ['staff', 'team', 'productivity', 'efficiency', 'workflow']):
        return "Operational Efficiency Gains"
    elif any(keyword in title.lower() for keyword in ['patient', 'schedule', 'appointment', 'experience']):
        return "Patient Experience Improvement"
    elif any(keyword in title.lower() for keyword in ['technology', 'software', 'system', 'automation']):
        return "Technology & Automation"
    else:
        return "Strategic Growth Opportunities"


def sanitize_streamlit_text(text):
    """
    Sanitize AI-generated text for proper Streamlit markdown rendering.
    Escapes LaTeX math delimiters ($) that cause rendering issues.
    """
    import re
    if not text:
        return text
    
    # Remove LaTeX-style math expressions entirely: $...$
    # These cause Streamlit to render text vertically and with weird formatting
    text = re.sub(r'\$([^$]+)\$', r'\1', text)
    
    # Escape any remaining lone dollar signs to prevent LaTeX interpretation
    # But preserve currency amounts like $500 by not escaping $ followed by digit
    text = re.sub(r'\$(?!\d)', r'\\$', text)
    
    # Remove double asterisks that create unwanted bold/formatting
    text = re.sub(r'\*\*', '', text)
    
    # Remove single asterisks used for italics
    text = re.sub(r'(?<!\*)\*(?!\*)', '', text)
    
    # Clean up any LaTeX artifacts like \$ back to $
    text = text.replace('\\$', '$')
    
    # Remove common LaTeX formatting artifacts
    text = re.sub(r'\\text\{([^}]+)\}', r'\1', text)
    text = re.sub(r'\\mathbf\{([^}]+)\}', r'\1', text)
    text = re.sub(r'\\mathrm\{([^}]+)\}', r'\1', text)
    
    # Normalize multiple spaces
    text = re.sub(r'\s+', ' ', text)
    
    return text.strip()


def normalize_insight_text(text):
    """Fix common formatting issues in AI-generated text like missing spaces"""
    import re
    # First sanitize for Streamlit rendering
    text = sanitize_streamlit_text(text)
    
    # Then fix spacing issues
    text = re.sub(r',([a-zA-Z])', r', \1', text)
    text = re.sub(r'\.(\d)', r'. \1', text)
    text = re.sub(r'(\d)([a-zA-Z])', r'\1 \2', text)
    text = re.sub(r'([a-zA-Z])(\$\d)', r'\1 \2', text)
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def extract_compelling_insights(results, max_insights=5):
    """
    Extract 3-5 compelling, specific insights from AI analysis results.
    Prioritizes quantitative trends and specific areas of concern.
    """
    insights = []
    
    all_trends = results.get('all_trends', [])
    deduplicated_issues = results.get('deduplicated_issues', [])
    
    seen_texts = set()
    
    for trend in all_trends:
        text = trend['text'].strip()
        if text and text.lower() not in seen_texts:
            if any(char.isdigit() for char in text) or '%' in text:
                insights.append({
                    'type': 'trend',
                    'text': text,
                    'priority': 1
                })
                seen_texts.add(text.lower())
    
    for issue in deduplicated_issues:
        title = issue.get('title', '').strip()
        impact = issue.get('impact', '').strip()
        
        if title and title.lower() not in seen_texts:
            if impact and len(impact) > 20:
                insight_text = f"{title}: {impact}"
            else:
                insight_text = title
            
            insights.append({
                'type': 'issue',
                'text': insight_text,
                'priority': 2 if issue.get('count', 1) > 1 else 3
            })
            seen_texts.add(title.lower())
    
    insights.sort(key=lambda x: x['priority'])
    
    final_insights = []
    trend_count = 0
    issue_count = 0
    
    for insight in insights:
        if len(final_insights) >= max_insights:
            break
        
        if insight['type'] == 'trend' and trend_count < 3:
            final_insights.append(insight['text'])
            trend_count += 1
        elif insight['type'] == 'issue' and issue_count < 3:
            final_insights.append(insight['text'])
            issue_count += 1
    
    if len(final_insights) < 3:
        for insight in insights:
            if len(final_insights) >= max_insights:
                break
            if insight['text'] not in final_insights:
                final_insights.append(insight['text'])
    
    normalized_insights = [normalize_insight_text(text) for text in final_insights[:max_insights]]
    return normalized_insights


def send_followup_email(user_info, tool_name, results):
    """Send follow-up email to user with detailed insights in branded HTML format"""
    sg = sendgrid.SendGridAPIClient(api_key=os.getenv("SENDGRID_API_KEY"))
    
    insights = extract_compelling_insights(results, max_insights=5)
    
    subject = "alphaSource AI Analysis Results - Key Insights"
    
    if insights:
        insights_html = ""
        for insight in insights:
            insights_html += f'<li style="margin-bottom:8px;">{insight}</li>'
    else:
        insights_html = """
            <li style="margin-bottom:8px;">Multiple areas of operational improvement identified</li>
            <li style="margin-bottom:8px;">Financial patterns requiring closer examination</li>
            <li style="margin-bottom:8px;">Opportunities for enhanced profitability</li>
        """
    
    plain_text = f"""Hi {user_info['first_name']},

Thank you for submitting your financial documents to our AI analysis platform.

Our multi-model AI analysis has completed its review. Here are the key insights we identified:

{chr(10).join(['- ' + i for i in insights]) if insights else '- Multiple areas of operational improvement identified'}

These findings represent just a preview of the detailed analysis we've prepared.

Book a complimentary consultation: https://calendar.app.google/QWQor8w5MqDqGXHv7

Or reply to this email for a personalized review.

- Destinee
alphaSource Consulting
info@alphasourceai.com"""

    html_content = f'''<!doctype html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
  <head>
    <meta http-equiv="x-ua-compatible" content="ie=edge">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <meta name="format-detection" content="telephone=no,address=no,email=no,date=no,url=no">
    <meta name="color-scheme" content="dark only">
    <meta name="supported-color-schemes" content="dark">
    <title>Your Financial Analysis Results</title>
    <!--[if mso]>
      <xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml>
    <![endif]-->
    <style>
      @media (max-width: 640px) {{
        .container {{ width: 100% !important; max-width: 100% !important; }}
        .px-24 {{ padding-left: 16px !important; padding-right: 16px !important; }}
      }}
    </style>
  </head>
  <body style="margin:0;padding:0;background:#0A1547;">
    <div style="display:none!important;max-height:0;overflow:hidden;opacity:0;visibility:hidden;">
      Your AI-powered financial analysis is ready. Key insights identified for your practice.
    </div>

    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="width:100%!important;min-width:100%!important;background:#0A1547;">
      <tr>
        <td align="center">
          <table role="presentation" cellpadding="0" cellspacing="0" width="640" class="container" style="width:640px;max-width:640px;">
            <tr>
              <td class="px-24" style="padding:32px 24px 16px 24px;">
                <a href="https://www.alphasourceai.com" target="_blank" style="text-decoration:none;border:0;outline:0;display:inline-block;">
                  <img src="https://rytlclkkcvvnkoncfaid.supabase.co/storage/v1/object/public/email-assets/Color%20logo%20-%20no%20background.png"
                    alt="alphaSource AI" width="300"
                    style="display:block;max-width:300px;width:300px;height:auto;border:0;outline:none;text-decoration:none;">
                </a>
              </td>
            </tr>

            <tr>
              <td class="px-24" style="padding:8px 24px 24px 24px;">
                <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="border-radius:16px;background:#0F1E5D;border:1px solid rgba(255,255,255,0.08);box-shadow:0 8px 24px rgba(0,0,0,0.25);">
                  <tr>
                    <td style="padding:28px 28px 8px 28px;font-family:-apple-system, Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;color:#E6EBFF;font-size:22px;line-height:28px;font-weight:800;">
                      Your Financial Analysis Results
                    </td>
                  </tr>

                  <tr>
                    <td style="padding:0 28px 18px 28px;font-family:-apple-system, Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;color:#C9D3FF;font-size:14px;line-height:22px;">
                      Hi {user_info['first_name']},<br><br>
                      Thank you for submitting your financial documents to our AI analysis platform. Our multi-model analysis has completed its review.
                    </td>
                  </tr>

                  <tr>
                    <td style="padding:0 28px 10px 28px;font-family:-apple-system, Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;color:#E6EBFF;font-size:16px;line-height:22px;font-weight:800;">
                      Key Insights Identified
                    </td>
                  </tr>
                  <tr>
                    <td style="padding:0 28px 18px 28px;font-family:-apple-system, Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;color:#C9D3FF;font-size:14px;line-height:22px;">
                      <ul style="margin:10px 0 0 18px;padding:0;color:#C9D3FF;">
                        {insights_html}
                      </ul>
                    </td>
                  </tr>

                  <tr>
                    <td style="padding:0 28px 10px 28px;font-family:-apple-system, Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;color:#E6EBFF;font-size:16px;line-height:22px;font-weight:800;">
                      What's Next?
                    </td>
                  </tr>
                  <tr>
                    <td style="padding:0 28px 18px 28px;font-family:-apple-system, Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;color:#C9D3FF;font-size:14px;line-height:22px;">
                      These findings represent just a preview of the detailed analysis we've prepared. The complete report includes specific recommendations, benchmarking insights, and prioritized action items tailored to your practice.
                    </td>
                  </tr>

                  <tr>
                    <td style="padding:6px 28px 18px 28px;font-family:-apple-system, Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;color:#C9D3FF;font-size:14px;line-height:22px;">
                      Ready to dive into the full details? Book a complimentary consultation or simply reply to this email.<br><br>
                      Destinee<br>
                      <span style="color:#6B77C9;">alphaSource Consulting</span>
                    </td>
                  </tr>

                  <tr>
                    <td align="left" style="padding:10px 28px 28px 28px;">
                      <!--[if mso]>
                      <v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" href="https://calendar.app.google/QWQor8w5MqDqGXHv7" arcsize="12%" strokecolor="#AD8BF7" strokeweight="1px" fillcolor="#AD8BF7" style="height:44px;v-text-anchor:middle;width:280px;">
                        <w:anchorlock/>
                        <center style="color:#0A1547;font-family:Segoe UI, Arial, sans-serif;font-size:14px;font-weight:700;">
                          Book Free Consultation
                        </center>
                      </v:roundrect>
                      <![endif]-->
                      <!--[if !mso]><!-- -->
                      <a href="https://calendar.app.google/QWQor8w5MqDqGXHv7" target="_blank"
                         style="display:inline-block;background:#AD8BF7;color:#0A1547;text-decoration:none;
                                font-family:-apple-system, Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;font-weight:700;
                                font-size:14px;line-height:14px;padding:15px 22px;border-radius:10px;
                                border:1px solid rgba(255,255,255,0.12);min-width:260px;text-align:center;">
                        Book Free Consultation
                      </a>
                      <!--<![endif]-->
                    </td>
                  </tr>

                  <tr>
                    <td style="padding:0 28px 28px 28px;font-family:-apple-system, Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;color:#6B77C9;font-size:12px;line-height:18px;">
                      Need help? Email <a href="mailto:info@alphasourceai.com" style="color:#C9D3FF;text-decoration:none;">info@alphasourceai.com</a>
                    </td>
                  </tr>

                </table>
              </td>
            </tr>

            <tr>
              <td class="px-24" style="padding:8px 24px 40px 24px;font-family:-apple-system, Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;color:#6B77C9;font-size:11px;line-height:16px;text-align:left;">
                &copy; alphaSource AI - All rights reserved.
              </td>
            </tr>

          </table>
        </td>
      </tr>
    </table>
  </body>
</html>'''
    
    message = Mail(
        from_email="info@alphasourceai.com",
        to_emails=user_info['email'],
        subject=subject,
        plain_text_content=plain_text,
        html_content=html_content
    )
    
    message.tracking_settings = TrackingSettings()
    message.tracking_settings.click_tracking = ClickTracking(enable=False, enable_text=False)
    
    sg.send(message)


def send_email(user_info, file_content, file_name, file_type, results, tool_name):
    """Send full analysis email to admin team"""
    sg = sendgrid.SendGridAPIClient(api_key=os.getenv("SENDGRID_API_KEY"))
    
    subject = f"[{tool_name}] {user_info['office_name']} ({user_info['email']})"
    
    raw_analyses = results['raw_analyses']
    all_trends = results.get('all_trends', [])
    
    trends_section = ""
    if all_trends:
        trends_section = "\n\n=== KEY TRENDS IDENTIFIED ===\n"
        unique_trends = []
        for trend in all_trends[:5]:
            if trend['text'] not in [t['text'] for t in unique_trends]:
                unique_trends.append(trend)
        
        for i, trend in enumerate(unique_trends, 1):
            trends_section += f"{i}. {trend['text']} (Source: {trend['source']})\n"
    
    body = textwrap.dedent(f"""
        New file submitted for analysis.
        
        Tool: {tool_name}
        File Name: {file_name}
        File Type: {file_type}
        
        Submitted by:
        First Name: {user_info['first_name']}
        Last Name: {user_info['last_name']}
        Office/Group: {user_info['office_name']}
        Email: {user_info['email']}
        Type: {user_info['org_type']}
        
        --- AI Analysis ---
        
        OpenAI Analysis:
        {raw_analyses["OpenAI Analysis"]}
        
        xAI Analysis:
        {raw_analyses["xAI Analysis"]}
        
        AnthropicAI Analysis:
        {raw_analyses["AnthropicAI Analysis"]}{trends_section}
    """).strip()
    
    with open("/tmp/unified_analysis.txt", "w") as f:
        f.write(f"Analysis Results for {user_info['office_name']}\n")
        f.write(f"\nTool: {tool_name}\n")
        for model, analysis in raw_analyses.items():
            f.write(f"\n{model}:\n{analysis}\n")
    
    with open("/tmp/unified_analysis.txt", "rb") as f:
        file_data = f.read()
        encoded = base64.b64encode(file_data).decode()
        analysis_attachment = Attachment(
            FileContent(encoded),
            FileName("unified_analysis.txt"),
            FileType("text/plain"),
            Disposition("attachment")
        )
    
    original_encoded = base64.b64encode(file_content).decode()
    original_attachment = Attachment(
        FileContent(original_encoded),
        FileName(file_name),
        FileType(file_type),
        Disposition("attachment")
    )
    
    message = Mail(
        from_email="info@alphasourceai.com",
        to_emails="consulting@alphasourceai.com",
        subject=subject,
        plain_text_content=body
    )
    message.add_attachment(analysis_attachment)
    message.add_attachment(original_attachment)
    sg.send(message)
