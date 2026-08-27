# RCA-Tool

Automated root-cause analysis for Prometheus/Alertmanager alerts. Alertmanager
webhooks in, the service pulls Prometheus history + K8s/deployment context
around each alert, groups related alerts into one incident, determines a
likely root cause per group, and emails a report.

## Architecture

```
Alertmanager --webhook--> [api]  --writes-->  Postgres
                                                  ^
                                                  |
                                              [worker]  (polls every N seconds)
                                                  |
                                     evidence -> correlation -> RCA -> email
```

Two processes, one image:

- **api** — receives the Alertmanager webhook, validates + stores alerts,
  buckets them into a debounced grouping window. Read endpoints for
  inspecting windows/alerts. Stateless, safe to run multiple replicas.
- **worker** — polls for windows whose debounce period has elapsed and runs
  the pipeline (evidence collection -> correlation -> RCA -> email). Kept out
  of the API process on purpose: if the scheduler ran inside `api`, running
  more than one uvicorn worker or replica (normal in prod) would spin up
  multiple independent pollers racing on the same windows. `worker` claims
  windows with `SELECT ... FOR UPDATE SKIP LOCKED`, so it's also safe to
  scale to multiple replicas if one falls behind.
- **migrate** — one-shot Alembic migration runner; `api`/`worker` wait for
  it to finish before starting.

### Alert grouping

A window stays open and absorbs new alerts as long as they keep arriving
within `WINDOW_DEBOUNCE_MINUTES` of each other (default 10), capped at
`WINDOW_MAX_DURATION_MINUTES` (default 30) so a continuous storm can't keep
a window open indefinitely. This is what lets a deploy at 9:59 and the
symptoms it causes at 10:01 land in the same window and get correlated
together, instead of being split by a fixed-size bucket.

Within a window, `CorrelationEngine` groups alerts by (in order of
confidence): shared recent deployment -> time+node proximity -> same alert
type. `RCAEngine` then applies a rule ladder (deployment -> OOMKill -> disk
full -> node pressure -> cascade -> CPU saturation -> undetermined) per group.

## Running it

```bash
cp .env.example .env   # fill in real values — see comments in the file
docker compose up -d --build
```

- API: `http://localhost:8000` — `GET /health` for liveness/readiness
- `POST /webhook/alertmanager` — point Alertmanager's `webhook_config` here,
  with `X-Webhook-Token: <WEBHOOK_TOKEN>` as a header
- `POST /windows/{id}/process` — force a window through the pipeline now,
  without waiting for the worker (useful for testing)

### Alertmanager config example

```yaml
receivers:
  - name: rca-tool
    webhook_configs:
      - url: http://rca-api:8000/webhook/alertmanager
        http_config:
          authorization:
            type: X-Webhook-Token
            credentials: <WEBHOOK_TOKEN>
```
(Adjust to however your Alertmanager version expresses custom headers —
some setups need a reverse proxy in front to inject the header instead.)

## Migrations

Schema changes go through Alembic, not `Base.metadata.create_all`:

```bash
# after changing app/models.py
alembic revision --autogenerate -m "describe the change"
alembic upgrade head
```

`create_all` is still called on process startup as a dev-only fallback so a
fresh checkout works without a migration step — in any real deployment the
`migrate` service in docker-compose is what actually applies schema changes.

## Security notes

- The webhook is protected by a shared-secret header (`WEBHOOK_TOKEN`) — set
  it before exposing this anywhere non-local.
- Every alert label that ends up interpolated into a PromQL query goes
  through an allowlist filter (`app/utils/promql.py`) first. Alert labels
  are attacker/operator controlled data, not trusted input.
- No secrets are baked into `docker-compose.yml`; everything comes from
  `.env` (see `.env.example`).

## Local dev without Docker

```bash
pip install -r requirements.txt
cp .env.example .env   # point DATABASE_URL at a local Postgres or sqlite:///./dev.db
alembic upgrade head
uvicorn app.main:app --reload          # terminal 1
python -m app.worker                   # terminal 2
```
