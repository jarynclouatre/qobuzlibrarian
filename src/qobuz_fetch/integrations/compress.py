"""Compression-hook wrapper.

compress.py is a standalone script shipped with the project. In the
Docker image it lives at /app/compress.py (next to the entrypoint); in
a dev checkout it lives at <repo>/scripts/compress.py. We probe both.

Imported best-effort: if the script can't be found, compression is
silently skipped rather than aborting the pipeline. HAVE_COMPRESS is
the gate the rest of the code checks.
"""
import os
import sys
from pathlib import Path

HAVE_COMPRESS = False
compress_dir = None  # type: ignore

# Probe likely script locations in priority order:
#   1. /app  — Dockerfile COPYs compress.py here (container only).
#   2. <repo>/scripts — dev checkout via `pip install -e .`.
_HERE = Path(__file__).resolve()
_candidates = []
if os.environ.get("QF_IN_CONTAINER"):
    _candidates.append(Path("/app"))
_candidates.append(_HERE.parents[3] / "scripts")

for _dir in _candidates:
    if (_dir / "compress.py").exists():
        if str(_dir) not in sys.path:
            sys.path.insert(0, str(_dir))
        try:
            from compress import compress_dir  # type: ignore[import]
            HAVE_COMPRESS = True
            break
        except Exception:
            compress_dir = None  # type: ignore
            continue
