import logging

from fastapi import FastAPI, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from sqlalchemy import text
from datetime import datetime, timedelta

from .database import engine, SessionLocal
from .models import Base, Alert, AlertWindow
from .schemas.alertmanager import AlertmanagerWebhookPayload
from .scheduler import process_window
from .services.prometheus import PrometheusService
from . import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

config.validate_config()

# NOTE: for production use Alembic migrations (see /alembic) instead of
# create_all — this is kept only as a dev convenience so a fresh checkout
# works without a migration step.
Base.metadata.create_all(bind=engine)

app = FastAPI(title="RCA Engine")


# Proper dependency injection for DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def verify_webhook_token(x_webhook_token: str | None = Header(default=None)):
    """
    Shared-secret auth for the Alertmanager webhook. Configure Alertmanager's
    webhook_config to send this header. If WEBHOOK_TOKEN isn't set at all,
    auth is skipped (dev only — validate_config() warns loudly about this
    in production).
    """
    if config.WEBHOOK_TOKEN and x_webhook_token != config.WEBHOOK_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing webhook token")


def get_or_create_window(db: Session) -> AlertWindow:
    """
    Debounced grouping window: reuse the current open window and push its
    window_end forward as long as alerts keep arriving, up to a hard cap
    of WINDOW_MAX_DURATION_MINUTES. This is what lets a deploy and the
    symptoms it causes a couple minutes later land in the same window
    instead of being split by an arbitrary fixed-size bucket.

    Uses SELECT ... FOR UPDATE so concurrent webhook deliveries (Alertmanager
    retries, multiple API replicas) can't race and create duplicate windows.
    """
    now = datetime.utcnow()

    window = (
        db.query(AlertWindow)
        .filter(AlertWindow.status == "open")
        .order_by(AlertWindow.window_start.desc())
        .with_for_update()
        .first()
    )

    if window:
        window_age = now - window.window_start
        if now <= window.window_end and window_age < timedelta(minutes=config.WINDOW_MAX_DURATION_MINUTES):
            # still within the debounce gap and under the hard cap — extend it
            max_end = window.window_start + timedelta(minutes=config.WINDOW_MAX_DURATION_MINUTES)
            window.window_end = min(now + timedelta(minutes=config.WINDOW_DEBOUNCE_MINUTES), max_end)
            return window
        # otherwise this window is past due (the worker will pick it up
        # on its next poll) or has hit the max duration cap — start a new one

    window = AlertWindow(
        window_start=now,
        window_end=now + timedelta(minutes=config.WINDOW_DEBOUNCE_MINUTES),
        status="open",
        alert_count=0,
    )
    db.add(window)
    db.flush()  # get the id without committing

    return window


def parse_prometheus_time(ts: str | None) -> datetime | None:
    """
    Parse Prometheus ISO 8601 timestamp safely.
    """
    if not ts or ts == "0001-01-01T00:00:00Z":
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


@app.get("/")
async def root():
    return {"status": "RCA Engine Running"}


@app.get("/health")
async def health(db: Session = Depends(get_db)):
    """
    Liveness/readiness probe. Checks the two hard dependencies: DB and
    Prometheus. SMTP is deliberately NOT checked here — a broken mail
    server shouldn't take the whole service out of rotation, since RCA
    reports still get generated and persisted either way.
    """
    checks = {}

    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {e}"

    try:
        PrometheusService().instant_query("up")
        checks["prometheus"] = "ok"
    except Exception as e:
        checks["prometheus"] = f"error: {e}"

    healthy = all(v == "ok" for v in checks.values())
    status_code = 200 if healthy else 503

    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=status_code,
        content={"status": "ok" if healthy else "unhealthy", "checks": checks},
    )


@app.post("/webhook/alertmanager", dependencies=[Depends(verify_webhook_token)])
async def receive_alert(payload: AlertmanagerWebhookPayload, db: Session = Depends(get_db)):

    saved = 0
    skipped = 0

    window = get_or_create_window(db)

    for item in payload.alerts:
        alertname = item.labels.get("alertname")
        if not alertname:
            logger.warning("Dropping alert with no 'alertname' label: %s", item.labels)
            skipped += 1
            continue

        fingerprint = item.fingerprint

        # skip duplicates — Alertmanager retries the same alert
        if fingerprint:
            exists = db.query(Alert).filter(
                Alert.fingerprint == fingerprint
            ).first()
            if exists:
                skipped += 1
                continue

        alert = Alert(
            alert_name=alertname,
            instance=item.labels.get("instance"),
            severity=item.labels.get("severity"),
            status=item.status,
            fingerprint=fingerprint,
            starts_at=parse_prometheus_time(item.startsAt),
            window_id=window.id,
            rca_status="pending",
            raw_payload=item.model_dump(),
        )

        db.add(alert)
        window.alert_count += 1
        saved += 1

    db.commit()

    return {
        "status": "success",
        "alerts_received": len(payload.alerts),
        "alerts_saved": saved,
        "alerts_skipped": skipped,
        "window_id": window.id,
    }


@app.get("/windows")
async def list_windows(db: Session = Depends(get_db)):
    windows = db.query(AlertWindow).order_by(
        AlertWindow.created_at.desc()
    ).limit(20).all()
    return windows


@app.get("/alerts/{window_id}")
async def list_alerts(window_id: int, db: Session = Depends(get_db)):
    alerts = db.query(Alert).filter(
        Alert.window_id == window_id
    ).all()
    return alerts


@app.post("/windows/{window_id}/process")
async def process_window_now(window_id: int, db: Session = Depends(get_db)):
    """
    Manually trigger the RCA pipeline (evidence -> correlation -> RCA -> email)
    for a window, without waiting for the worker or the debounce window to
    close naturally. Useful for testing/ops.
    """
    window = (
        db.query(AlertWindow)
        .filter(AlertWindow.id == window_id)
        .with_for_update()
        .first()
    )
    if not window:
        raise HTTPException(status_code=404, detail="window not found")
    if window.status != "open":
        raise HTTPException(status_code=400, detail=f"window is '{window.status}', not 'open'")

    process_window(db, window)

    return {"status": "processed", "window_id": window.id}
