"""End-to-end smoke test of the TPC client against the real controller.

Reads creds from .env (or environment). Safe to run any time:

    python scripts/smoke_test_tpc.py

The default run does NOT change any lights — it only reads state and probes the
trigger endpoint with an invalid trigger number (9999) which the TPC rejects
with HTTP 400. To actually fire a real trigger end-to-end, pass --fire NUM:

    python scripts/smoke_test_tpc.py --fire 14

WARNING: --fire WILL change the lights. Only do this when the building is
unobserved or you have cleared it with someone who cares.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running this script directly without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.tpc import (  # noqa: E402
    TPCAuthError,
    TPCClient,
    TPCError,
    TPCInvalidRequest,
    TPCNotReachable,
)


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fire",
        type=int,
        default=None,
        metavar="NUM",
        help="Fire this real trigger number (WILL change the lights).",
    )
    args = parser.parse_args()

    settings = get_settings()
    print(f"TPC base URL: {settings.tpc_base_url}")
    print(f"User:         {settings.tpc_username}")
    print(f"Verify SSL:   {settings.tpc_verify_ssl}")

    if not settings.tpc_password:
        print("\nERROR: TPC_PASSWORD is not set. Copy .env.example to .env and fill it in.")
        return 2

    failures: list[str] = []

    with TPCClient(
        base_url=settings.tpc_base_url,
        username=settings.tpc_username,
        password=settings.tpc_password,
        verify_ssl=settings.tpc_verify_ssl,
        timeout_seconds=settings.tpc_request_timeout_seconds,
    ) as tpc:
        # 1. System info (unauthenticated)
        section("1. /api/system (unauthenticated)")
        try:
            sysinfo = tpc.get_system()
            print(f"  Hardware:    {sysinfo.hardware_type} (serial {sysinfo.serial_number})")
            print(f"  Firmware:    {sysinfo.firmware_version}")
            print(f"  IP:          {sysinfo.ip_address}")
            print(f"  Memory used: {sysinfo.memory_used_kb} KB")
            print(f"  Last boot:   {sysinfo.last_boot_time} ({sysinfo.reset_reason})")
        except TPCError as e:
            failures.append(f"get_system: {e}")
            print(f"  FAIL: {e}")

        # 2. Authentication probe (forces login by calling an authed endpoint)
        section("2. Authentication + /api/timeline")
        try:
            timelines = tpc.list_timelines()
            print(f"  Logged in OK. {len(timelines)} timeline(s) loaded.")
            for t in timelines:
                marker = " <-- ACTIVE" if t.is_active else ""
                print(
                    f"    #{t.num:3d} {t.name!r:20s} state={t.state:8s} "
                    f"prio={t.priority} onstage={t.onstage}{marker}"
                )
            active = tpc.active_timeline()
            if active:
                print(f"  Currently active: #{active.num} {active.name!r} ({active.state})")
            else:
                print("  Nothing currently playing.")
        except TPCAuthError as e:
            failures.append(f"auth: {e}")
            print(f"  FAIL: {e}")
        except TPCError as e:
            failures.append(f"list_timelines: {e}")
            print(f"  FAIL: {e}")

        # 3. List triggers (the existing project schedule)
        section("3. /api/trigger (list)")
        try:
            triggers = tpc.list_triggers()
            print(f"  Project defines {len(triggers)} triggers.")
            real_time = [t for t in triggers if t.type == "Real Time"]
            print(f"  Of those, {len(real_time)} are Real Time (scheduled).")
            for t in sorted(real_time, key=lambda x: x.trigger_text):
                action = t.actions[0] if t.actions else "(no action)"
                label = t.name or "(unnamed)"
                print(f"    #{t.num:3d} {t.trigger_text:30s} -> {action:40s} [{label}]")
        except TPCError as e:
            failures.append(f"list_triggers: {e}")
            print(f"  FAIL: {e}")

        # 4. Safe probe: invalid trigger number
        section("4. POST /api/trigger with invalid number 9999 (safe)")
        try:
            tpc.fire_trigger(9999)
            failures.append("fire_trigger(9999): expected TPCInvalidRequest, got success")
            print("  UNEXPECTED: TPC accepted trigger 9999.")
        except TPCInvalidRequest as e:
            print(f"  OK (expected rejection): {e}")
        except TPCError as e:
            failures.append(f"fire_trigger(9999): {e}")
            print(f"  FAIL: {e}")

        # 5. Optionally fire a real trigger
        if args.fire is not None:
            section(f"5. POST /api/trigger num={args.fire} (LIVE)")
            print(f"  Firing trigger {args.fire} for real...")
            try:
                tpc.fire_trigger(args.fire)
                print(f"  Sent. Check the building.")
            except TPCError as e:
                failures.append(f"fire_trigger({args.fire}): {e}")
                print(f"  FAIL: {e}")
        else:
            section("5. Live trigger fire")
            print("  Skipped. Pass --fire NUM to fire a real trigger.")

    # Summary
    section("Summary")
    if not failures:
        print("  All probes succeeded.")
        return 0
    for f in failures:
        print(f"  FAIL: {f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())


# pylint reachable check
def _unused() -> tuple[type, ...]:
    return (TPCNotReachable,)
