# app/worker.py
#
# Run this as its own process/container:  python -m app.worker
#
# Kept separate from the FastAPI app on purpose — see the comment at the
# top of app/scheduler.py for why.

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from . import config
from .database import engine
from .models import Base
from .scheduler import check_and_process_windows

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    config.validate_config()

    # dev convenience only — use Alembic migrations in production instead
    Base.metadata.create_all(bind=engine)

    scheduler = BlockingScheduler()
    scheduler.add_job(
        check_and_process_windows,
        "interval",
        seconds=config.WINDOW_POLL_INTERVAL_SECONDS,
        id="rca_window_processor",
        max_instances=1,   # don't let a slow tick overlap with the next one
        coalesce=True,
    )

    logger.info(
        "RCA worker started (poll interval=%ss)",
        config.WINDOW_POLL_INTERVAL_SECONDS,
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("RCA worker shutting down")


if __name__ == "__main__":
    main()
