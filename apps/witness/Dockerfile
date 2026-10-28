FROM python:3.12-slim

# Pinned to the versions the API is tested with (no floating installs in a key-holding service).
RUN pip install --no-cache-dir fastapi==0.115.6 "uvicorn[standard]==0.34.0" cryptography==50.0.2 pydantic==2.10.4

WORKDIR /app
COPY __init__.py config.py crypto.py main.py /app/apps/witness/
RUN touch /app/apps/__init__.py \
    && useradd --system --uid 10002 witness \
    && mkdir -p /var/lib/witness && chown witness /var/lib/witness

ENV PYTHONPATH=/app WITNESS_STATE_PATH=/var/lib/witness/state.json
USER witness
EXPOSE 8000
CMD ["uvicorn", "apps.witness.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
