#!/usr/bin/env python3
"""Pause (scale to 0) or completely tear down AWS ECS demo resources so nothing keeps billing.

Usage:
    # Scale all 3 services to 0 tasks between coding/demo sessions ($0 compute cost):
    python infra/aws_teardown.py --pause

    # Resume all 3 services to 1 task before your demo:
    python infra/aws_teardown.py --resume

    # Full teardown after the hackathon (deletes services, cluster, log groups, ECR repos):
    python infra/aws_teardown.py --destroy

    # Preview commands without executing:
    python infra/aws_teardown.py --destroy --dry-run
"""

from __future__ import annotations

import argparse
import subprocess

CLUSTER_NAME = "cloud-ops-demo-cluster"
SERVICES = ["salescreate-api", "inventory-worker", "notifier"]


def run_aws(args: list[str], region: str, dry_run: bool) -> None:
    cmd = ["aws", *args, "--region", region]
    if dry_run:
        print(f"[DRY-RUN] {' '.join(cmd)}")
        return
    print(f"Running: {' '.join(cmd)}")
    subprocess.run(cmd, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Pause, resume, or destroy AWS ECS demo resources.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pause", action="store_true", help="Scale all 3 ECS services to 0 tasks")
    group.add_argument("--resume", action="store_true", help="Scale all 3 ECS services back to 1 task")
    group.add_argument("--destroy", action="store_true", help="Delete all demo ECS services, cluster, logs, and ECR repos")
    parser.add_argument("--region", default="us-east-1", help="AWS region (default: us-east-1)")
    parser.add_argument("--dry-run", action="store_true", help="Print AWS CLI commands without executing")
    args = parser.parse_args()

    if args.pause or args.resume:
        target = "0" if args.pause else "1"
        action_label = "Pausing (scaling to 0)" if args.pause else "Resuming (scaling to 1)"
        print(f"=== {action_label} all services in {CLUSTER_NAME} ===")
        for svc in SERVICES:
            run_aws(
                [
                    "ecs",
                    "update-service",
                    "--cluster",
                    CLUSTER_NAME,
                    "--service",
                    svc,
                    "--desired-count",
                    target,
                ],
                region=args.region,
                dry_run=args.dry_run,
            )
        return 0

    print(f"=== Full teardown of {CLUSTER_NAME} in {args.region} ===")
    for svc in SERVICES:
        run_aws(
            [
                "ecs",
                "update-service",
                "--cluster",
                CLUSTER_NAME,
                "--service",
                svc,
                "--desired-count",
                "0",
            ],
            region=args.region,
            dry_run=args.dry_run,
        )
        run_aws(
            [
                "ecs",
                "delete-service",
                "--cluster",
                CLUSTER_NAME,
                "--service",
                svc,
                "--force",
            ],
            region=args.region,
            dry_run=args.dry_run,
        )
        run_aws(
            ["logs", "delete-log-group", "--log-group-name", f"/ecs/{svc}"],
            region=args.region,
            dry_run=args.dry_run,
        )
        run_aws(
            ["ecr", "delete-repository", "--repository-name", svc, "--force"],
            region=args.region,
            dry_run=args.dry_run,
        )

    run_aws(
        ["ecs", "delete-cluster", "--cluster", CLUSTER_NAME],
        region=args.region,
        dry_run=args.dry_run,
    )
    print("Teardown complete. Zero billable resources remain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
