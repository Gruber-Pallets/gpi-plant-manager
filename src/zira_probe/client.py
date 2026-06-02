"""Thin HTTP client for the Zira.us public API.

Exposes one method per documented endpoint plus a generic `request()` method
used by the undocumented-probing suite.
"""

from __future__ import annotations

import threading
from typing import Any

import requests


class ZiraClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.zira.us/public/",
        timeout_seconds: float = 15.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout_seconds = timeout_seconds
        # requests.Session is not safe for concurrent use, and leaderboard()
        # fans get_readings() across a ThreadPoolExecutor. Give each thread its
        # own Session (carrying the API-key header) so they never share one
        # pooled connection and corrupt it.
        self._local = threading.local()

    @property
    def session(self) -> requests.Session:
        """Per-thread requests.Session, created on first use per thread."""
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"X-API-Key": self.api_key})
            self._local.session = s
        return s

    def _url(self, path: str) -> str:
        return self.base_url + path.lstrip("/")

    def get_readings(
        self,
        meter_id: str,
        end_time: str,
        start_time: str | None = None,
        limit: int | None = None,
        last_value: str | None = None,
    ) -> Any:
        params: dict[str, str] = {"meterId": meter_id, "endTime": end_time}
        if start_time is not None:
            params["startTime"] = start_time
        if limit is not None:
            params["limit"] = str(limit)
        if last_value is not None:
            params["lastValue"] = last_value

        resp = self.session.get(
            self._url("reading"), params=params, timeout=self.timeout_seconds
        )
        resp.raise_for_status()
        return resp.json()

    def get_channel_analysis(
        self,
        channel_id: str,
        interval: str,
        from_time: str,
        to_time: str,
    ) -> Any:
        params = {"interval": interval, "fromTime": from_time, "toTime": to_time}
        resp = self.session.get(
            self._url(f"channels/{channel_id}/analysis"),
            params=params,
            timeout=self.timeout_seconds,
        )
        resp.raise_for_status()
        return resp.json()

    def add_readings(self, readings: list[dict]) -> Any:
        resp = self.session.post(
            self._url("reading/ids/"),
            json=readings,
            timeout=self.timeout_seconds,
        )
        resp.raise_for_status()
        return resp.json()

    def request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: Any = None,
        timeout_seconds: float | None = None,
    ) -> requests.Response:
        """Generic request for undocumented-endpoint probing.

        Never raises on 4xx/5xx — returns the response so probes can record
        the exact error shape.
        """

        return self.session.request(
            method=method.upper(),
            url=self._url(path),
            params=params,
            json=json_body,
            timeout=timeout_seconds or self.timeout_seconds,
        )
