"""Run the internal two-tenant Alpha and two governed release cycles."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    environment = {**os.environ, "DATABASE_URL": os.environ["DATABASE_URL"]}
    for script in ("evaluate_phase3_pilot_rehearsal.py", "evaluate_phase4_release_candidate.py"):
        subprocess.run([sys.executable, str(root / script)], check=True, env=environment)
    print('{"suite":"phase4_internal_alpha","status":"passed"}')


if __name__ == "__main__":
    main()
