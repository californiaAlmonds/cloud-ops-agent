"""Configuration management for Cloud Ops Agent."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent.parent

DEPRECATED_GEMINI_PREFIXES = ("gemini-2.5", "gemini-2.0", "gemini-1.5")
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_VERTEX_PROJECT = "cog01m1hm7b45680madcxrzh1adja"
DEFAULT_VERTEX_LOCATION = "global"


def detect_adc_credentials_path() -> Path | None:
    """Locate Google Cloud Application Default Credentials (ADC) file if present on disk."""
    explicit = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p

    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        win_adc = Path(appdata) / "gcloud" / "application_default_credentials.json"
        if win_adc.exists():
            return win_adc

    unix_adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    if unix_adc.exists():
        return unix_adc

    return None


def normalize_gemini_model(model_name: str | None) -> str:
    """Ensure the configured model is never a deprecated Gemini model."""
    if not model_name or not model_name.strip():
        return DEFAULT_GEMINI_MODEL
    cleaned = model_name.strip()
    if any(cleaned.startswith(prefix) for prefix in DEPRECATED_GEMINI_PREFIXES):
        return DEFAULT_GEMINI_MODEL
    return cleaned


@dataclass
class AgentConfig:
    """Runtime configuration for the Cloud Ops Agent backend."""

    gemini_model: str = DEFAULT_GEMINI_MODEL
    use_vertex_ai: bool = True
    vertex_project: str = DEFAULT_VERTEX_PROJECT
    vertex_location: str = DEFAULT_VERTEX_LOCATION
    aws_region: str = "us-east-1"
    ecs_cluster: str = "cloud-ops-demo-cluster"
    live_aws: bool = False
    artifacts_dir: Path = field(default_factory=lambda: ROOT_DIR / "artifacts")
    data_dir: Path = field(default_factory=lambda: ROOT_DIR / "data")
    services_dir: Path = field(default_factory=lambda: ROOT_DIR / "services")
    iam_policy_path: Path = field(
        default_factory=lambda: ROOT_DIR / "infra" / "agent_iam_policy.json"
    )

    def __post_init__(self) -> None:
        self.gemini_model = normalize_gemini_model(self.gemini_model)
        self.artifacts_dir = Path(self.artifacts_dir)
        self.data_dir = Path(self.data_dir)
        self.services_dir = Path(self.services_dir)
        self.iam_policy_path = Path(self.iam_policy_path)

    @classmethod
    def from_env(cls) -> AgentConfig:
        """Create an AgentConfig populated from environment variables with safe defaults."""
        model = normalize_gemini_model(os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL))
        vertex_project = (
            os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("VERTEX_PROJECT")
            or os.environ.get("BRIDGE_VERTEX_PROJECT")
            or DEFAULT_VERTEX_PROJECT
        ).strip()
        vertex_location = (
            os.environ.get("GOOGLE_CLOUD_LOCATION")
            or os.environ.get("VERTEX_LOCATION")
            or os.environ.get("BRIDGE_VERTEX_LOCATION")
            or DEFAULT_VERTEX_LOCATION
        ).strip()

        use_vertex_raw = (
            os.environ.get("CLOUD_OPS_USE_VERTEX_AI")
            or os.environ.get("GOOGLE_GENAI_USE_VERTEXAI")
            or ""
        ).strip().lower()
        if use_vertex_raw in ("0", "false", "no"):
            use_vertex_ai = False
        elif use_vertex_raw in ("1", "true", "yes"):
            use_vertex_ai = True
        else:
            # Default to Vertex AI ADC unless GEMINI_API_KEY is explicitly set without Vertex project
            use_vertex_ai = True

        region = os.environ.get("AWS_REGION", "us-east-1")
        cluster = os.environ.get("ECS_CLUSTER", "cloud-ops-demo-cluster")
        live_aws_raw = os.environ.get("CLOUD_OPS_LIVE_AWS", "false").strip().lower()
        live_aws = live_aws_raw in ("1", "true", "yes")

        is_vercel = os.environ.get("VERCEL", "").strip().lower() in ("1", "true", "yes")
        if is_vercel:
            tmp_root = Path(tempfile.gettempdir()) / "cloud-ops-agent"
            artifacts_dir = tmp_root / "artifacts"
            data_dir = tmp_root / "data"
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            data_dir.mkdir(parents=True, exist_ok=True)
            seed_memory = ROOT_DIR / "data" / "memory_store.json"
            target_memory = data_dir / "memory_store.json"
            if seed_memory.exists() and not target_memory.exists():
                shutil.copy2(seed_memory, target_memory)
            seed_conversations = ROOT_DIR / "data" / "conversations.json"
            target_conversations = data_dir / "conversations.json"
            if seed_conversations.exists() and not target_conversations.exists():
                shutil.copy2(seed_conversations, target_conversations)
        else:
            artifacts_dir = ROOT_DIR / "artifacts"
            data_dir = ROOT_DIR / "data"

        return cls(
            gemini_model=model,
            use_vertex_ai=use_vertex_ai,
            vertex_project=vertex_project,
            vertex_location=vertex_location,
            aws_region=region,
            ecs_cluster=cluster,
            live_aws=live_aws,
            artifacts_dir=artifacts_dir,
            data_dir=data_dir,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize configuration to a plain dictionary."""
        adc_path = detect_adc_credentials_path()
        return {
            "gemini_model": self.gemini_model,
            "use_vertex_ai": self.use_vertex_ai,
            "vertex_project": self.vertex_project,
            "vertex_location": self.vertex_location,
            "adc_credentials_found": adc_path is not None,
            "adc_credentials_path": str(adc_path) if adc_path else None,
            "aws_region": self.aws_region,
            "ecs_cluster": self.ecs_cluster,
            "live_aws": self.live_aws,
            "artifacts_dir": str(self.artifacts_dir),
            "data_dir": str(self.data_dir),
            "services_dir": str(self.services_dir),
            "iam_policy_path": str(self.iam_policy_path),
        }
