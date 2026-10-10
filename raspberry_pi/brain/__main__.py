"""Placeholder entry point for the Tankie brain (issue #72, Phase 1 T1).

Keeps ``python3 -m brain`` a valid target for the T12 systemd service (which
runs the brain as a long-lived process). The actual loop (reflex +
deliberation) lands in Phase 2; until then this prints a notice and exits
non-zero so a mis-wired service is visible rather than silently idle.
"""

import sys

from . import __version__

_MSG = (
    "brain not yet implemented (Phase 2) — scaffolding only (issue #72).\n"
    f"brain package version: {__version__}\n"
    "Phase 2 will add the reflex + deliberation loops (issue #69)."
)


def main() -> int:
    print(_MSG, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
