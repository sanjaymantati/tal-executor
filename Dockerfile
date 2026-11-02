FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini ./
COPY app ./app

RUN useradd --create-home --uid 1000 tal \
    && mkdir -p /app/data \
    && chown -R tal /app
USER tal

# Migrations are idempotent; running them first means a fresh volume just works.
CMD ["sh", "-c", "alembic upgrade head && exec python -m app.main run"]
