# SentinelOps incident detector (module M4). Standard library only: no pip install, no requirements file.
# Build context is the repository root, like the M1 Dockerfiles (docker-compose.yml: context ".").
FROM python:3.12-slim

# APP_VERSION comes from the build argument and is reported by /health (a rolled-back image reports its own version).
ARG APP_VERSION=1.0.0
ENV APP_VERSION=${APP_VERSION} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non-root user app (uid 10001). /data is created and owned by app BEFORE the named volume detector-data is
# first mounted: Docker copies this directory and its owner into a new empty named volume, so the volume is writable.
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir /data \
 && chown app:app /data

WORKDIR /srv
COPY detector/app ./app
COPY detector/rules ./rules

USER app
EXPOSE 9100
CMD ["python", "-m", "app.main"]
