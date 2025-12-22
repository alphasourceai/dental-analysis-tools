# Dental Operations Analysis Tools

### Overview
This Streamlit-based web application provides AI-powered analysis of dental practice operations. It functions as a consulting platform with a public-facing financial analysis tool for clients and a secure admin dashboard for advanced analytics and client management. The platform aims to enhance operational efficiency and profitability for dental practices by leveraging multiple AI models for comprehensive insights.

### User Preferences
I prefer iterative development with clear communication at each stage. Please ask before making major architectural changes or introducing new dependencies. Focus on delivering robust and maintainable code. For explanations, prioritize conciseness while ensuring all critical information is conveyed.

### System Architecture
The application is built with a Streamlit frontend for interactive web interfaces and a Python backend utilizing SQLAlchemy ORM for database interactions. PostgreSQL is used as the primary database. The system integrates with OpenAI GPT-4, xAI Grok, and Anthropic Claude for multi-AI analysis, providing structured outputs (ISSUE → IMPACT → RECOMMENDATION) and supporting deduplication of findings. Email notifications are handled via SendGrid.

**Key Architectural Decisions:**
- **UI/UX:** A professional design features a navy blue background with purple accents, custom SVG icons, Raleway font, and a full-width main content area. The public page is streamlined for financial analysis, while advanced AR/Claims analysis is exclusive to the admin dashboard.
- **AI Integration:** Prompts are engineered for dental consulting expertise, requiring a minimum number of improvement areas. A parsing mechanism extracts structured data and quantitative trends.
- **Admin Dashboard:** Provides secure, multi-user access with bcrypt-hashed passwords, an "Admin Setup" page for initial credential creation, and comprehensive client submission tracking. It combines user and upload data, displaying detailed AI outputs in modal views. Admin management includes password changes and new user creation with forced password resets.
- **Data Handling:** File uploads (Excel, CSV, PDF) are processed, with PDF text and image extraction handled by PyMuPDF and OCR via pytesseract. Analysis data is stored as JSON in the database.
- **Email System:** Sends strategic, high-level summaries to clients and detailed AI outputs, including original uploaded files, to the admin team. SendGrid click tracking is disabled for cleaner links.
- **Security:** Replit Secrets manage API keys and initial admin setup token. The admin setup process is secured with a token and is only accessible when no admin accounts exist.

**Feature Specifications:**
- **Public Page:** Financial Analysis file uploads, single document analysis.
- **Admin Dashboard:**
    - Client Submissions: View user and upload records, access detailed AI analyses.
    - Document Analysis: Admin-exclusive AR and Insurance Claims analysis with client info input.
    - Admin Management: User and password management, admin account creation.
- **Multi-AI Analysis:** Utilizes `gpt-4o`, `grok-4-1-fast-reasoning`, and `claude-sonnet-4-5` for up-to-date model access via stable aliases.
- **Deployment:** Configured for Replit autoscale deployment.

### External Dependencies
- **Database:** PostgreSQL (Replit's built-in)
- **AI Services:**
    - OpenAI GPT-4 (`gpt-4o`)
    - xAI Grok (`grok-4-1-fast-reasoning`)
    - Anthropic Claude (`claude-sonnet-4-5`)
- **Email Service:** SendGrid
- **File Processing:** PyMuPDF (for PDF), pytesseract (for OCR)