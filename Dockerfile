# Planilla Costa Rica · API para Railway
# Dependencias fijadas con hashes (api/requirements.lock), usuario sin privilegios y sin caché.
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv
COPY api/requirements.lock /srv/requirements.lock
RUN pip install --require-hashes -r /srv/requirements.lock
COPY motor /srv/motor
COPY api /srv/api
RUN pip install --no-deps /srv/motor /srv/api \
 && useradd --system --uid 10001 --home /nonexistent --shell /usr/sbin/nologin aurom
USER 10001
WORKDIR /srv/api
EXPOSE 8000
# La IP del cliente se toma del último salto de X-Forwarded-For (app/seguridad.py).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --no-server-header --workers ${WEB_CONCURRENCY:-2}"]
