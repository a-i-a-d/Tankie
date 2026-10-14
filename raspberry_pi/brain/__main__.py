"""Entry point for the Tankie brain (issue #82, T6).

Wires ``python3 -m brain`` to the :class:`~brain.brain.Brain` orchestrator:
load the config, construct the Brain, start it, run until SIGINT/SIGTERM,
then stop cleanly (the reflex loop sends a final ``drive 0 0``).

This is the T12 systemd target (the brain runs as a long-lived process).
``--once`` (A1) runs one bounded cycle and exits — the T14 smoke-test path.

No IP addresses or model names in this module (A4); everything comes from
the config source (``brain.yaml`` / env).
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .brain import Brain
from .config import load_config


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m brain",
        description="Run the Tankie AI brain (reflex + deliberation loops).",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="run one bounded cycle (one reflex idle tick + one deliberation "
        "tick if a task is set), then stop cleanly and exit (T14 smoke test).",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="logging level (default: INFO).",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"brain {__version__}",
    )
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("brain.__main__")

    config = load_config()
    brain = Brain(config)
    log.info("brain starting (vlm=%s reflex_hz=%s deliberate_hz=%s)",
             config.vlm_model, config.reflex_hz, config.deliberate_hz)
    brain.start()
    try:
        brain.run(once=args.once)
    finally:
        brain.stop()
    log.info("brain stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
