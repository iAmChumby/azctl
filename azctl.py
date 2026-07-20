#!/usr/bin/env python3
"""azctl — a single-file terminal dashboard for the Azurite storage emulator.

Behavioral contract: BEHAVIOR.md in this repository.

This is the initial scaffold: a read-only ``status`` snapshot with zero
dependencies. The full dashboard lands on top of this skeleton.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys

SERVICES = {
    "blob": 10000,
    "queue": 10001,
    "table": 10002,
}


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def snapshot(host: str = "127.0.0.1") -> dict:
    return {
        name: {
            "port": port,
            "state": "port in use" if port_open(host, port) else "stopped",
        }
        for name, port in SERVICES.items()
    }


def cmd_status(args: argparse.Namespace) -> int:
    snap = snapshot(args.host)
    if args.json:
        print(json.dumps(snap, indent=2))
        return 0
    for name, info in snap.items():
        print(f"{name:<6} {info['state']:<12} port {info['port']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="azctl", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    sub = parser.add_subparsers(dest="command")
    status = sub.add_parser("status", help="one read-only snapshot, then exit")
    status.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "status":
        return cmd_status(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
