FROM python:3.12-slim AS base

# system deps needed to build psycopg2-binary's dependencies stay minimal
# since we use the binary wheel; curl only for the healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

RUN useradd --create-home --shell /bin/false rca \
    && chown -R rca:rca /srv
USER rca

EXPOSE 8000

# default command runs the API; docker-compose overrides this for the
# worker (python -m app.worker) and the one-shot migrate service
# (alembic upgrade head)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
