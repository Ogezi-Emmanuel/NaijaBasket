"""Time-driven sweeps for the two PRD flows that cannot live inside a request.

* FR-02 - the 15-minute merchant picking SLA, with automatic escalation.
* FR-06 - abandoned-cart recovery over WhatsApp.

Run this as an always-on worker (`python scheduler.py`) on Railway / Fly, or
dispatch the two Render cron jobs declared in render.yaml instead. Run one or
the other, never both, or every sweep fires twice.

Intervals are overridable:
    SLA_INTERVAL_SECONDS       default 60
    RECOVERY_INTERVAL_SECONDS  default 300
"""

import logging
import os
import signal
import time

from dotenv import load_dotenv

load_dotenv()

from app import create_app  # noqa: E402
from app.services import sla as sla_service  # noqa: E402
from app.services import whatsapp as whatsapp_service  # noqa: E402

logger = logging.getLogger("naijabasket.scheduler")

_running = True
_TICK_SECONDS = 5


def _handle_stop(signum, _frame) -> None:
    global _running
    _running = False
    logger.info("Received signal %s; stopping after the current cycle.", signum)


def main() -> None:
    sla_interval = int(os.environ.get("SLA_INTERVAL_SECONDS", 60))
    recovery_interval = int(os.environ.get("RECOVERY_INTERVAL_SECONDS", 300))

    app = create_app()
    logging.basicConfig(
        level=app.config.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    logger.info(
        "Scheduler started (sla every %ss, recovery every %ss).",
        sla_interval,
        recovery_interval,
    )

    next_sla = 0.0
    next_recovery = 0.0

    while _running:
        now = time.monotonic()
        try:
            # A fresh app context per cycle keeps the SQLAlchemy session clean
            # and reconnects automatically if the database blips.
            with app.app_context():
                if now >= next_sla:
                    result = sla_service.reconcile()
                    if result["warnings"] or result["breaches"]:
                        logger.info("SLA sweep: %s", result)
                    next_sla = now + sla_interval

                if now >= next_recovery:
                    links = whatsapp_service.run_sweep()
                    if links:
                        logger.info("Cart recovery queued %s link(s).", len(links))
                    next_recovery = now + recovery_interval
        except Exception:  # keep the loop alive across transient failures
            logger.exception("Sweep cycle failed; will retry on the next tick.")

        time.sleep(_TICK_SECONDS)

    logger.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
