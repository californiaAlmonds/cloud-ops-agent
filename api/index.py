"""Vercel Python Serverless Function entrypoint for Cloud Ops Agent (Single-Package UI + API)."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cloud_ops.api.server import CloudOpsRequestHandler  # noqa: E402


class handler(CloudOpsRequestHandler):  # noqa: N801
    """Vercel @vercel/python BaseHTTPRequestHandler adapter serving /, /static/*, /api/*, and /artifacts/*."""
