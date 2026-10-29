"""Shared HTTP session: descriptive User-Agent, 6h response cache, robots.txt, per-host pacing."""

import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests
import requests_cache


class RobotsDisallowed(Exception):
    pass


class Http:
    def __init__(
        self,
        user_agent: str,
        cache_path: Path | None = None,
        ttl_hours: float = 6,
        min_interval_s: float = 1.0,
        host_intervals: dict[str, float] | None = None,
        timeout_s: float = 30,
        robots_exempt_hosts: set[str] | None = None,
    ):
        if cache_path is None:
            self.session = requests.Session()
        else:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.session = requests_cache.CachedSession(
                str(cache_path), expire_after=ttl_hours * 3600, allowable_codes=(200,)
            )
        self.session.headers["User-Agent"] = user_agent
        self.user_agent = user_agent
        self.min_interval_s = min_interval_s
        self.host_intervals = host_intervals or {}
        self.timeout_s = timeout_s
        self.robots_exempt_hosts = robots_exempt_hosts or set()
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}

    def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.netloc in self.robots_exempt_hosts:
            return True
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser = RobotFileParser()
            try:
                resp = self.session.get(f"{origin}/robots.txt", timeout=self.timeout_s)
                # A missing robots.txt means everything is allowed.
                parser.parse(resp.text.splitlines() if resp.status_code == 200 else [])
            except requests.RequestException:
                parser.parse([])
            self._robots[origin] = parser
        return self._robots[origin].can_fetch(self.user_agent, url)

    def _pace(self, host: str) -> None:
        interval = self.host_intervals.get(host, self.min_interval_s)
        wait = self._last_hit.get(host, 0) + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def get(self, url: str, **kwargs) -> requests.Response:
        if not self._allowed(url):
            raise RobotsDisallowed(url)
        resp_from_cache = False
        if isinstance(self.session, requests_cache.CachedSession):
            resp_from_cache = self.session.cache.contains(url=url)
        if not resp_from_cache:
            self._pace(urlsplit(url).netloc)
        resp = self.session.get(url, timeout=self.timeout_s, **kwargs)
        resp.raise_for_status()
        return resp
