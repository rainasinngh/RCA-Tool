import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL")

# --- Window / correlation processing ---
# A window stays "open" and keeps absorbing new alerts as long as they keep
# arriving within WINDOW_DEBOUNCE_MINUTES of each other (standard alert-storm
# debounce pattern) — this is what lets a deploy at 9:59 and symptoms at
# 10:01 land in the same window instead of being split across two.
# WINDOW_MAX_DURATION_MINUTES is a hard cap so one continuous alert storm
# can't keep a window open forever.
WINDOW_DEBOUNCE_MINUTES = int(os.getenv("WINDOW_DEBOUNCE_MINUTES", "10"))
WINDOW_MAX_DURATION_MINUTES = int(os.getenv("WINDOW_MAX_DURATION_MINUTES", "30"))

# how often (seconds) the worker polls for windows that are ready to process
WINDOW_POLL_INTERVAL_SECONDS = int(os.getenv("WINDOW_POLL_INTERVAL_SECONDS", "60"))

# --- Webhook auth ---
# Alertmanager should be configured to send this as a header, e.g.
#   http_config: { authorization: { credentials: "<token>" } }
# or a custom header — see README. Leave unset only for local dev.
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN")

# --- SMTP / email report settings ---
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"

MAIL_FROM = os.getenv("MAIL_FROM", SMTP_USERNAME)
# comma-separated list of recipients, e.g. "oncall@company.com,sre-team@company.com"
MAIL_TO = [addr.strip() for addr in os.getenv("MAIL_TO", "").split(",") if addr.strip()]

# --- Environment ---
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")


def validate_config():
    """
    Fail fast on missing required config instead of dying halfway through
    the first webhook call or the first scheduler tick. Call this once at
    process startup (both the API and the worker do).
    """
    missing = []
    if not DATABASE_URL:
        missing.append("DATABASE_URL")
    if not PROMETHEUS_URL:
        missing.append("PROMETHEUS_URL")

    if missing:
        raise RuntimeError(
            f"Missing required environment variable(s): {', '.join(missing)}"
        )

    if ENVIRONMENT == "production" and not WEBHOOK_TOKEN:
        logger.warning(
            "WEBHOOK_TOKEN is not set while ENVIRONMENT=production — "
            "the /webhook/alertmanager endpoint is unauthenticated."
        )

    if not SMTP_HOST or not MAIL_TO:
        logger.warning(
            "SMTP_HOST/MAIL_TO not fully configured — RCA reports will be "
            "generated but not emailed until this is set."
        )
