FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir ".[dev]"

COPY alembic.ini ./
COPY alembic ./alembic
COPY tests ./tests

CMD ["python", "-m", "app.scheduler"]
