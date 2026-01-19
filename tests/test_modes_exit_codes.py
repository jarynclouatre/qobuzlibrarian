"""Mode-level exits honour the documented exit-code contract.

The CLI advertises EXIT_AUTH=2 / EXIT_LOCK_BUSY=3 / EXIT_TRANSIENT=4 /
EXIT_CONFIG=64 in --help so cron wrappers can branch on transient vs.
permanent failures. Each mode entry point must route through die() with
the right code, not a generic sys.exit(1).
"""
import types
from unittest.mock import patch

import pytest

from qobuz_fetch.api.auth import AuthLost
from qobuz_fetch.modes.album import run_album_mode
from qobuz_fetch.ui_cli.errors import EXIT_AUTH


def test_album_mode_auth_lost_exits_with_auth_code():
    args = types.SimpleNamespace(
        query=["x"], dry_run=False, force=False, yes=False,
        no_import=False, verbose=False, consolidate=False,
        no_upgrade=False, prefer_hires=False, no_compress=False,
        include_singles=False, auto_safe=False, upgrade_walk=False,
    )

    with patch("qobuz_fetch.modes.album.resolve_album_from_args",
               side_effect=AuthLost("401 from test")), \
         patch("qobuz_fetch.modes.album.clear_scan_caches"):
        with pytest.raises(SystemExit) as exc:
            run_album_mode(args, "tok", query_args=["x"], loop=False)

    assert exc.value.code == EXIT_AUTH
