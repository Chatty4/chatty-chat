# Packages shared by the API and the labeler; changes only when requirements.txt changes
FROM python:3.14-slim AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN useradd --create-home --uid 1000 app

COPY requirements.txt .
RUN pip install -r requirements.txt


# The labeling worker: deps + OCR and image models (~400 MB more)
FROM deps AS labeler
COPY requirements-labeler.txt .
RUN pip install -r requirements-labeler.txt
COPY app ./app
USER app
CMD ["python", "-m", "app.jobs.label_attachments"]


# Last stage = what a plain build produces: the API, without the labeler packages
FROM deps AS api
COPY app ./app
COPY alembic.ini .
COPY alembic ./alembic

USER app

EXPOSE 8001

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=2)"

# Clients only send heartbeat and watch frames (under 200 bytes); the default allows 16 MB.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--ws-max-size", "4096"]