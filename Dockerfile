# Cloud Run image for the web app + API. The ingestion functions deploy separately from ingestion/.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend backend
COPY frontend frontend

RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

# --proxy-headers / --forwarded-allow-ips make request.url.scheme reflect the https the browser used,
# which the same-origin check relies on. Cloud Run terminates TLS in front of the container.
CMD exec python -m uvicorn backend.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips="*"
