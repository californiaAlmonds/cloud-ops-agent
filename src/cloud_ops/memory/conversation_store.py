"""Persistent multi-turn conversation store backed by data/conversations.json."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
import uuid

from cloud_ops.schemas import utc_now_iso


def _make_id(prefix: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    short_hex = uuid.uuid4().hex[:6]
    return f"{prefix}-{ts}-{short_hex}"


def _derive_title(text: str, max_len: int = 48) -> str:
    cleaned = " ".join((text or "").strip().split())
    if not cleaned:
        return "New Conversation"
    if len(cleaned) <= max_len:
        return cleaned
    truncated = cleaned[:max_len].rsplit(" ", 1)[0]
    return f"{truncated or cleaned[:max_len]}..."


@dataclass
class ExecutedCommandRecord:
    """Single MCP tool or AWS CLI command executed during an assistant turn."""

    index: int
    tool_name: str
    command: str
    status: str = "ALLOWED"  # ALLOWED | BLOCKED | REQUIRES_APPROVAL
    execution_mode: str = "SIMULATION"  # LIVE_AWS | SIMULATION | BLOCKED
    output_summary: str = ""
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutedCommandRecord:
        return cls(
            index=int(data.get("index", 1)),
            tool_name=str(data.get("tool_name", "aws_cli")),
            command=str(data.get("command", "")),
            status=str(data.get("status", "ALLOWED")),
            execution_mode=str(data.get("execution_mode", "SIMULATION")),
            output_summary=str(data.get("output_summary", ""))[:600],
            timestamp=str(data.get("timestamp") or utc_now_iso()),
        )


@dataclass
class ChatMessageRecord:
    """Single user or assistant turn persisted inside a ConversationSession."""

    message_id: str = field(default_factory=lambda: _make_id("msg"))
    role: str = "user"  # "user" | "assistant"
    content: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    attachments: list[dict[str, str]] = field(default_factory=list)
    executed_commands: list[ExecutedCommandRecord] = field(default_factory=list)
    incident_id: str | None = None
    proposal: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at,
            "attachments": list(self.attachments),
            "executed_commands": [cmd.to_dict() for cmd in self.executed_commands],
            "incident_id": self.incident_id,
            "proposal": self.proposal,
            "verification": self.verification,
            "artifacts": list(self.artifacts),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatMessageRecord:
        raw_cmds = data.get("executed_commands") or []
        cmds = [
            cmd if isinstance(cmd, ExecutedCommandRecord) else ExecutedCommandRecord.from_dict(cmd)
            for cmd in raw_cmds
        ]
        return cls(
            message_id=str(data.get("message_id") or _make_id("msg")),
            role=str(data.get("role", "user")),
            content=str(data.get("content", "")),
            created_at=str(data.get("created_at") or utc_now_iso()),
            attachments=list(data.get("attachments") or []),
            executed_commands=cmds,
            incident_id=data.get("incident_id"),
            proposal=data.get("proposal"),
            verification=data.get("verification"),
            artifacts=list(data.get("artifacts") or []),
        )


@dataclass
class ConversationSession:
    """Multi-turn conversation thread with retained service/incident context."""

    conversation_id: str = field(default_factory=lambda: _make_id("conv"))
    title: str = "New Conversation"
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    active_service: str | None = None
    active_incident_id: str | None = None
    messages: list[ChatMessageRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "active_service": self.active_service,
            "active_incident_id": self.active_incident_id,
            "message_count": len(self.messages),
            "messages": [msg.to_dict() for msg in self.messages],
        }

    def to_summary_dict(self) -> dict[str, Any]:
        last_preview = ""
        if self.messages:
            last_preview = " ".join(self.messages[-1].content.strip().split())[:90]
        return {
            "conversation_id": self.conversation_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "active_service": self.active_service,
            "active_incident_id": self.active_incident_id,
            "message_count": len(self.messages),
            "last_preview": last_preview,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversationSession:
        raw_msgs = data.get("messages") or []
        msgs = [
            m if isinstance(m, ChatMessageRecord) else ChatMessageRecord.from_dict(m)
            for m in raw_msgs
        ]
        return cls(
            conversation_id=str(data.get("conversation_id") or _make_id("conv")),
            title=str(data.get("title") or "New Conversation"),
            created_at=str(data.get("created_at") or utc_now_iso()),
            updated_at=str(data.get("updated_at") or utc_now_iso()),
            active_service=data.get("active_service"),
            active_incident_id=data.get("active_incident_id"),
            messages=msgs,
        )


class ConversationStore:
    """JSON-backed persistence store for multi-turn ConversationSession records."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store_path = self.data_dir / "conversations.json"
        self.active_conversation_id: str | None = None
        self._sessions: dict[str, ConversationSession] = {}
        self._load()

    def _load(self) -> None:
        if not self.store_path.exists():
            self._save()
            return
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
            self.active_conversation_id = raw.get("active_conversation_id")
            self._sessions = {}
            for item in raw.get("conversations", []):
                sess = ConversationSession.from_dict(item)
                self._sessions[sess.conversation_id] = sess
            if (
                self.active_conversation_id not in self._sessions
                and self._sessions
            ):
                sorted_sessions = sorted(
                    self._sessions.values(),
                    key=lambda s: s.updated_at,
                    reverse=True,
                )
                self.active_conversation_id = sorted_sessions[0].conversation_id
        except Exception:
            self._sessions = {}
            self.active_conversation_id = None

    def _save(self) -> None:
        sorted_sessions = sorted(
            self._sessions.values(),
            key=lambda s: s.updated_at,
            reverse=True,
        )
        payload = {
            "active_conversation_id": self.active_conversation_id,
            "conversations": [s.to_dict() for s in sorted_sessions],
        }
        tmp_path = self.store_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_path.replace(self.store_path)

    def list_conversations(self) -> list[dict[str, Any]]:
        """Return conversation summaries sorted by updated_at descending."""
        sorted_sessions = sorted(
            self._sessions.values(),
            key=lambda s: s.updated_at,
            reverse=True,
        )
        return [s.to_summary_dict() for s in sorted_sessions]

    def create_conversation(self, title: str | None = None) -> ConversationSession:
        """Create and persist a new empty ConversationSession."""
        clean_title = (title or "").strip() or "New Conversation"
        session = ConversationSession(title=clean_title)
        self._sessions[session.conversation_id] = session
        self.active_conversation_id = session.conversation_id
        self._save()
        return session

    def get_conversation(self, conversation_id: str) -> ConversationSession | None:
        """Look up a conversation session by ID."""
        return self._sessions.get(conversation_id)

    def get_or_create_active(self, conversation_id: str | None = None) -> ConversationSession:
        """Return requested session if found, most recent session if omitted, or create new."""
        if conversation_id:
            existing = self._sessions.get(conversation_id)
            if existing is not None:
                self.active_conversation_id = existing.conversation_id
                return existing
            session = ConversationSession(conversation_id=conversation_id)
            self._sessions[session.conversation_id] = session
            self.active_conversation_id = session.conversation_id
            self._save()
            return session

        if self.active_conversation_id and self.active_conversation_id in self._sessions:
            return self._sessions[self.active_conversation_id]

        if self._sessions:
            sorted_sessions = sorted(
                self._sessions.values(),
                key=lambda s: s.updated_at,
                reverse=True,
            )
            self.active_conversation_id = sorted_sessions[0].conversation_id
            return sorted_sessions[0]

        return self.create_conversation()

    def append_message(
        self,
        conversation_id: str,
        message: ChatMessageRecord,
        active_service: str | None = None,
        active_incident_id: str | None = None,
    ) -> ConversationSession:
        """Append a message to a conversation, auto-title from first user message, and save."""
        session = self.get_or_create_active(conversation_id)
        if (
            message.role == "user"
            and session.title == "New Conversation"
            and message.content.strip()
        ):
            session.title = _derive_title(message.content)

        if active_service:
            session.active_service = active_service
        if active_incident_id:
            session.active_incident_id = active_incident_id
        elif message.incident_id:
            session.active_incident_id = message.incident_id

        session.messages.append(message)
        session.updated_at = utc_now_iso()
        self.active_conversation_id = session.conversation_id
        self._save()
        return session

    def update_proposal_status(
        self,
        incident_id: str,
        proposal_dict: dict[str, Any],
        verification_dict: dict[str, Any] | None = None,
        conversation_id: str | None = None,
    ) -> str | None:
        """Update inline proposal/verification status on messages matching incident_id."""
        matched_conv_id: str | None = conversation_id
        proposal_id = proposal_dict.get("proposal_id")
        for sess in self._sessions.values():
            if conversation_id and sess.conversation_id != conversation_id:
                continue
            for msg in sess.messages:
                msg_prop_id = (msg.proposal or {}).get("proposal_id")
                if msg.incident_id == incident_id or (
                    proposal_id and msg_prop_id == proposal_id
                ):
                    msg.proposal = dict(proposal_dict)
                    if verification_dict is not None:
                        msg.verification = dict(verification_dict)
                    sess.updated_at = utc_now_iso()
                    matched_conv_id = sess.conversation_id
        if matched_conv_id is None:
            # Fallback: scan all conversations if specific conversation didn't have the message
            for sess in self._sessions.values():
                for msg in sess.messages:
                    msg_prop_id = (msg.proposal or {}).get("proposal_id")
                    if msg.incident_id == incident_id or (
                        proposal_id and msg_prop_id == proposal_id
                    ):
                        msg.proposal = dict(proposal_dict)
                        if verification_dict is not None:
                            msg.verification = dict(verification_dict)
                        sess.updated_at = utc_now_iso()
                        matched_conv_id = sess.conversation_id
        if matched_conv_id:
            self._save()
        return matched_conv_id

    def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a conversation session by ID."""
        if conversation_id not in self._sessions:
            return False
        del self._sessions[conversation_id]
        if self.active_conversation_id == conversation_id:
            if self._sessions:
                sorted_sessions = sorted(
                    self._sessions.values(),
                    key=lambda s: s.updated_at,
                    reverse=True,
                )
                self.active_conversation_id = sorted_sessions[0].conversation_id
            else:
                self.active_conversation_id = None
        self._save()
        return True
