FROM python:3.12-slim

# Build arg -> env var: shown in /health, app_info and every log line.
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

# Dependencies first so this layer is cached when only code changes.
COPY services/payments/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Shared package + this service (build context is the repository root).
COPY common ./common
COPY services/payments/app ./app

# Run as a non-root user.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home app
USER app

EXPOSE 8002
# One worker per service (design decision D5); access log is written by our own middleware.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002", "--workers", "1", "--no-access-log"]
