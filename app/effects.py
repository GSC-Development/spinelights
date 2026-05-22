"""Server-side effect engine for app-driven lighting animations.

Effects are background threads that push /api/override on a tick (every
~150-250 ms). Only one effect runs at a time; starting a new effect stops
the previous and firing any direct control also stops the effect.

Available effects:
  - rainbow    - hue rotation, optionally chasing across the 17 spine fixtures
  - crossfade  - smooth cycle through a colour palette, optionally chasing
  - two_color  - alternates between two user-picked colours, optionally chasing

"Together" mode targets group 0 (one /api/override per tick, whole building
moves as one colour). "Chase" mode targets each of the 17 fixtures with a
different colour per tick so the pattern visibly travels along the spine.

Per-fixture pushes are run in parallel via a small thread pool so a 17-fixture
update completes in a handful of network round trips.
"""
from __future__ import annotations

import colorsys
import logging
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from app.tpc import TPCClient, TPCError

logger = logging.getLogger(__name__)


# The spine has 17 individually-addressable fixtures (verified via probe:
# PUT /api/override target=fixture num=1..17 → 204, num=18 → 400).
NUM_FIXTURES = 17

# Hand-picked palettes used by the crossfade effect.
PALETTES: dict[str, list[str]] = {
    "rainbow":  ["#dc2626", "#f97316", "#facc15", "#22c55e", "#06b6d4", "#3b82f6", "#7c3aed", "#ec4899"],
    "warm":     ["#dc2626", "#f97316", "#facc15", "#fef3c7"],
    "cool":     ["#06b6d4", "#3b82f6", "#1e3a8a", "#7c3aed"],
    "pastels":  ["#fecaca", "#fde68a", "#bbf7d0", "#bfdbfe", "#ddd6fe", "#fbcfe8"],
    "fire":     ["#7f1d1d", "#dc2626", "#f97316", "#facc15"],
    "ocean":    ["#0c4a6e", "#0ea5e9", "#22d3ee", "#a5f3fc"],
}


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


# Parallelise per-fixture pushes. 17 sequential PUTs at 5-10 ms each would eat
# most of a 200 ms tick budget; with 6 workers a full frame lands in ~30 ms.
_FIXTURE_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="fx-push")


def _push_fixture(tpc: TPCClient, n: int, rgb: tuple[int, int, int],
                  *, intensity: int, fade: float) -> None:
    try:
        r, g, b = rgb
        tpc.set_override_color(r, g, b, target="fixture", num=n,
                               intensity=intensity, fade_seconds=fade)
    except TPCError:
        pass  # transient — next frame will retry


def _push_all_fixtures(tpc: TPCClient, colours: list[tuple[int, int, int]],
                       *, intensity: int = 255, fade: float = 0.15) -> None:
    """Push one RGB per fixture (colours[i] is fixture i+1), in parallel."""
    futures = [
        _FIXTURE_POOL.submit(_push_fixture, tpc, i + 1, colours[i],
                             intensity=intensity, fade=fade)
        for i in range(min(NUM_FIXTURES, len(colours)))
    ]
    for f in futures:
        try:
            f.result(timeout=1.5)
        except Exception:
            pass


@dataclass
class ActiveEffect:
    name: str
    params: dict
    started_at: datetime
    swatch: str    # representative hex colour shown on the live panel
    label: str     # display label, e.g. "Two-colour chase — RED / GREEN"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class EffectEngine:
    """Thread-based effect runner. One effect at a time.

    `current` is an `ActiveEffect` when running, `None` when idle. Read from
    request threads (dashboard polling) and written by the effect thread, so
    guarded by a lock.
    """

    def __init__(self, tpc: TPCClient) -> None:
        self._tpc = tpc
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.current: ActiveEffect | None = None

    def is_running(self) -> bool:
        return self.current is not None

    def start(self, name: str, params: dict) -> ActiveEffect:
        runner = _RUNNERS.get(name)
        if runner is None:
            raise ValueError(f"unknown effect: {name}")
        self.stop()
        self._stop.clear()
        active = _build_active(name, params)
        with self._lock:
            self.current = active
        self._thread = threading.Thread(
            target=self._loop, args=(runner, params),
            daemon=True, name=f"effect-{name}",
        )
        self._thread.start()
        logger.info("effect started: %s params=%s", name, params)
        return active

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        with self._lock:
            was = self.current
            self.current = None
        if was is not None:
            # Clear EVERYTHING — group AND each fixture — so the next action
            # starts from a known empty state. Without scoped per-fixture
            # clears, leftover chase colours would shadow subsequent group-0
            # pushes (per-fixture overrides win over per-group on Pharos).
            try:
                self._tpc.clear_overrides(fade_seconds=0.3, num_fixtures=NUM_FIXTURES)
            except TPCError:
                logger.warning("clear_overrides after effect stop failed")
            logger.info("effect stopped: %s", was.name)

    def _loop(self, runner: Callable, params: dict) -> None:
        try:
            runner(self._tpc, self._stop, params)
        except Exception:
            logger.exception("effect runner crashed")
        finally:
            with self._lock:
                self.current = None


def describe_effect(name: str, params: dict) -> tuple[str, str]:
    """Return (representative_swatch_hex, display_label) for an effect spec.

    Used both for live effect status (ActiveEffect.swatch/label) and for
    scheduled overrides that fire effects (Override.swatch / display label).
    """
    chase = " (chase)" if params.get("chase") else ""
    if name == "rainbow":
        return ("#ec4899", f"Rainbow{chase}")
    if name == "crossfade":
        pal_name = params.get("palette", "rainbow")
        pal = PALETTES.get(pal_name, PALETTES["rainbow"])
        return (pal[0], f"Crossfade — {pal_name}{chase}")
    if name == "two_color":
        a = params.get("color_a", "#dc2626")
        b = params.get("color_b", "#22c55e")
        return (a, f"Two colour{chase} — {a.upper()} / {b.upper()}")
    return ("#7c3aed", name.title())


def _build_active(name: str, params: dict) -> ActiveEffect:
    sw, lbl = describe_effect(name, params)
    return ActiveEffect(name, params, datetime.now(timezone.utc), sw, lbl)


# ---------------------------------------------------------------------------
# Effect runners — each runs in its own thread until stop is set.
# ---------------------------------------------------------------------------


def _rainbow(tpc: TPCClient, stop: threading.Event, params: dict) -> None:
    speed = float(params.get("speed", 2.5))   # degrees the rainbow advances per tick
    chase = bool(params.get("chase", False))

    if not chase:
        # Together mode — single colour on group 0, whole building shifts.
        hue_deg = 0.0
        while not stop.is_set():
            r, g, b = _hsv_to_rgb(hue_deg / 360.0, 1.0, 1.0)
            try:
                tpc.set_override_color(r, g, b, intensity=255, fade_seconds=0.2)
            except TPCError:
                pass
            hue_deg = (hue_deg + speed) % 360.0
            stop.wait(0.15)
        return

    # Chase mode — each fixture gets a hue offset so the rainbow visibly
    # travels along the spine. One full rainbow span across all 17 fixtures.
    base = 0.0
    step = 360.0 / NUM_FIXTURES
    while not stop.is_set():
        colours = [
            _hsv_to_rgb(((base + i * step) % 360) / 360.0, 1.0, 1.0)
            for i in range(NUM_FIXTURES)
        ]
        _push_all_fixtures(tpc, colours, intensity=255, fade=0.2)
        base = (base + speed) % 360.0
        stop.wait(0.2)


def _crossfade(tpc: TPCClient, stop: threading.Event, params: dict) -> None:
    palette_name = params.get("palette", "rainbow")
    palette = PALETTES.get(palette_name, PALETTES["rainbow"])
    dwell = float(params.get("dwell", 4.0))
    fade = float(params.get("fade", 1.8))
    chase = bool(params.get("chase", False))

    if not chase:
        # Whole building together.
        idx = 0
        while not stop.is_set():
            r0, g0, b0 = _hex_to_rgb(palette[idx % len(palette)])
            try:
                tpc.set_override_color(r0, g0, b0, intensity=255, fade_seconds=fade)
            except TPCError:
                pass
            idx += 1
            stop.wait(dwell)
        return

    # Chase mode — fixtures show consecutive palette entries; on each tick the
    # pattern shifts by one so colours roll along the spine.
    offset = 0
    pal_rgb = [_hex_to_rgb(c) for c in palette]
    pal_len = len(pal_rgb)
    # In chase mode dwell is shorter (it's "how fast the wave shifts").
    chase_tick = max(0.2, dwell / 6.0)
    chase_fade = min(fade, chase_tick * 2.0)
    while not stop.is_set():
        colours = [pal_rgb[(offset + i) % pal_len] for i in range(NUM_FIXTURES)]
        _push_all_fixtures(tpc, colours, intensity=255, fade=chase_fade)
        offset += 1
        stop.wait(chase_tick)


def _two_color(tpc: TPCClient, stop: threading.Event, params: dict) -> None:
    a_rgb = _hex_to_rgb(params.get("color_a", "#dc2626"))
    b_rgb = _hex_to_rgb(params.get("color_b", "#22c55e"))
    chase = bool(params.get("chase", False))

    if not chase:
        # Whole building alternates between the two colours with a slow fade.
        dwell = float(params.get("dwell", 4.0))
        fade = float(params.get("fade", 1.2))
        show_a = True
        while not stop.is_set():
            r, g, b = a_rgb if show_a else b_rgb
            try:
                tpc.set_override_color(r, g, b, intensity=255, fade_seconds=fade)
            except TPCError:
                pass
            show_a = not show_a
            stop.wait(dwell)
        return

    # Chase mode — barber-pole: even fixtures = A, odd = B, swap pattern each
    # tick so colours march along.
    tick = float(params.get("tick", 0.5))  # seconds per pattern shift
    offset = 0
    while not stop.is_set():
        colours = [a_rgb if (i + offset) % 2 == 0 else b_rgb for i in range(NUM_FIXTURES)]
        _push_all_fixtures(tpc, colours, intensity=255, fade=min(0.2, tick * 0.4))
        offset += 1
        stop.wait(tick)


_RUNNERS: dict[str, Callable] = {
    "rainbow":   _rainbow,
    "crossfade": _crossfade,
    "two_color": _two_color,
}
