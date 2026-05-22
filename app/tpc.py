"""HTTP client for the Pharos TPC controller (firmware 2.x, Designer API v6).

Endpoints used (all verified against firmware 2.10 on 21 May 2026):

    POST /authenticate           form-encoded username/password -> {"token": <jwt>}
    GET  /api/system             system info, no auth required
    GET  /api/trigger            list of triggers in the loaded project (no auth required)
    POST /api/trigger            fire trigger, body {"num": N}, Bearer auth required
    GET  /api/timeline           currently-loaded timelines + state, Bearer auth required

Authentication notes:

* The TPC issues a JWT in the response body on /authenticate (no cookie).
* Every authenticated response *also* contains a refreshed "token" field. We
  rotate to the latest token on each call so the session never ages out while
  in use, but we also re-login on 401 as a safety net.
* The JWT payload includes an access bitmask: Status=1, Control=2. The
  scheduler app needs Control+Status (3).

Threading: APScheduler fires jobs from worker threads. Token access is guarded
by a lock.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TPCError(Exception):
    """Base class for all TPC client errors."""


class TPCAuthError(TPCError):
    """Authentication failed or insufficient permissions."""


class TPCNotReachable(TPCError):
    """Controller did not respond (network down, TPC offline, etc.)."""


class TPCInvalidRequest(TPCError):
    """The TPC returned 4xx for a malformed request (e.g. invalid trigger number)."""


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SystemInfo:
    hardware_type: str
    serial_number: str
    firmware_version: str
    bootloader_version: str
    ip_address: str
    channel_capacity: int
    memory_used_kb: int
    memory_available_kb: int
    last_boot_time: str
    reset_reason: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SystemInfo:
        return cls(
            hardware_type=d.get("hardware_type", ""),
            serial_number=d.get("serial_number", ""),
            firmware_version=d.get("firmware_version", ""),
            bootloader_version=d.get("bootloader_version", ""),
            ip_address=d.get("ip_address", ""),
            channel_capacity=int(d.get("channel_capacity", 0) or 0),
            memory_used_kb=_kb_int(d.get("memory_used")),
            memory_available_kb=_kb_int(d.get("memory_available")),
            last_boot_time=d.get("last_boot_time", ""),
            reset_reason=d.get("reset_reason", ""),
        )


@dataclass(frozen=True)
class Trigger:
    num: int
    name: str
    trigger_text: str
    type: str
    group: str
    description: str
    actions: list[str]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Trigger:
        return cls(
            num=int(d["num"]),
            name=d.get("name", "") or "",
            trigger_text=d.get("trigger_text", "") or "",
            type=d.get("type", "") or "",
            group=d.get("group", "") or "",
            description=d.get("description", "") or "",
            actions=[a.get("text", "") for a in d.get("actions", []) if isinstance(a, dict)],
        )


@dataclass(frozen=True)
class TimelineState:
    num: int
    name: str
    state: str        # "none", "running", "paused", ...
    position: int     # ms into timeline
    length: int       # ms total
    priority: str
    onstage: bool
    group: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TimelineState:
        return cls(
            num=int(d["num"]),
            name=d.get("name", "") or "",
            state=d.get("state", "none") or "none",
            position=int(d.get("position", 0) or 0),
            length=int(d.get("length", 0) or 0),
            priority=d.get("priority", "normal") or "normal",
            onstage=bool(d.get("onstage", False)),
            group=d.get("group", "") or "",
        )

    @property
    def is_active(self) -> bool:
        return self.state.lower() in {"running", "playing", "paused"}


@dataclass(frozen=True)
class SceneState:
    """A scene as reported by GET /api/scene.

    `state` observed values: "started", "released", "none". `onstage` is the
    authoritative "this scene is currently driving the output" flag.
    """

    num: int
    name: str
    state: str
    onstage: bool
    group: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SceneState:
        return cls(
            num=int(d["num"]),
            name=d.get("name", "") or "",
            state=d.get("state", "none") or "none",
            onstage=bool(d.get("onstage", False)),
            group=d.get("group", "") or "",
        )

    @property
    def is_live(self) -> bool:
        return self.onstage or self.state.lower() in {"started", "running", "playing"}


def _kb_int(raw: Any) -> int:
    """Convert '42792 KB' -> 42792, gracefully handling None."""
    if raw is None:
        return 0
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw).split()[0])
    except (ValueError, IndexError):
        return 0


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class TPCClient:
    """Thread-safe synchronous client for the Pharos TPC HTTP API.

    Construct once at app startup, share across threads. APScheduler jobs and
    request handlers both use the same instance.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        verify_ssl: bool = False,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token: str | None = None
        self._token_lock = threading.Lock()
        self._http = httpx.Client(
            verify=verify_ssl,
            timeout=timeout_seconds,
            base_url=self._base_url,
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> TPCClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- token management --------------------------------------------------

    def _login(self) -> str:
        """POST /authenticate, store and return a fresh JWT."""
        try:
            r = self._http.post(
                "/authenticate",
                data={"username": self._username, "password": self._password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.RequestError as e:
            raise TPCNotReachable(f"Could not reach TPC at {self._base_url}: {e}") from e

        if r.status_code == 401:
            raise TPCAuthError("TPC rejected username/password")
        if r.status_code >= 400:
            raise TPCAuthError(f"Login failed: HTTP {r.status_code} {r.text!r}")

        try:
            token = r.json()["token"]
        except (KeyError, ValueError) as e:
            raise TPCAuthError(f"Unexpected login response: {r.text!r}") from e

        with self._token_lock:
            self._token = token
        logger.info("TPC login successful (token=%d chars)", len(token))
        return token

    def _refresh_token_from_response(self, body: dict[str, Any]) -> None:
        """Many TPC responses include a refreshed token. Adopt it if present."""
        new_token = body.get("token")
        if new_token and isinstance(new_token, str):
            with self._token_lock:
                self._token = new_token

    def _current_token(self) -> str:
        with self._token_lock:
            tok = self._token
        if tok is None:
            tok = self._login()
        return tok

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._current_token()}"}

    # -- request helpers ---------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        authed: bool = False,
        retry_on_401: bool = True,
        _conn_retry_done: bool = False,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if json is not None:
            headers["Content-Type"] = "application/json"
        if authed:
            headers.update(self._auth_headers())

        try:
            r = self._http.request(method, path, json=json, headers=headers)
        except httpx.RemoteProtocolError as e:
            # Pharos firmware sometimes closes the keep-alive connection
            # between requests (notably after DELETE). The pool's next request
            # then fails with "Server disconnected". Open a fresh connection
            # by retrying once.
            if not _conn_retry_done:
                logger.debug("TPC dropped connection on %s %s, retrying", method, path)
                return self._request(method, path, json=json, authed=authed,
                                     retry_on_401=retry_on_401, _conn_retry_done=True)
            raise TPCNotReachable(f"TPC dropped connection: {e}") from e
        except httpx.RequestError as e:
            raise TPCNotReachable(f"TPC request failed: {e}") from e

        # 401 -> try one re-login and retry
        if r.status_code == 401 and authed and retry_on_401:
            logger.info("TPC returned 401, re-authenticating")
            with self._token_lock:
                self._token = None
            return self._request(method, path, json=json, authed=True, retry_on_401=False)

        return r

    # -- public API --------------------------------------------------------

    def get_system(self) -> SystemInfo:
        """GET /api/system. No auth required."""
        r = self._request("GET", "/api/system")
        if r.status_code >= 400:
            raise TPCError(f"/api/system returned HTTP {r.status_code}")
        return SystemInfo.from_dict(r.json())

    def list_triggers(self) -> list[Trigger]:
        """GET /api/trigger. Returns all triggers defined in the loaded project."""
        r = self._request("GET", "/api/trigger")
        if r.status_code >= 400:
            raise TPCError(f"/api/trigger GET returned HTTP {r.status_code}")
        body = r.json()
        return [Trigger.from_dict(t) for t in body.get("triggers", [])]

    def fire_trigger(self, num: int) -> None:
        """POST /api/trigger to fire the given trigger number.

        Raises TPCInvalidRequest if the controller rejects the number (e.g.
        trigger does not exist in the loaded project).
        """
        r = self._request("POST", "/api/trigger", json={"num": int(num)}, authed=True)
        if r.status_code == 400:
            try:
                msg = r.json().get("message", r.text)
            except ValueError:
                msg = r.text
            raise TPCInvalidRequest(f"TPC rejected trigger {num}: {msg}")
        if r.status_code >= 400:
            raise TPCError(f"Fire trigger {num} failed: HTTP {r.status_code} {r.text!r}")
        # Refresh token if present
        try:
            self._refresh_token_from_response(r.json())
        except ValueError:
            pass
        logger.info("Fired TPC trigger %d", num)

    def list_timelines(self) -> list[TimelineState]:
        """GET /api/timeline. Returns currently-loaded timelines and their state."""
        r = self._request("GET", "/api/timeline", authed=True)
        if r.status_code >= 400:
            raise TPCError(f"/api/timeline returned HTTP {r.status_code} {r.text!r}")
        body = r.json()
        self._refresh_token_from_response(body)
        return [TimelineState.from_dict(t) for t in body.get("timelines", [])]

    def list_scenes(self) -> list[SceneState]:
        """GET /api/scene. Returns project scenes and their current state."""
        r = self._request("GET", "/api/scene", authed=True)
        if r.status_code >= 400:
            raise TPCError(f"/api/scene returned HTTP {r.status_code} {r.text!r}")
        body = r.json()
        self._refresh_token_from_response(body)
        return [SceneState.from_dict(s) for s in body.get("scenes", [])]

    def active_timeline(self) -> TimelineState | None:
        """Convenience: return the highest-priority currently-active timeline, or None."""
        actives = [t for t in self.list_timelines() if t.is_active]
        if not actives:
            return None
        # Onstage + higher priority first
        order = {"high": 0, "normal": 1, "low": 2}
        actives.sort(key=lambda t: (not t.onstage, order.get(t.priority.lower(), 99)))
        return actives[0]

    def is_reachable(self) -> bool:
        """Cheap unauthenticated health check. Used by the background poller."""
        try:
            self.get_system()
            return True
        except TPCError:
            return False

    # -- Direct colour override ------------------------------------------
    # PUT /api/override sets fixture/group output to an arbitrary RGB at a
    # higher priority than any running scene/timeline. DELETE clears it and
    # the daily schedule resumes immediately.

    def set_override_color(
        self,
        red: int,
        green: int,
        blue: int,
        *,
        target: str = "group",
        num: int = 0,
        intensity: int = 255,
        fade_seconds: float = 0.5,
        path: str = "Linear",
    ) -> None:
        """Set an RGB override on a fixture or group.

        Defaults to target=group num=0 (the project's 'All Fixtures' group) and
        intensity=255 (full). Intensity is REQUIRED and uses the 0-255 scale:
        without it the controller accepts the colour but drives the fixtures
        at 0%, so nothing visible happens once the daily schedule has been
        released. Verified live: 100 ≈ 39% brightness, 255 = full.
        """
        body = {
            "target": target,
            "num": int(num),
            "colour": {
                "red": max(0, min(255, int(red))),
                "green": max(0, min(255, int(green))),
                "blue": max(0, min(255, int(blue))),
            },
            "intensity": max(0, min(255, int(intensity))),
            "fade": float(fade_seconds),
            "path": path,
        }
        r = self._request("PUT", "/api/override", json=body, authed=True)
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text)
            except ValueError:
                msg = r.text
            raise TPCInvalidRequest(f"TPC rejected override: {msg}")
        try:
            self._refresh_token_from_response(r.json())
        except ValueError:
            pass
        logger.info(
            "Set TPC override target=%s num=%d color=(%d,%d,%d) fade=%.2fs",
            target, num, red, green, blue, fade_seconds,
        )

    def clear_overrides(self, *, fade_seconds: float = 0.5, num_fixtures: int = 0) -> None:
        """Clear active /api/override overrides.

        A plain DELETE /api/override (no target) clears the implicit global
        override but LEAVES per-fixture and per-group overrides in place. If
        `num_fixtures` > 0, also iterate fixtures 1..num_fixtures and clear
        each, plus clear group 0. Set this whenever previous activity may
        have written per-fixture overrides (e.g. effect chase modes), or the
        new push will be silently shadowed by the leftover state.
        """
        fade = float(fade_seconds)
        body = {"fade": fade}
        r = self._request("DELETE", "/api/override", json=body, authed=True)
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text)
            except ValueError:
                msg = r.text
            raise TPCError(f"TPC rejected clear-overrides: {msg}")
        try:
            self._refresh_token_from_response(r.json())
        except ValueError:
            pass

        if num_fixtures > 0:
            # Scoped clears. We do them sequentially because this only runs at
            # effect-stop / release, not in any hot loop. ~85 ms on LAN total.
            for n in range(1, num_fixtures + 1):
                self._request("DELETE", "/api/override",
                              json={"target": "fixture", "num": n, "fade": fade},
                              authed=True)
            self._request("DELETE", "/api/override",
                          json={"target": "group", "num": 0, "fade": fade},
                          authed=True)

        logger.info("Cleared TPC overrides (fade=%.2fs, num_fixtures=%d)",
                    fade, num_fixtures)
