"""Tests for qobuz_librarian.api.client — status handling, retry/backoff, UA."""
from unittest.mock import MagicMock, patch

import pytest
import requests

from qobuz_librarian.api.auth import AuthLost, QobuzError
from qobuz_librarian.api.client import qobuz_get, validate_token


def _response(status_code=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data or {}
    r.text = text
    r.headers = {}
    return r


def test_qobuz_get_maps_status_codes():
    # 200 → parsed JSON; 401 → AuthLost (so creds get torn down); network
    # failure → QobuzError (retryable / surfaced as a friendly message).
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.return_value = _response(200, {"albums": {"items": []}})
        assert qobuz_get("album/search", {"query": "x"}, "tok") == {"albums": {"items": []}}
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.return_value = _response(401)
        with pytest.raises(AuthLost):
            qobuz_get("album/search", {}, "bad")
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.side_effect = requests.RequestException("timeout")
        with pytest.raises(QobuzError):
            qobuz_get("album/search", {}, "tok")


def test_qobuz_get_retries_429_but_not_404():
    # 429 backs off and retries; a 404 is terminal and must not be retried.
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.side_effect = [_response(429), _response(429),
                                             _response(200, {"ok": True})]
        assert qobuz_get("album/search", {}, "tok") == {"ok": True}
    with patch("qobuz_librarian.api.client._get_session") as sess:
        sess.return_value.get.return_value = _response(404, text="missing")
        with pytest.raises(QobuzError):
            qobuz_get("album/get", {"album_id": "nope"}, "tok")
        assert sess.return_value.get.call_count == 1


def test_validate_token_exits_on_authlost_passes_otherwise():
    with patch("qobuz_librarian.api.client.qobuz_get", side_effect=AuthLost("401")):
        with pytest.raises(SystemExit):
            validate_token("bad")
    with patch("qobuz_librarian.api.client.qobuz_get", return_value={}):
        assert validate_token("good") is None


def test_user_agent_carries_installed_version():
    from importlib.metadata import version

    from qobuz_librarian.api.client import _get_session
    ua = _get_session().headers["User-Agent"]
    assert version("qobuz-librarian") in ua and "qobuz-librarian" in ua
