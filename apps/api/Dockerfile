FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install build deps for argon2-cffi wheels if needed, then clean up.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Dependency manifest first for better layer caching.
COPY pyproject.toml ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY scripts ./scripts

RUN pip install --upgrade pip && pip install .

# Drop build tooling to keep the image lean.
RUN apt-get purge -y build-essential && apt-get autoremove -y

# Non-root user.
RUN useradd --create-home --uid 10001 appuser && chmod +x ./scripts/*.sh
USER appuser

EXPOSE 8000

CMD ["bash", "scripts/entrypoint.sh"]
