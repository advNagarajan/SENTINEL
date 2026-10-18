FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY services/logging_service/requirements.lock /tmp/logging-requirements.lock

RUN addgroup --system sentinel \
    && adduser --system --ingroup sentinel sentinel \
    && python -m pip install --no-cache-dir --require-hashes -r /tmp/logging-requirements.lock \
    && mkdir -p /var/lib/sentinel-logging \
    && chown -R sentinel:sentinel /var/lib/sentinel-logging

COPY --chown=sentinel:sentinel schemas/__init__.py schemas/observability.py /app/schemas/
COPY --chown=sentinel:sentinel services/__init__.py /app/services/__init__.py
COPY --chown=sentinel:sentinel services/logging_service /app/services/logging_service

USER sentinel

EXPOSE 8200

CMD ["uvicorn", "services.logging_service.main:app", "--host", "0.0.0.0", "--port", "8200", "--workers", "1"]
