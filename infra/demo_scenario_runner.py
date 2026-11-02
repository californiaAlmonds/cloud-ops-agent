#!/usr/bin/env python3
"""Unified CLI controller for all 3 Cloud Ops Agent demo microservices.

Delegates to each service's `scripts/scenario.py` so you can list, inspect,
simulate locally, or apply/reset ECS task definition fault scenarios across
`salescreate-api`, `inventory-worker`, and `notifier` from the workspace root.

Examples:
    python infra/demo_scenario_runner.py list
    python infra/demo_scenario_runner.py simulate salescreate-api bad-release --ticks 5
    python infra/demo_scenario_runner.py apply salescreate-api bad-release --dry-run
    python infra/demo_scenario_runner.py reset-all --dry-run
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SERVICES = ("salescreate-api", "inventory-worker", "notifier")


def run_service_scenario_cli(service: str, cli_args: list[str]) -> int:
    root_dir = Path(__file__).resolve().parent.parent
    script = root_dir / "services" / service / "scripts" / "scenario.py"
    if not script.exists():
        print(f"ERROR: Scenario script not found at {script}", file=sys.stderr)
        return 1
    proc = subprocess.run(
        [sys.executable, str(script), *cli_args],
        cwd=str(script.parent.parent),
        check=False,
    )
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Unified fault scenario controller for Cloud Ops Agent demo services."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = sub.add_parser("list", help="List available scenarios for one or all services")
    p_list.add_argument("--service", choices=SERVICES, help="Filter to a single service")

    # inspect
    p_inspect = sub.add_parser("inspect", help="Inspect a specific service scenario")
    p_inspect.add_argument("service", choices=SERVICES)
    p_inspect.add_argument("scenario")

    # simulate
    p_sim = sub.add_parser("simulate", help="Simulate JSON log output for a service scenario")
    p_sim.add_argument("service", choices=SERVICES)
    p_sim.add_argument("scenario")
    p_sim.add_argument("--ticks", type=int, default=5)

    # apply
    p_apply = sub.add_parser("apply", help="Apply a fault scenario to an AWS ECS service")
    p_apply.add_argument("service", choices=SERVICES)
    p_apply.add_argument("scenario")
    p_apply.add_argument("--cluster", default="cloud-ops-demo-cluster")
    p_apply.add_argument("--region", default="us-east-1")
    p_apply.add_argument("--dry-run", action="store_true")

    # reset-all
    p_reset = sub.add_parser("reset-all", help="Reset all 3 services back to their healthy baseline")
    p_reset.add_argument("--cluster", default="cloud-ops-demo-cluster")
    p_reset.add_argument("--region", default="us-east-1")
    p_reset.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.command == "list":
        targets = [args.service] if args.service else list(SERVICES)
        rc = 0
        for svc in targets:
            print(f"\n==================== {svc} ====================")
            rc |= run_service_scenario_cli(svc, ["list"])
        return rc

    if args.command == "inspect":
        return run_service_scenario_cli(args.service, ["inspect", args.scenario])

    if args.command == "simulate":
        return run_service_scenario_cli(
            args.service, ["simulate", args.scenario, "--ticks", str(args.ticks)]
        )

    if args.command == "apply":
        cli = ["apply", args.scenario, "--cluster", args.cluster, "--region", args.region]
        if args.dry_run:
            cli.append("--dry-run")
        return run_service_scenario_cli(args.service, cli)

    if args.command == "reset-all":
        rc = 0
        for svc in SERVICES:
            print(f"\n--- Resetting {svc} to healthy ---")
            cli = ["reset", "--cluster", args.cluster, "--region", args.region]
            if args.dry_run:
                cli.append("--dry-run")
            rc |= run_service_scenario_cli(svc, cli)
        return rc

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
