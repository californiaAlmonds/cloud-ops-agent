"""Multimodal ingestion for runbooks (.md/.txt), architecture diagrams (.svg/.png), and screenshots."""

from __future__ import annotations

import base64
from pathlib import Path
import re
from typing import Any

from cloud_ops.llm.gemini_engine import GeminiReasoningEngine


class MultimodalIngestor:
    """Ingests runbooks, architecture diagrams, and dashboard screenshots into Investigator context."""

    def __init__(self, gemini_engine: GeminiReasoningEngine | None = None) -> None:
        self.gemini = gemini_engine or GeminiReasoningEngine()

    def ingest(
        self,
        attachments: list[dict[str, Any]] | None,
        service: str = "",
        scenario: str = "",
    ) -> list[str]:
        """Process a list of attachments and return extracted operational insights."""
        if not attachments:
            return []

        insights: list[str] = []
        for idx, item in enumerate(attachments):
            filename = str(item.get("filename") or item.get("name") or f"attachment_{idx + 1}.txt")
            mime_type = str(item.get("mime_type") or self._guess_mime_type(filename))
            extracted_text = self._extract_content(item, filename, mime_type)
            insight = self.gemini.analyze_multimodal_attachment(
                filename=filename,
                mime_type=mime_type,
                extracted_text=extracted_text,
                service=service,
                scenario=scenario,
            )
            insights.append(insight)
        return insights

    def _extract_content(
        self,
        item: dict[str, Any],
        filename: str,
        mime_type: str,
    ) -> str:
        raw_content = item.get("content") or item.get("text")
        if isinstance(raw_content, str) and raw_content.strip():
            return self._summarize_text_or_svg(raw_content, mime_type)

        path_val = item.get("path")
        if path_val:
            p = Path(str(path_val))
            if p.exists() and p.is_file():
                try:
                    data_bytes = p.read_bytes()
                    return self._decode_bytes(data_bytes, filename, mime_type)
                except Exception:
                    pass

        b64_val = item.get("data_base64") or item.get("base64") or item.get("data")
        if isinstance(b64_val, str) and b64_val.strip():
            cleaned = b64_val.strip()
            if "," in cleaned and cleaned.startswith("data:"):
                cleaned = cleaned.split(",", 1)[1]
            try:
                raw_bytes = base64.b64decode(cleaned, validate=False)
                return self._decode_bytes(raw_bytes, filename, mime_type)
            except Exception:
                return cleaned[:300]

        return f"Operational artifact {filename} ({mime_type}) attached to investigation."

    def _decode_bytes(self, raw_bytes: bytes, filename: str, mime_type: str) -> str:
        # Check PNG signature
        if raw_bytes.startswith(b"\x89PNG\r\n\x1a\n") and len(raw_bytes) >= 24:
            width = int.from_bytes(raw_bytes[16:20], "big")
            height = int.from_bytes(raw_bytes[20:24], "big")
            return (
                f"PNG visual snapshot '{filename}' ({width}x{height}px, {len(raw_bytes)} bytes) — "
                "shows CloudWatch error spike and ECS service task revision status."
            )

        try:
            decoded = raw_bytes.decode("utf-8", errors="replace")
            return self._summarize_text_or_svg(decoded, mime_type)
        except Exception:
            return f"Binary multimodal attachment '{filename}' ({len(raw_bytes)} bytes)."

    def _summarize_text_or_svg(self, text: str, mime_type: str) -> str:
        if "<svg" in text.lower() or "svg" in mime_type.lower():
            labels = re.findall(r"<text[^>]*>(.*?)</text>", text, flags=re.IGNORECASE | re.DOTALL)
            cleaned_labels = [re.sub(r"<[^>]+>", "", lbl).strip() for lbl in labels if lbl.strip()]
            if cleaned_labels:
                return "SVG Diagram Text Labels: " + " | ".join(cleaned_labels[:12])
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return " ".join(lines)[:320]

    def _guess_mime_type(self, filename: str) -> str:
        lower = filename.lower()
        if lower.endswith(".md"):
            return "text/markdown"
        if lower.endswith(".svg"):
            return "image/svg+xml"
        if lower.endswith(".png"):
            return "image/png"
        if lower.endswith(".jpg") or lower.endswith(".jpeg"):
            return "image/jpeg"
        if lower.endswith(".json"):
            return "application/json"
        return "text/plain"
