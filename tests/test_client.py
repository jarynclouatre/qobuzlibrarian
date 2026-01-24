"""Tests for qobuz_fetch.api.client"""
from unittest.mock import MagicMock, patch

import pytest
import requests

from qobuz_fetch.api.auth import AuthLost, QobuzError
from qobuz_fetch.api.client import qobuz_get, validate_token


class TestQobuzGet:
    def _make_response(self, status_code=200, json_data=None, text=""):
        mock_r = MagicMock()
        mock_r.status_code = status_code
        mock_r.json.return_value = json_data or {}
        mock_r.text = text
        return mock_r

    def test_returns_json_on_200(self):
        mock_r = self._make_response(200, {"albums": {"items": []}})
        with patch("qobuz_fetch.api.client._session") as mock_session:
            mock_session.get.return_value = mock_r
            result = qobuz_get("album/search", {"query": "test"}, "tok")
        assert result == {"albums": {"items": []}}

    def test_raises_authlost_on_401(self):
        mock_r = self._make_response(401)
        with patch("qobuz_fetch.api.client._session") as mock_session:
            mock_session.get.return_value = mock_r
            with pytest.raises(AuthLost):
                qobuz_get("album/search", {}, "bad_token")

    def test_raises_qobuzerror_on_network_failure(self):
        with patch("qobuz_fetch.api.client._session") as mock_session:
            mock_session.get.side_effect = requests.RequestException("timeout")
            with pytest.raises(QobuzError):
                qobuz_get("album/search", {}, "tok")

    def test_retries_on_429_then_succeeds(self):
        from qobuz_fetch.api import client as _client
        rate_limited = self._make_response(429)
        rate_limited.headers = {}
        ok = self._make_response(200, {"ok": True})
        with patch("qobuz_fetch.api.client._session") as mock_session:
            mock_session.get.side_effect = [rate_limited, rate_limited, ok]
            result = _client.qobuz_get("album/search", {}, "tok")
        assert result == {"ok": True}

    def test_does_not_retry_on_404(self):
        from qobuz_fetch.api import client as _client
        bad = self._make_response(404, text="missing")
        with patch("qobuz_fetch.api.client._session") as mock_session:
            mock_session.get.return_value = bad
            with pytest.raises(QobuzError):
                _client.qobuz_get("album/get", {"album_id": "nope"}, "tok")
        assert mock_session.get.call_count == 1


class TestValidateToken:
    def test_exits_on_authlost(self):
        with patch("qobuz_fetch.api.client.qobuz_get", side_effect=AuthLost("401")):
            with pytest.raises(SystemExit):
                validate_token("bad_token")

    def test_passes_on_success(self):
        with patch("qobuz_fetch.api.client.qobuz_get", return_value={}):
            assert validate_token("good_token") is None


class TestUserAgent:
    def test_user_agent_carries_installed_package_version(self):
        """The session UA must reflect the running build, not a hard-coded
        version string — otherwise the UA silently lies after a bump."""
        from importlib.metadata import version

        from qobuz_fetch.api.client import _session

        ua = _session.headers["User-Agent"]
        installed = version("qobuz-librarian")
        assert installed in ua, f"UA {ua!r} doesn't mention installed version {installed!r}"
        assert "qobuz-librarian" in ua


class TestConcurrentAccess:
    def test_concurrent_calls_serialize_through_lock(self):
        """The shared requests.Session is wrapped in a lock so the web app's
        run_in_executor fan-out can't corrupt urllib3/cookie state on the
        Session. Spawn enough threads to expose a missing lock and assert
        every call completes."""
        import threading
        from concurrent.futures import ThreadPoolExecutor

        mock_r = MagicMock()
        mock_r.status_code = 200
        mock_r.json.return_value = {"ok": True}

        in_flight = []
        max_in_flight = [0]
        observed_lock = threading.Lock()

        def _tracking_get(*args, **kwargs):
            with observed_lock:
                in_flight.append(1)
                max_in_flight[0] = max(max_in_flight[0], sum(in_flight))
            try:
                return mock_r
            finally:
                with observed_lock:
                    in_flight.pop()

        with patch("qobuz_fetch.api.client._session") as mock_session:
            mock_session.get.side_effect = _tracking_get
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(qobuz_get, "album/get", {"id": i}, "tok")
                           for i in range(32)]
                results = [f.result(timeout=5) for f in futures]
        assert all(r == {"ok": True} for r in results)
        assert max_in_flight[0] == 1
