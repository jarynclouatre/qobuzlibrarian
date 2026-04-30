"""Downsample-hook wrapper.

The script that does the actual work is shipped as `compress.py` for
historical reasons — the feature itself is hi-res downsampling, not
lossless compression. In the Docker image the script lives at
/app/compress.py; in a dev checkout it's at <repo>/scripts/compress.py.
We probe both.

Imported best-effort: if the script can't be found, the downsample step
is silently skipped rather than aborting the pipeline. HAVE_DOWNSAMPLE
is the gate the rest of the app checks.
"""
import os
import sys
from pathlib import Path

HAVE_DOWNSAMPLE = False
downsample_dir = None  # type: ignore

# Probe likely script locations in priority order:
#   1. /app  — Dockerfile COPYs compress.py here (container only).
#   2. <repo>/scripts — dev checkout via `pip install -e .`.
_HERE = Path(__file__).resolve()
_candidates = []
if os.environ.get("QL_IN_CONTAINER"):
    _candidates.append(Path("/app"))
_candidates.append(_HERE.parents[3] / "scripts")

for _dir in _candidates:
    if (_dir / "compress.py").exists():
        if str(_dir) not in sys.path:
            sys.path.insert(0, str(_dir))
        try:
            from compress import compress_dir as downsample_dir  # type: ignore[import]
            HAVE_DOWNSAMPLE = True
            break
        except Exception:
            downsample_dir = None  # type: ignore
            continue
