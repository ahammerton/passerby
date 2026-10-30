# Build from the repo root: docker build -f backend/Dockerfile -t ghcr.io/ahammerton/passerby:latest .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app/backend
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY frontend /app/frontend

RUN useradd --create-home --uid 1000 tracker && mkdir -p /data && chown tracker /data
USER tracker
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/status')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
