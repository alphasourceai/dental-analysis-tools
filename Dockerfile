FROM python:3.10-slim

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    poppler-utils \
    ghostscript \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /root/.streamlit
COPY .streamlit/config.toml /root/.streamlit/config.toml

RUN mkdir -p /var/secrets

EXPOSE 8080

CMD sh -c 'streamlit run app.py --server.port=${PORT:-8080} --server.address=0.0.0.0'
