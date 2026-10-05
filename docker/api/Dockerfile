# Analytics API (FastAPI + uvicorn). Read-only access to the warehouse as the
# role created by `python -m api.provision` (compose service api-init).
# Pinned to bookworm like docker/spark; installs only requirements/api.txt
# (no plotting, dbt or Great Expectations) from the lock file.
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/opt/connecthub \
    API_HOST=0.0.0.0 \
    API_PORT=8000

WORKDIR /opt/connecthub
COPY requirements/api.txt requirements/constraints-py311.txt requirements/
RUN pip install --no-cache-dir -r requirements/api.txt -c requirements/constraints-py311.txt

COPY api/ api/
COPY pipeline/ pipeline/
COPY analytics/__init__.py analytics/features.py analytics/health_scoring.py \
     analytics/retention.py analytics/
COPY experimentation/__init__.py experimentation/experiments.py experimentation/

RUN useradd --system --uid 10001 --no-create-home app
USER app

EXPOSE 8000
# Liveness only (no database): a stopped warehouse must not restart the API.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('API_PORT', '8000')}/api/health\", timeout=4)"]
CMD ["python", "-m", "api"]
