#!/usr/bin/env python3
"""Provision the low-cost AWS ECS Fargate demo environment for Cloud Ops Agent.

Designed specifically for the AWS Free Plan ($100-$200 credit budget):
- No Application Load Balancer (~$18/mo saved)
- No NAT Gateway (~$32/mo saved)
- Uses default VPC public subnets with assignPublicIp=ENABLED
- Smallest Fargate task size: 0.25 vCPU (256 CPU units), 0.5 GB (512 MB) memory (~$0.012/hr per task)
- CloudWatch Log Groups set to 1-day retention so logs never accumulate charges
- Creates the scoped Cloud Ops Agent IAM policy (read-only + allowlisted remediation on demo cluster)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

CLUSTER_NAME = "cloud-ops-demo-cluster"
EXECUTION_ROLE_NAME = "cloud-ops-ecs-execution-role"
AGENT_POLICY_NAME = "CloudOpsAgentScopedPolicy"
SERVICES = ["salescreate-api", "inventory-worker", "notifier"]
LOG_RETENTION_DAYS = 1


def aws_cli(args: list[str], region: str, dry_run: bool = False) -> dict | str | None:
    cmd = ["aws", *args, "--region", region, "--output", "json"]
    if dry_run:
        print(f"[DRY-RUN] {' '.join(cmd)}")
        return None
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed ({' '.join(cmd)}):\n{proc.stderr.strip()}")
    out = proc.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


def bootstrap(region: str, account_id: str | None, desired_count: int, dry_run: bool) -> int:
    root_dir = Path(__file__).resolve().parent.parent
    if not dry_run and not account_id:
        ident = aws_cli(["sts", "get-caller-identity"], region=region, dry_run=False)
        assert isinstance(ident, dict)
        account_id = ident["Account"]
    account_id = account_id or "123456789012"

    print(f"=== Cloud Ops Agent AWS Bootstrap (Account: {account_id}, Region: {region}) ===")

    # 1. Create ECR repositories & CloudWatch log groups (1-day retention)
    for svc in SERVICES:
        print(f"\n--- Configuring ECR & CloudWatch Logs for {svc} ---")
        aws_cli(
            [
                "ecr",
                "create-repository",
                "--repository-name",
                svc,
                "--image-scanning-configuration",
                "scanOnPush=false",
            ],
            region=region,
            dry_run=dry_run,
        )
        log_group = f"/ecs/{svc}"
        aws_cli(
            ["logs", "create-log-group", "--log-group-name", log_group],
            region=region,
            dry_run=dry_run,
        )
        aws_cli(
            [
                "logs",
                "put-retention-policy",
                "--log-group-name",
                log_group,
                "--retention-in-days",
                str(LOG_RETENTION_DAYS),
            ],
            region=region,
            dry_run=dry_run,
        )

    # 2. Create ECS Cluster
    print(f"\n--- Creating ECS Cluster: {CLUSTER_NAME} ---")
    aws_cli(
        ["ecs", "create-cluster", "--cluster-name", CLUSTER_NAME],
        region=region,
        dry_run=dry_run,
    )

    # 3. Create ECS Task Execution Role
    trust_policy = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    )
    print(f"\n--- Creating ECS Task Execution Role: {EXECUTION_ROLE_NAME} ---")
    aws_cli(
        [
            "iam",
            "create-role",
            "--role-name",
            EXECUTION_ROLE_NAME,
            "--assume-role-policy-document",
            trust_policy,
        ],
        region=region,
        dry_run=dry_run,
    )
    aws_cli(
        [
            "iam",
            "attach-role-policy",
            "--role-name",
            EXECUTION_ROLE_NAME,
            "--policy-arn",
            "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
        ],
        region=region,
        dry_run=dry_run,
    )

    # 4. Create Scoped IAM Policy for the Cloud Ops Agent
    policy_path = root_dir / "infra" / "agent_iam_policy.json"
    print(f"\n--- Creating Scoped Agent IAM Policy: {AGENT_POLICY_NAME} ---")
    aws_cli(
        [
            "iam",
            "create-policy",
            "--policy-name",
            AGENT_POLICY_NAME,
            "--policy-document",
            f"file://{policy_path}",
        ],
        region=region,
        dry_run=dry_run,
    )

    # 5. Register initial Task Definitions for each service
    for svc in SERVICES:
        tpl_path = root_dir / "services" / svc / "ecs" / "task-definition.template.json"
        print(f"\n--- Registering Task Definition for {svc} ({tpl_path.name}) ---")
        aws_cli(
            [
                "ecs",
                "register-task-definition",
                "--cli-input-json",
                f"file://{tpl_path}",
            ],
            region=region,
            dry_run=dry_run,
        )

    print(
        f"\nBootstrap complete! Desired task count target: {desired_count}.\n"
        "Tip: Use 'python infra/aws_teardown.py --pause' between sessions to scale tasks to 0 and spend $0."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap AWS ECS demo resources.")
    parser.add_argument("--region", default="us-east-1", help="AWS region (default: us-east-1)")
    parser.add_argument("--account-id", help="AWS account ID (auto-detected when not dry-run)")
    parser.add_argument("--desired-count", type=int, default=1, help="Desired tasks per service")
    parser.add_argument("--dry-run", action="store_true", help="Print AWS CLI commands without executing")
    args = parser.parse_args()
    return bootstrap(args.region, args.account_id, args.desired_count, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
