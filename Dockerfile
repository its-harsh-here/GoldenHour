# GoldenHour container: python run.py on $PORT (Cloud Run, Render, Fly and most hosts set PORT).
# Defaults to replay mode so a public deployment never spends a Groq key; set LLM_MODE=groq and
# GROQ_API_KEY as host secrets to go live. Secrets are never baked into the image (.dockerignore).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    LLM_MODE=replay \
    SEED_HISTORY=true \
    DB_PATH=/tmp/goldenhour.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT', '8000'))"
CMD ["python", "run.py"]
