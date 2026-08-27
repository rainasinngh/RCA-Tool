# app/scheduler.py
#
# Pure pipeline logic — no scheduling library here. The actual polling loop
# lives in app/worker.py, which is meant to run as its own process/container,
# separate from the API. That split matters: if the scheduler ran inside the
# FastAPI app itself, running more than one uvicorn worker or more than one
# API replica (both normal in production) would spin up multiple independent
# pollers, all racing to process the same windows. Running it as a single
# dedicated worker process (with row-level locking as a second line of
# defense if you ever do scale the worker itself) avoids that entirely.

import logging
from datetime import datetime

from .database import SessionLocal
from .models import AlertWindow, Alert
from .services.evidence import EvidenceCollector
from .services.correlation import CorrelationEngine
from .services.rca import RCAEngine
from .services.report import build_window_report
from .services.mailer import send_rca_report

logger = logging.getLogger(__name__)


def check_and_process_windows():
    """
    Find windows that are still 'open' but past their window_end, claim
    each one with a row lock (SELECT ... FOR UPDATE SKIP LOCKED on Postgres;
    a harmless no-op on SQLite in dev), and run the pipeline on it.

    Claiming one window per transaction — rather than locking the whole
    due-list at once — means a slow window doesn't block others from being
    picked up, and a second worker process can safely run the same query
    without double-processing anything.
    """
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        due_ids = [
            row.id
            for row in db.query(AlertWindow.id)
            .filter(AlertWindow.status == "open", AlertWindow.window_end <= now)
            .all()
        ]
    finally:
        db.close()

    for window_id in due_ids:
        _claim_and_process(window_id)


def _claim_and_process(window_id: int):
    db = SessionLocal()
    try:
        window = (
            db.query(AlertWindow)
            .filter(AlertWindow.id == window_id, AlertWindow.status == "open")
            .with_for_update(skip_locked=True)
            .first()
        )
        if not window:
            # already claimed by another worker process, or already moved
            # on from 'open' since the due-list was built
            return

        try:
            process_window(db, window)
        except Exception:
            logger.exception("Failed processing window %s", window_id)
            db.rollback()
            window = db.query(AlertWindow).filter(AlertWindow.id == window_id).first()
            if window:
                window.status = "failed"
                db.commit()
    finally:
        db.close()


def process_window(db, window: AlertWindow):
    """
    Full pipeline for one window:
      1. collect evidence for each pending alert
      2. correlate alerts into groups (writes AlertGroup rows)
      3. run RCA on each group (writes RCAFinding rows)
      4. render + email a single report covering the whole window
    """

    logger.info("Processing window %s (%s alert(s))", window.id, window.alert_count)

    window.status = "analyzing"
    db.commit()

    alerts = db.query(Alert).filter(Alert.window_id == window.id).all()

    # --- 1. evidence collection ---
    collector = EvidenceCollector()
    for alert in alerts:
        if alert.rca_status != "pending":
            continue
        if not alert.instance or not alert.starts_at:
            # can't query Prometheus without a host + timestamp
            alert.rca_status = "failed"
            continue
        try:
            collector.collect_for_alert(
                db=db,
                alert_id=alert.id,
                alert_name=alert.alert_name,
                instance=alert.instance,
                alert_time=alert.starts_at,
            )
            alert.rca_status = "analyzing"
        except Exception:
            logger.exception("Evidence collection failed for alert %s", alert.id)
            alert.rca_status = "failed"
    db.commit()

    # --- 2. correlation ---
    groups = CorrelationEngine().correlate_window(db, window.id)

    # --- 3. RCA ---
    rca_engine = RCAEngine()
    findings = []
    for group in groups:
        try:
            findings.append(rca_engine.analyze_group(db, group))
        except Exception:
            logger.exception("RCA analysis failed for group %s", group.group_id)

    for alert in alerts:
        if alert.rca_status == "analyzing":
            alert.rca_status = "complete"
    db.commit()

    # --- 4. report + email ---
    if findings:
        try:
            html_body, subject = build_window_report(window, groups, findings, alerts)
            send_rca_report(subject=subject, html_body=html_body)
        except Exception:
            logger.exception("Report generation/email failed for window %s", window.id)
    else:
        logger.info("Window %s had no groups to report (no alerts?)", window.id)

    window.status = "complete"
    db.commit()

    logger.info(
        "Window %s complete — %s group(s), %s finding(s)",
        window.id, len(groups), len(findings),
    )
