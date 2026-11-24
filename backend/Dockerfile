# Build from the repo root: docker build -f backend/Dockerfile -t ghcr.io/ahammerton/passerby:latest .
FROM python:3.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app/backend
COPY backend/requirements.txt .
# pip isn't needed once the packages are in, and the libraries bundled inside it (urllib3, msgpack,
# setuptools) get reported as vulnerabilities in this image. ensurepip holds a second copy.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall --yes pip \
    && rm -rf /usr/local/lib/python3.*/ensurepip

COPY backend/app ./app
COPY frontend /app/frontend

RUN useradd --create-home --uid 1000 tracker && mkdir -p /data && chown tracker /data
USER tracker
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/status')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
