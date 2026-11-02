# Conversational Multi-Turn SRE Chatbot UI & Dynamic AWS Tool-Calling Engine

*   **Author:** Cloud Ops Agent Team
*   **Status:** Draft
*   **Date:** 2026-10-30

--------------------------------------------------------------------------------

## Overview

Cloud Ops Agent is an operational assistant that connects Google Cloud Vertex AI (`gemini-3.8-flash` via Application Default Credentials) to Amazon ECS, Amazon CloudWatch, and a guardrailed AWS CLI execution layer. It allows engineers and non-technical stakeholders to inspect service health, diagnose production failures, generate operational reports (`.svg`, `.xlsx`, `.docx`, `.md`), and execute human-approved remediations using natural language.

This design replaces the existing multi-panel telemetry dashboard (`src/cloud_ops/ui/`) and static keyword router (`CloudOpsOrchestrator.handle_chat()`) with a two-column conversational interface and a multi-turn Gemini function-calling loop. The interface consists of a left sidebar for creating new chats and switching between persisted conversation threads, and a centered message stream that renders inline expandable command-execution drawers, inline human-approval cards for mutating AWS operations, and inline visual charts and downloadable document artifacts.

## The Problem

The current implementation has three structural limitations that prevent it from functioning as a general-purpose conversational layer over live AWS infrastructure:

1.  **Overloaded Dashboard Layout**: The web interface (`src/cloud_ops/ui/index.html`) renders a 5-stage pipeline banner, three service telemetry cards with fault-injection dropdowns, a narrow side chat box, and seven right-hand inspector panels simultaneously. At a `1440x900` viewport, the layout overflows horizontally and exposes internal agent data structures (`IncidentRecord`, task-definition revision diffs, hypothesis matrices) rather than answering the user's question in plain language.
2.  **Hardcoded Service Allowlists and Keyword Routing**: `CloudWatchMCPServer` (`src/cloud_ops/mcp_servers/cloudwatch_mcp.py`) and `ECSMCPServer` (`src/cloud_ops/mcp_servers/ecs_mcp.py`) enforce `SUPPORTED_SERVICES = ("salescreate-api", "inventory-worker", "notifier")` and compare task definitions against local Python files in `services/<name>/src`. When an external GitHub Actions pipeline deploys a pre-configured faulty service to Amazon ECS, `_validate_service()` raises a `ValueError`, and the `if/elif` substring router in `CloudOpsOrchestrator.handle_chat()` cannot discover or inspect the deployed service.
3.  **Stateless Chat & Global Unscoped Audit Logs**: The frontend stores messages only in the live DOM (`#chatMessages`), losing all conversation history on page reload, while `GuardrailedAWSCLI` (`src/cloud_ops/mcp_servers/aws_cli_guard.py`) records all commands in a single process-wide list (`self.audit_log`). Consequently, users cannot switch between past conversations, ask multi-turn follow-up questions with retained context, or expand a specific assistant reply to inspect the exact AWS CLI and MCP commands executed during that turn.

The proposed solution replaces the multi-panel dashboard with a two-column chatbot interface backed by a persisted `ConversationStore` (`data/conversations.json`), removes hardcoded service name restrictions in favor of live ECS cluster/service/log-group discovery and revision `N` vs. `N-1` task-definition diffing, and drives each assistant turn through a bounded Gemini tool-calling loop that records per-message command slices and pauses on mutating actions for inline human approval.

## Goals

*   **Two-Column Conversational Interface**: Replace the multi-panel dashboard in `src/cloud_ops/ui/` with a collapsible left sidebar (`+ New Chat` button and chronological conversation history list) and a centered chat stream (`max-width: 820px`) with a multimodal attachment composer (`📎` file/image/runbook upload and drag-and-drop).
*   **Persistent Multi-Turn Conversation Store**: Persist conversation threads and messages to `data/conversations.json` (and `/tmp/cloud-ops-agent/data/conversations.json` when `VERCEL=1`), exposing `/api/conversations` CRUD endpoints so users can create new chats, reload prior conversations across browser refreshes, and ask multi-turn follow-up questions with prior turns passed to Gemini.
*   **Dynamic ECS & CloudWatch Discovery**: Remove `SUPPORTED_SERVICES` validation gates in `ECSMCPServer` and `CloudWatchMCPServer` so any ECS service deployed via GitHub Actions can be discovered (`list_ecs_services`), inspected (`describe_ecs_service`), diffed between active revision `N` and prior revision `N-1` (`diff_task_definitions`), and queried for logs and metrics (`get_recent_logs`, `get_service_metrics`) by auto-resolving the `awslogs-group` from the active ECS task definition.
*   **Bounded Gemini Function-Calling Loop with Offline Parity**: Implement a multi-turn tool-calling loop (`MAX_TOOL_ROUNDS = 6`) in `GeminiReasoningEngine` using `gemini-3.8-flash` on Vertex AI ADC, paired with a deterministic offline tool router when `CLOUD_OPS_OFFLINE_LLM=1` so existing and new unit tests execute without external network calls.
*   **Inline Collapsible Command Drawer**: Record the exact MCP tools and AWS CLI commands executed during each assistant turn (`executed_commands`) and render them inside a collapsed `<details>` drawer attached to that message so the user can expand and retract the executed commands on demand.
*   **Inline Human Approval & Multimodal Artifacts**: Intercept any mutating tool call (`propose_service_remediation` / rollback / env update / scaling) to render an inline approval card (`Approve & Execute` / `Reject`) inside the assistant message, and render generated `.svg` charts inline alongside download cards for `.xlsx`, `.docx`, and `.md` reports.

## Non-Goals

*   **UI Fault-Injection Controls**: Fault-injection dropdowns and "Inject Scenario" buttons are removed from the web UI; faulty services are pushed externally via GitHub Actions (while programmatic `inject_scenario` APIs remain available for unit tests).
*   **Unapproved or Destructive AWS Mutations**: The agent will never execute `ecs:DeleteCluster`, `ecs:DeleteService`, `ecs:DeregisterTaskDefinition`, or IAM privilege changes, nor execute allowlisted write commands (`ecs:UpdateService`, `ecs:RegisterTaskDefinition`) without an HMAC-SHA256 human approval token.
*   **External Database or Frontend Bundler Dependencies**: The system will not add PostgreSQL, Redis, React, Vite, or Node.js build steps; the backend remains pure Python 3.12+ standard HTTP server + `google-genai` and the frontend remains vanilla HTML, CSS, and ES2022 JavaScript.

## Open Questions

*   None. All scope boundaries (inline collapsible command drawer, external GitHub Actions fault deployment, dynamic ECS discovery, and single-package local/Vercel operation) were resolved during Phase 1 brainstorming.

## Critical User Journeys (CUJs)

### CUJ 1: Conversational Diagnosis of an Externally Deployed Faulty ECS Service

*   **Given**: A user has deployed a faulty ECS service revision to the cluster via GitHub Actions and opens `http://127.0.0.1:8000` in a new chat session.
*   **When**: The user asks in plain English, *"Hey, are our services healthy right now? Why is checkout failing?"*
*   **Then**:
    1.  The backend creates or appends to the active `ConversationSession` and invokes `GeminiReasoningEngine.chat_with_tools()`.
    2.  The agent calls read-only MCP tools in the background (`list_ecs_services`, `describe_ecs_service`, `get_service_metrics`, `get_recent_logs`, `diff_task_definitions`), capturing every underlying AWS CLI / MCP invocation in the turn's `executed_commands` list.
    3.  The assistant replies in plain English explaining which service is failing and why, accompanied by a collapsed pill (`▸ Executed 5 AWS & MCP commands — click to expand`), an inline SVG metric chart of the error spike, and an inline Human Approval Card proposing the specific rollback or environment fix.
    4.  Clicking the command pill expands the drawer to reveal each command string, mode (`LIVE_AWS` or `SIMULATION`), policy status (`ALLOWED`), and output summary; clicking it again retracts the drawer.

### CUJ 2: Inline Human Approval and Post-Fix Verification

*   **Given**: An assistant message in the active conversation contains a `PENDING_APPROVAL` remediation card (for example, rolling back the faulty ECS service to its previous task definition revision or scaling task count).
*   **When**: The user clicks **Approve & Execute Fix** on the inline card (or types *"Yes, go ahead and apply that fix"* in the chat input).
*   **Then**:
    1.  `RemediatorAgent` generates an HMAC-SHA256 approval token and executes the allowlisted command through `GuardrailedAWSCLI`.
    2.  `VerifierAgent` queries post-remediation metrics and task counts to confirm recovery (or generates a rollback proposal if recovery checks fail).
    3.  The inline card updates to `APPROVED & EXECUTED`, and the assistant appends a follow-up message in the same conversation confirming service recovery with its own collapsible command drawer showing the executed write and verification commands.

### CUJ 3: On-Demand Multimodal Reporting & Conversation History Switching

*   **Given**: An ongoing conversation where the user has investigated service events or metrics, plus one or more prior conversations saved on disk.
*   **When**: The user asks, *"Log these events into an Excel sheet and give me a Word post-incident report for my manager,"* and later clicks **+ New Chat** in the left sidebar before clicking back to the previous conversation in the history list.
*   **Then**:
    1.  The agent invokes `generate_operational_artifacts` (backed by `CloudOpsOrchestrator.generate_artifacts`), returning inline download cards for the `.xlsx` workbook, `.docx` Word report, `.md` summary, and an inline `.svg` chart inside the chat reply.
    2.  Clicking **+ New Chat** clears the main canvas to a fresh session while preserving the previous conversation in the left sidebar list.
    3.  Clicking the previous conversation in the left sidebar restores all prior user/assistant messages, collapsible command drawers, inline charts, and download links from `data/conversations.json`.

--------------------------------------------------------------------------------

## Technical Plan (Architecture)

### 1. System Architecture & End-to-End Request Flow

The redesigned system separates presentation (two-column conversational UI), session state (`ConversationStore`), multi-turn reasoning and tool dispatch (`GeminiReasoningEngine` + `CloudOpsOrchestrator`), and infrastructure access (`ECSMCPServer`, `CloudWatchMCPServer`, `GuardrailedAWSCLI`).

```text
+---------------------------------------------------------------------------------------------------+
| Browser: Two-Column Conversational UI (src/cloud_ops/ui/)                                         |
|  +------------------------+  +-----------------------------------------------------------------+  |
|  | Left Sidebar (260px)   |  | Centered Chat Stream (max-width: 820px)                         |  |
|  | - [+ New Chat]         |  | - User Bubble (+ Multimodal Attachments)                        |  |
|  | - Conversation History |  | - Assistant Bubble:                                             |  |
|  |   (Switch / Delete)    |  |   * Plain-English Narrative (Markdown)                          |  |
|  | - Connection Footer    |  |   * <details> Executed AWS & MCP Commands Drawer (Expand/Close) |  |
|  |                        |  |   * Inline SVG Chart & Download Cards (.xlsx, .docx, .md)       |  |
|  |                        |  |   * Inline Human Approval Card (Approve & Execute / Reject)     |  |
|  +------------------------+  +-----------------------------------------------------------------+  |
+-------------------------------------------------|-------------------------------------------------+
                                                  | REST JSON (/api/conversations/*, /api/chat)
                                                  v
+---------------------------------------------------------------------------------------------------+
| CloudOpsRequestHandler (src/cloud_ops/api/server.py) -> CloudOpsOrchestrator (orchestrator.py)    |
|  1. Load or create ConversationSession from ConversationStore (data/conversations.json)           |
|  2. Snapshot GuardrailedAWSCLI audit cursor (start_idx = len(cli_guard.audit_log))                |
|  3. Invoke GeminiReasoningEngine.chat_with_tools() (bounded at MAX_TOOL_ROUNDS = 6)               |
+-------------------------------------------------|-------------------------------------------------+
                                                  |
          +---------------------------------------+---------------------------------------+
          | Read-Only Tool Calls (Auto-Executed)                                          | Proposal / Artifact Calls
          v                                                                               v
+---------------------------------------------------+   +---------------------------------------------------+
| Dynamic MCP & AWS CLI Layer                       |   | Write Interception & Multimodal Artifact Layer    |
| - list_ecs_services(cluster)                      |   | - propose_service_remediation(...)                |
| - describe_ecs_service(service)                   |   |   -> Creates PENDING_APPROVAL RemediationProposal |
| - diff_task_definitions(service, rev_a, rev_b)    |   |      (Does NOT mutate AWS until user approves)    |
| - get_service_metrics(service, window_minutes)    |   | - generate_operational_artifacts(...)             |
| - get_recent_logs(service, limit)                 |   |   -> Emits .svg chart + .xlsx + .docx + .md       |
| - inspect_iam_role_permissions()                  |   +---------------------------------------------------+
| - query_incident_memory(service, symptom)         |
+---------------------------------------------------+
```

### 2. Persistent Conversation Data Model (`ConversationStore`)

A new module `src/cloud_ops/memory/conversation_store.py` manages multi-turn conversation threads backed by `data/conversations.json` (and `/tmp/cloud-ops-agent/data/conversations.json` when `VERCEL=1` in `AgentConfig.from_env()`).

*   **`ExecutedCommandRecord` Schema**:
    *   `index` (`int`): 1-based sequence number within the assistant turn.
    *   `tool_name` (`str`): High-level MCP tool name (for example, `describe_ecs_service`, `get_recent_logs`, `rollback_task_definition`) or `aws_cli`.
    *   `command` (`str`): Exact AWS CLI command string validated by `GuardrailedAWSCLI`.
    *   `status` (`str`): `ALLOWED`, `BLOCKED`, or `REQUIRES_APPROVAL`.
    *   `execution_mode` (`str`): `LIVE_AWS`, `SIMULATION`, or `BLOCKED`.
    *   `output_summary` (`str`): Concise human-readable finding or truncated stdout/stderr (capped at 600 characters for UI readability).
    *   `timestamp` (`str`): ISO-8601 UTC timestamp.
*   **`ChatMessageRecord` Schema**:
    *   `message_id` (`str`): Unique identifier (`msg-<timestamp>-<short_hex>`).
    *   `role` (`str`): `"user"` or `"assistant"`.
    *   `content` (`str`): Plain-English markdown message text.
    *   `created_at` (`str`): ISO-8601 UTC timestamp.
    *   `attachments` (`list[dict[str, str]]`): Uploaded user files/images/runbooks (`filename`, `mime_type`, `preview_text`).
    *   `executed_commands` (`list[ExecutedCommandRecord]`): Per-turn slice of all MCP and AWS CLI commands executed to produce this assistant message.
    *   `incident_id` (`str | None`): Associated `IncidentRecord.incident_id` when an investigation or remediation proposal is linked to this turn.
    *   `proposal` (`dict[str, Any] | None`): Serialized `RemediationProposal` (`status` in `PENDING_APPROVAL`, `APPROVED_AND_EXECUTED`, `REJECTED`) for rendering the inline approval card.
    *   `verification` (`dict[str, Any] | None`): Serialized `VerificationResult` populated after human approval executes.
    *   `artifacts` (`list[dict[str, Any]]`): Generated `.svg`, `.xlsx`, `.docx`, and `.md` metadata (`kind`, `name`, `url`, `preview_url`, `download_url`, `size_bytes`) rendered inline in the message bubble.
*   **`ConversationSession` Schema**:
    *   `conversation_id` (`str`): Unique identifier (`conv-<timestamp>-<short_hex>`).
    *   `title` (`str`): Auto-generated 3–6 word title derived from the first user prompt (or user-provided title).
    *   `created_at` (`str`): ISO-8601 UTC timestamp.
    *   `updated_at` (`str`): ISO-8601 UTC timestamp.
    *   `active_service` (`str | None`): Last service discussed in the thread for conversational pronoun resolution (for example, *"Now export its logs to Excel"*).
    *   `active_incident_id` (`str | None`): Most recent incident linked to the conversation.
    *   `messages` (`list[ChatMessageRecord]`): Ordered list of user and assistant turns.

### 3. Dynamic ECS & CloudWatch Discovery (Removing Hardcoded Service Allowlists)

To support arbitrary faulty ECS services pushed via GitHub Actions without breaking offline simulation tests:

1.  **Removal of `_validate_service()` Restrictions**:
    *   In `src/cloud_ops/mcp_servers/cloudwatch_mcp.py` and `src/cloud_ops/mcp_servers/ecs_mcp.py`, `SUPPORTED_SERVICES` is retained as `DEFAULT_SIMULATED_SERVICES = ("salescreate-api", "inventory-worker", "notifier")` for seeding local simulation state, but `_validate_service(service)` no longer raises `ValueError` for unknown service names. Instead, it normalizes the service string (stripping whitespace and rejecting shell metacharacters).
2.  **Cluster-Wide Service Discovery (`list_ecs_services`)**:
    *   `ECSMCPServer.list_ecs_services()` executes `aws ecs list-services --cluster <cluster> --region <region>` via `GuardrailedAWSCLI.execute_command()`.
    *   When `live_aws=True`, it extracts the service names from the returned ARNs, calls `describe_ecs_service(svc)` for each discovered service, and returns their live status, task counts (`running_count` / `desired_count`), active task definition revision, and recent deployment events.
    *   When `live_aws=False` (or if live AWS returns no services and offline fallback is enabled), it returns the registered simulator services in `CloudWatchMCPServer.active_scenarios`.
3.  **Dynamic Task Definition Revision Diffing (`diff_task_definitions`)**:
    *   When `live_aws=True` (or when inspecting a service outside the 3 seeded simulator services), `ECSMCPServer.diff_task_definitions(service, baseline_revision=None, active_revision=None)` resolves the active task definition family/revision from `describe_ecs_service(service)`.
    *   If `baseline_revision` is not specified, it queries `aws ecs list-task-definitions --family-prefix <family> --sort DESC --max-items 2` to identify revision `N` (active) and revision `N-1` (previous healthy baseline).
    *   It then executes `aws ecs describe-task-definition --task-definition <family>:<rev>` for both revisions and computes `TaskDefDiff.changed_env` across container `environment` variables, container `image` tags, and `cpu`/`memory` limits.
4.  **Automatic CloudWatch Log Group Resolution (`get_recent_logs`)**:
    *   When querying logs for an arbitrary service in `live_aws=True` mode, `CloudWatchMCPServer.get_recent_logs(service)` inspects the container `logConfiguration.options["awslogs-group"]` from the active ECS task definition (if available), then falls back sequentially to `/ecs/<cluster>/<service>`, `/ecs/cloud-ops-demo/<service>`, and `/ecs/<service>` using `aws logs filter-log-events`.
5.  **Allowlist Updates in `GuardrailedAWSCLI` & IAM Policy**:
    *   `READ_ONLY_PREFIXES` in `src/cloud_ops/mcp_servers/aws_cli_guard.py` and `InvestigatorAndVerifierReadOnly` in `infra/agent_iam_policy.json` permit `ecs:ListServices` and `logs:DescribeLogGroups`, while `RemediatorAllowlistedWriteOnDemoCluster` in `infra/agent_iam_policy.json` is widened to `"arn:aws:ecs:*:*:service/cloud-ops-demo-cluster/*"` and `"arn:aws:ecs:*:*:task-definition/*:*"` so dynamic services in the demo cluster can be remediated upon human approval.

### 4. Bounded Multi-Turn Gemini Tool-Calling Loop & Per-Turn Command Audit

`GeminiReasoningEngine` (`src/cloud_ops/llm/gemini_engine.py`) and `CloudOpsOrchestrator` (`src/cloud_ops/orchestrator.py`) coordinate each user turn as follows:

1.  **Per-Turn Command Audit Capture**:
    *   Before dispatching each MCP tool invocation in a chat turn or approval execution, `CloudOpsOrchestrator` records `step_audit_start = len(self.cli_guard.audit_log)`.
    *   Every MCP tool invocation summarizes its finding into `output_summary` (`<= 600` chars) and maps newly appended entries in `self.cli_guard.get_audit_slice(step_audit_start)` into `ExecutedCommandRecord` items (`tool_name`, `command`, `status`, `execution_mode`, `output_summary`, `timestamp`) on the assistant message.
2.  **Callable Tool Registry**:
    *   The orchestrator exposes 8 structured tools to `GeminiReasoningEngine.chat_with_tools()`:
        1.  `list_ecs_services()`: Lists all ECS services in the cluster with task counts, health status, and active task definition revisions.
        2.  `investigate_service(service: str)`: Runs the multi-step read-only investigation (`get_service_metrics`, `get_recent_logs`, `describe_ecs_service`, `diff_task_definitions`, `query_incident_memory`) and creates/updates an `IncidentRecord`.
        3.  `get_service_logs_and_metrics(service: str, limit: int = 20)`: Pulls CloudWatch logs and metric series (`error_rate_pct`, `p95_latency_ms`, `memory_utilization_pct`, `queue_depth`) for a specific service.
        4.  `diff_service_deployments(service: str)`: Compares the active task definition revision against the previous revision (`N` vs. `N-1`).
        5.  `inspect_iam_and_guardrails(test_command: str = "")`: Returns the agent's IAM policy summary and optionally validates a command against `GuardrailedAWSCLI` (allowing the user to test blocked commands like `aws ecs delete-cluster` safely).
        6.  `propose_service_remediation(service: str, action_type: str, target_value: str, reason: str)`: Prepares a human-approval-gated `RemediationProposal` (`rollback_task_definition`, `update_service_env`, or `scale_service`) without executing the write immediately.
        7.  `generate_operational_artifacts(service: str, include_chart: bool = True, include_excel: bool = True, include_word: bool = True, include_markdown: bool = True)`: Generates `.svg` metric charts, `.xlsx` event timelines, `.docx` Word reports, and `.md` post-incident summaries for inline display and download.
        8.  `run_readonly_aws_cli(command: str)`: Executes an allowlisted read-only `aws ecs ...`, `aws logs ...`, or `aws cloudwatch ...` command through `GuardrailedAWSCLI`.
3.  **Loop Bounding & Offline Parity**:
    *   When live Vertex AI ADC (`gemini-3.8-flash`) or `GEMINI_API_KEY` is active, `chat_with_tools()` passes the system prompt, the last 16 conversation messages (truncated to compact text + tool summaries), any user attachments, and the 8 tool declarations to `client.models.generate_content`. If the model returns `function_calls`, the orchestrator executes each tool, appends the `function_response` parts, and repeats up to `MAX_TOOL_ROUNDS = 6` before synthesizing the final plain-English markdown response.
    *   If `CLOUD_OPS_OFFLINE_LLM=1` or Vertex AI is unreachable, a deterministic fallback planner selects and executes the matching tools from the same 8-tool registry based on conversation context and user intent, guaranteeing identical response structures (`content`, `executed_commands`, `proposal`, `artifacts`) in offline unit tests.

### 5. HTTP API Contract Extensions (`src/cloud_ops/api/server.py`)

All existing endpoints (`/api/health`, `/api/state`, `/api/chat`, `/api/investigate`, `/api/incidents/<id>/approve|reject|artifacts`, `/api/scenarios/inject`, `/api/webhooks/slack|teams`) remain backward-compatible. New and extended endpoints:

*   `GET /api/conversations`: Returns `{ "conversations": [ ...summary items... ], "active_conversation_id": "..." }`.
*   `POST /api/conversations`: Creates a new empty `ConversationSession` (optional `{ "title": "..." }`) and returns `{ "conversation": { ... } }`.
*   `GET /api/conversations/<conversation_id>`: Returns full conversation with all `messages`, `executed_commands`, `proposal`, `verification`, and `artifacts`.
*   `DELETE /api/conversations/<conversation_id>`: Deletes a conversation thread from `ConversationStore`.
*   `POST /api/conversations/<conversation_id>/messages` (and `POST /api/chat` with optional `conversation_id`): Appends the user message, runs the tool loop, persists the assistant reply, and returns `{ "reply": "...", "conversation_id": "...", "message": { ...ChatMessageRecord... }, "conversation": { ... }, "incident": { ... } }`.
*   `POST /api/incidents/<incident_id>/approve` & `POST /api/incidents/<incident_id>/reject`: Extended to accept optional `{ "conversation_id": "...", "message_id": "..." }` so approving or rejecting an inline proposal card updates the proposal state inside `ConversationStore` and appends the assistant's post-execution verification message (with its own `executed_commands` drawer and updated recovery chart) directly to that conversation thread.

### 6. Two-Column Conversational UI Architecture (`src/cloud_ops/ui/`)

The frontend (`index.html`, `styles.css`, `app.js`) is replaced with a clean two-column layout:

1.  **Left Sidebar (`<aside class="chat-sidebar">`)**:
    *   Brand header (`Cloud Ops Agent`) + primary **`+ New Chat`** button (`#btnNewChat`).
    *   Scrollable **`Recent Conversations`** list (`#conversationList`) showing conversation titles, relative timestamps, and a hover delete button.
    *   Minimal footer badge displaying connection state (`🟢 Connected to AWS • Gemini`).
2.  **Main Conversation Area (`<main class="chat-main">`)**:
    *   **Welcome Empty State (`#emptyChatState`)**: Shown when a conversation has 0 messages; displays a friendly greeting and 4 plain-English starter prompt cards (for example, *"Check if all our AWS services are healthy right now"*, *"Why is checkout throwing errors and show me a chart"*, *"Scale down SalesCreate for off-business hours"*, *"Export today's service events to Excel and a Word report"*).
    *   **Message Stream (`#chatMessages`)**: Centered (`max-width: 820px`) list of user and assistant turns. Each assistant turn renders:
        *   **Collapsible Command Drawer (`<details class="cmd-drawer">`)**: Summary pill showing `▸ Executed N AWS & MCP commands (click to view)`. Expanding it reveals each command's tool name, exact `aws ...` CLI string, `LIVE_AWS`/`SIMULATION` badge, `ALLOWED`/`BLOCKED` badge, and output snippet.
        *   **Formatted Narrative**: Clean Markdown rendering (headings, bold, lists, inline code, tables) with `text-wrap: balance` on headings and `text-wrap: pretty` on body paragraphs.
        *   **Inline Multimodal Visualizations & Downloads**: Any `.svg` chart artifact renders directly inside the assistant message bubble as a crisp visual figure; `.xlsx`, `.docx`, and `.md` artifacts render as one-click download cards (`📊 Excel Event Timeline`, `📄 Word Post-Incident Report`, `📝 Markdown Summary`).
        *   **Inline Human Approval Card**: When `message.proposal` is present, renders a clear card inside the message explaining the proposed change in plain English, a collapsible `<details>` toggle for the exact AWS CLI command and rollback plan, and **✓ Approve & Execute** / **✕ Cancel** buttons.
    *   **Bottom Composer (`<footer class="chat-composer">`)**:
        *   Attachment preview strip (`#attachmentStrip`) for uploaded screenshots, architecture diagrams, or runbooks.
        *   `📎` file upload button (`<input type="file" multiple>`), auto-resizing `<textarea id="chatInput">`, and **Send** button (`#btnSend`).

--------------------------------------------------------------------------------

## Alternatives Considered

### Alternative A: Retain the Split-Screen Inspector Dashboard and Only Hide the Top Fault-Injector Bar

*   **Pros**: Requires minimal HTML/CSS changes to `src/cloud_ops/ui/index.html` and reuses existing DOM binding functions in `src/cloud_ops/ui/app.js`.
*   **Cons**: Forces every user interaction into a single active `IncidentRecord` view on the right half of the screen. When an operator asks general operational questions (such as *"List all services in our cluster"*, *"Check IAM write permissions"*, or *"Compare the last two deployments"*), most of the seven right-hand inspector cards remain blank or display stale data from a prior incident, while the chat column remains cramped at `380px` width.
*   **Reason for Rejection**: Does not satisfy the core requirement for a human-like conversational interface where charts, file downloads, approval cards, and expandable command logs appear contextually inline within the conversation stream.

### Alternative B: Let Gemini Generate Raw `aws ...` Shell Strings Directly Without Typed MCP Tools

*   **Pros**: Avoids defining structured tool schemas in `GeminiReasoningEngine` and `CloudOpsOrchestrator`.
*   **Cons**: Free-form CLI string generation frequently produces invalid flag combinations, unpaginated JSON dumps that exceed the model context window, or inconsistent task-definition diffs. It also makes offline deterministic unit testing (`CLOUD_OPS_OFFLINE_LLM=1`) fragile because tests would depend on exact string matching of generated shell commands.
*   **Reason for Rejection**: Combining 8 typed MCP tools (`list_ecs_services`, `investigate_service`, `get_service_logs_and_metrics`, `diff_service_deployments`, `inspect_iam_and_guardrails`, `propose_service_remediation`, `generate_operational_artifacts`, and `run_readonly_aws_cli`) with `GuardrailedAWSCLI` gives the model reliable high-level operations (like revision `N` vs. `N-1` diffing and OpenXML `.xlsx`/`.docx` generation) while still recording the exact underlying `aws ...` CLI commands for the collapsible command drawer.

### Alternative C: Persist Conversation History Exclusively in Browser `localStorage`

*   **Pros**: Requires zero backend storage changes or new `/api/conversations` endpoints.
*   **Cons**: Browser-only storage breaks server-side correlation between multi-turn conversation state, pending `RemediationProposal` HMAC approval gates, and generated `.xlsx`/`.svg`/`.docx`/`.md` files on disk. Clearing browser storage or opening the URL from another tab loses all conversation history and active approval states.
*   **Reason for Rejection**: Persisting threads on the backend in `data/conversations.json` (and `/tmp/cloud-ops-agent/data/conversations.json` on Vercel) keeps multi-turn context, inline approval states, and per-message command execution logs synchronized across reloads and API clients.

--------------------------------------------------------------------------------

## Non-Functional Requirements

*   **Security & Access Control**:
    *   All AWS CLI commands invoked by MCP servers or `run_readonly_aws_cli` must pass through `GuardrailedAWSCLI.validate_command()`.
    *   Destructive operations (`ecs:DeleteCluster`, `ecs:DeleteService`, `ecs:DeregisterTaskDefinition`, `iam:*`, `ec2:TerminateInstances`) are blocked unconditionally.
    *   Mutating operations (`ecs:UpdateService`, `ecs:RegisterTaskDefinition`, `application-autoscaling:RegisterScalableTarget`) cannot execute during the autonomous Gemini tool loop; they can only create a `PENDING_APPROVAL` proposal and require an explicit human approval request bearing a valid HMAC-SHA256 token.
*   **Performance, Latency & Token Bounds**:
    *   The Gemini tool-calling loop is capped at `MAX_TOOL_ROUNDS = 6` per user turn with `ThinkingConfig(thinking_budget=0)` on `gemini-3.8-flash` to keep per-turn LLM latency low and prevent runaway API loops.
    *   CloudWatch log payloads returned to the Gemini tool loop are capped at 25 recent log events (truncated to 400 characters per line) and conversation context sent to Vertex AI is bounded to the most recent 16 messages.
*   **Backward Compatibility & Rollback**:
    *   All existing `CloudOpsOrchestrator` methods (`start_investigation`, `approve_remediation`, `reject_remediation`, `generate_artifacts`, `inject_scenario`, `get_dashboard_state`, `handle_chat`) and HTTP routes (`/api/health`, `/api/state`, `/api/chat`, `/api/investigate`, `/api/incidents/<id>/approve|reject|artifacts`, `/api/webhooks/slack|teams`) remain intact so existing unit tests and ChatOps webhooks continue to pass without modification.

--------------------------------------------------------------------------------

### Additional Resources

*   Concept Ideation Brief: [concepts_brief.md](./concepts_brief.md)
*   Existing IAM Policy Specification: [infra/agent_iam_policy.json](./infra/agent_iam_policy.json)

### Future Improvements

*   **Server-Sent Events (SSE) Streaming**: Stream intermediate tool-execution pills to the browser in real time as each round of `chat_with_tools()` completes, rather than returning the completed `executed_commands` array at the end of the turn.

--------------------------------------------------------------------------------

## Detailed Implementation Plan

### Implementation Phasing & Dependency Checkpoints

The implementation is partitioned into three sequential dependency tiers so that each phase leaves the repository in a passing, testable state:

*   **Phase A (Tier 0 — Storage, Config & Dynamic AWS/MCP Layer)**:
    *   Files: `src/cloud_ops/config.py`, `src/cloud_ops/memory/conversation_store.py`, `data/conversations.json`, `infra/agent_iam_policy.json`, `src/cloud_ops/mcp_servers/aws_cli_guard.py`, `src/cloud_ops/mcp_servers/cloudwatch_mcp.py`, `src/cloud_ops/mcp_servers/ecs_mcp.py`.
    *   *Checkpoint*: `python -m unittest discover -s tests -p "test_*.py"` passes all existing 11 tests while enabling arbitrary ECS service names and persistent `ConversationStore` CRUD.
*   **Phase B (Tier 1 — Parallel Work Units: Backend Tool-Calling Engine vs. Two-Column Chatbot UI)**:
    *   **Work Unit B1 (Backend LLM & Orchestrator + HTTP Routes)**: `src/cloud_ops/llm/gemini_engine.py`, `src/cloud_ops/orchestrator.py`, `src/cloud_ops/api/server.py`.
    *   **Work Unit B2 (Frontend Chatbot UI)**: `src/cloud_ops/ui/index.html`, `src/cloud_ops/ui/styles.css`, `src/cloud_ops/ui/app.js`.
    *   *Checkpoint*: Both work units own non-overlapping files and expose the exact `/api/conversations` and `ChatMessageRecord` JSON contract defined in the Technical Plan.
*   **Phase C (Tier Final — End-to-End Integration & Playwright Visual Verification)**:
    *   Files: `tests/test_cloud_ops.py`, `tests/test_api_server.py`.
    *   *Checkpoint*: Full unit/API test suite passes and Playwright headless browser verification confirms the two-column conversational UI, collapsible command drawer, inline approval card, and inline multimodal artifacts render without horizontal overflow.

### Target Files to Create/Modify

#### 1. `src/cloud_ops/config.py` (Modify)

*   **Type / Scope**: Python Configuration Module.
*   **Rationale**: Ensure `VERCEL=1` serverless mode seeds `conversations.json` into `/tmp/cloud-ops-agent/data/conversations.json` alongside `memory_store.json` so conversation history write operations succeed on read-only serverless filesystems.
*   **Proposed Changes**:
    *   In `AgentConfig.from_env()`, when `is_vercel` is `True`, copy `ROOT_DIR / "data" / "conversations.json"` to `data_dir / "conversations.json"` if `seed_conversations.exists() and not target_conversations.exists()`.

#### 2. `src/cloud_ops/memory/conversation_store.py` (Create)

*   **Type / Scope**: Python Persistent Conversation Storage Module.
*   **Rationale**: Provide multi-turn conversation thread persistence (`ConversationSession`, `ChatMessageRecord`, `ExecutedCommandRecord`) backed by `data/conversations.json` so users can start new chats, switch across past conversation threads, and inspect per-message executed commands, inline proposals, and artifacts across browser reloads.
*   **Proposed Changes**:
    *   Define dataclasses `ExecutedCommandRecord`, `ChatMessageRecord`, and `ConversationSession` with `.to_dict()` and `.from_dict(data)` serialization helpers.
    *   Define class `ConversationStore(data_dir: Path)`:
        *   `list_conversations() -> list[dict[str, Any]]`: Returns conversation summaries (`conversation_id`, `title`, `created_at`, `updated_at`, `message_count`, `active_service`, `active_incident_id`) sorted by `updated_at` descending.
        *   `create_conversation(title: str | None = None) -> ConversationSession`: Creates a new session with ID `conv-<YYYYMMDDHHMMSS>-<6_hex>`, default title `"New Conversation"`, persists to `conversations.json`, and sets `active_conversation_id`.
        *   `get_conversation(conversation_id: str) -> ConversationSession | None`: Looks up a session by ID.
        *   `get_or_create_active(conversation_id: str | None = None) -> ConversationSession`: Returns the requested session if found, the most recent session if `conversation_id` is omitted and sessions exist, or creates a new session.
        *   `append_message(conversation_id: str, message: ChatMessageRecord, active_service: str | None = None, active_incident_id: str | None = None) -> ConversationSession`: Appends a message, auto-titles the conversation from the first user message (truncating to 48 characters at a word boundary if title is still `"New Conversation"`), updates `updated_at`, and writes atomically to `conversations.json`.
        *   `update_proposal_status(incident_id: str, proposal_dict: dict[str, Any], verification_dict: dict[str, Any] | None = None) -> str | None`: Scans messages across conversations for any message referencing `incident_id` or `proposal_dict["proposal_id"]`, updates `message.proposal` and `message.verification` in-place, saves to disk, and returns the matching `conversation_id`.
        *   `delete_conversation(conversation_id: str) -> bool`: Removes the session from `conversations.json` and updates `active_conversation_id`.

#### 3. `data/conversations.json` (Create)

*   **Type / Scope**: JSON Seed Data File.
*   **Rationale**: Seed the repository with one historical example conversation thread (`conv-seed-salescreate`) demonstrating an investigated incident, an expandable command drawer, and generated artifacts, while keeping the initial state valid JSON (`{"active_conversation_id": "conv-seed-salescreate", "conversations": [...]}`).
*   **Proposed Changes**:
    *   Create `data/conversations.json` containing the top-level keys `active_conversation_id` and `conversations` list matching the `ConversationSession` schema.

#### 4. `infra/agent_iam_policy.json` (Modify)

*   **Type / Scope**: IAM Policy Specification JSON.
*   **Rationale**: Ensure `InvestigatorAndVerifierReadOnly` retains `ecs:ListServices` and `logs:DescribeLogGroups` while widening `RemediatorAllowlistedWriteOnDemoCluster` resource ARNs to cover any dynamic ECS service or task definition deployed to `cloud-ops-demo-cluster`.
*   **Proposed Changes**:
    *   In `RemediatorAllowlistedWriteOnDemoCluster`, add `"arn:aws:ecs:*:*:service/cloud-ops-demo-cluster/*"` and `"arn:aws:ecs:*:*:task-definition/*:*"` to the `Resource` array.

#### 5. `src/cloud_ops/mcp_servers/aws_cli_guard.py` (Modify)

*   **Type / Scope**: Guardrailed AWS CLI Validator & Executor.
*   **Rationale**: Provide a helper to slice newly appended audit log entries per chat turn or tool call (`self.audit_log` stores `dict[str, Any]` entries).
*   **Proposed Changes**:
    *   Verify `READ_ONLY_PREFIXES` permits `ecs list-services` and `logs describe-log-groups`.
    *   Add method `get_audit_slice(start_index: int) -> list[dict[str, Any]]` returning `[dict(entry) for entry in self.audit_log[start_index:]]`.

#### 6. `src/cloud_ops/mcp_servers/cloudwatch_mcp.py` (Modify)

*   **Type / Scope**: CloudWatch Logs & Metrics MCP Server.
*   **Rationale**: Remove hardcoded `SUPPORTED_SERVICES` failures and `self._modules[service]` `KeyError` crashes so arbitrary ECS services deployed via GitHub Actions can be queried for CloudWatch logs and metrics, auto-resolving the log group name when provided or falling back to standard ECS log group naming patterns.
*   **Proposed Changes**:
    *   Add `cluster: str = "cloud-ops-demo-cluster"` to `CloudWatchMCPServer.__init__` and store `self.cluster = cluster`.
    *   Modify `_validate_service(service: str) -> str` to strip whitespace and validate that `service` matches `^[A-Za-z0-9_.-]+$` instead of raising `ValueError` when `service not in SUPPORTED_SERVICES`. If a service is not yet in `self.active_scenarios`, initialize `self.active_scenarios[svc] = "healthy"` and `self.custom_env_overrides.setdefault(svc, {})`.
    *   Add safe fallback handling whenever `service not in self._modules`:
        *   `get_baseline_env(service)` returns `{"SERVICE_NAME": svc, "RELEASE_VERSION": "v1.0.0", "FAULT_MODE": "healthy"}`.
        *   `get_resolved_env(service, scenario=None)` returns the baseline dict merged with `{"FAULT_MODE": scenario or self.active_scenarios.get(svc, "healthy")}` and `self.custom_env_overrides.get(svc, {})`.
        *   `set_service_scenario(service, scenario, env_overrides=None)` updates `self.active_scenarios[svc]` and `self.custom_env_overrides[svc]` without indexing `self._modules` when `svc not in self._modules`.
        *   `list_available_scenarios(service=None)` returns `{"healthy": "Normal operation"}` for any dynamic service not in `self._modules`.
    *   In `get_recent_logs(self, service: str, limit: int = 20, scenario: str | None = None, log_group_override: str | None = None)`:
        *   When `self.live_aws` is `True`, probe candidate log groups in order: `[log_group_override, f"/ecs/{self.cluster}/{svc}", f"/ecs/cloud-ops-demo/{svc}", f"/ecs/{svc}"]` (filtering out `None`), executing `aws logs filter-log-events --log-group-name <group> --limit <limit> --region <region>` via `self.cli_guard.execute_command()` until events are returned.
        *   When `self.live_aws` is `False` (or when live CloudWatch returns no events) and `svc not in self._modules`, synthesize structured JSON log events and metric series so offline queries never crash.

#### 7. `src/cloud_ops/mcp_servers/ecs_mcp.py` (Modify)

*   **Type / Scope**: Amazon ECS MCP Server.
*   **Rationale**: Enable cluster-wide service discovery (`list_ecs_services`), allow `describe_ecs_service` and `diff_task_definitions` on any ECS service name without raising `ValueError`, and compute live task-definition diffs between active revision `N` and prior revision `N-1` directly from AWS ECS task definitions.
*   **Proposed Changes**:
    *   Update `_validate_service(service: str) -> str` to accept any valid service identifier matching `^[A-Za-z0-9_.-]+$` and ensure `self._task_revisions`, `self._desired_counts`, and `self._deployment_history` initialize defaults for unseen services.
    *   Add method `list_ecs_services(self) -> dict[str, Any]`:
        *   Validates and executes `aws ecs list-services --cluster <cluster> --region <region>` via `self.cli_guard.execute_command()`.
        *   When `self.live_aws` is `True` and `serviceArns` are returned, extracts each service short name (`arn.split("/")[-1]`), calls `self.describe_ecs_service(name)`, and returns `{"cluster": self.cluster, "services": [...]}`.
        *   Otherwise returns descriptions for all services registered in `self.cloudwatch.active_scenarios`.
    *   In `describe_ecs_service(self, service: str)`:
        *   When `self.live_aws` is `True`, also inspect the active task definition (`aws ecs describe-task-definition`) to extract `log_group` from `containerDefinitions[0].logConfiguration.options["awslogs-group"]` and include `"log_group"` in the returned dict.
    *   In `diff_task_definitions(self, service: str, baseline_revision: int | None = None, active_revision: int | None = None) -> TaskDefDiff`:
        *   When `self.live_aws` is `True`, query `aws ecs list-task-definitions --family-prefix <svc> --sort DESC --max-items 5 --region <region>`. Identify the active revision (from `describe_ecs_service` or latest) and the immediately preceding revision (`N-1`), call `aws ecs describe-task-definition` for both, and populate `TaskDefDiff.changed_env` (including any added/removed env keys or container image changes with `baseline` and `active` values) and `release_change`.

#### 8. `src/cloud_ops/llm/gemini_engine.py` (Modify)

*   **Type / Scope**: Vertex AI / Gemini Reasoning & Tool-Calling Engine.
*   **Rationale**: Add a bounded multi-turn tool-calling runner (`chat_with_tools`) using `gemini-3.8-flash` function declarations on Vertex AI ADC, with a deterministic offline fallback dispatcher when `CLOUD_OPS_OFFLINE_LLM=1` or credentials are unavailable.
*   **Proposed Changes**:
    *   Define constant `MAX_TOOL_ROUNDS = 6`.
    *   Add method `chat_with_tools(self, messages: list[dict[str, Any]], attachments: list[dict[str, str]], tool_declarations: list[dict[str, Any]], tool_executor: Callable[[str, dict[str, Any]], dict[str, Any]], active_service: str | None = None) -> dict[str, Any]`:
        *   When `self.live_sdk_available` is `True`:
            *   Build `google.genai.types.Tool(function_declarations=...)` from `tool_declarations` and pass `GenerateContentConfig(temperature=0.2, thinking_config=ThinkingConfig(thinking_budget=0), tools=[tool_obj], system_instruction=...)`.
            *   Loop up to `MAX_TOOL_ROUNDS`: inspect `response.function_calls`; if present, invoke `tool_executor(fc.name, dict(fc.args))` for each call, append the function call and function response contents, and call `generate_content` again. When no further function calls are returned (or `MAX_TOOL_ROUNDS` is reached), return `{"reply": final_text, "mode": self.auth_mode}`.
        *   When `self.live_sdk_available` is `False` (or if a runtime API exception occurs):
            *   Invoke `_deterministic_tool_chat(messages, attachments, tool_executor, active_service)` which inspects the latest user message and conversation context, invokes the appropriate tools through `tool_executor` (ensuring identical `executed_commands`, `proposal`, and `artifacts` side effects), and formats a clear, plain-English response.

#### 9. `src/cloud_ops/orchestrator.py` (Modify)

*   **Type / Scope**: Multi-Agent & Conversation Orchestrator.
*   **Rationale**: Integrate `ConversationStore`, expose the 8 callable tools to `GeminiReasoningEngine.chat_with_tools()`, capture per-message `executed_commands` slices, pause on mutating operations to attach inline `PENDING_APPROVAL` proposals, and append verification messages to the conversation thread when approvals execute while keeping `approve_remediation` and `reject_remediation` return types (`IncidentRecord`) backward-compatible.
*   **Proposed Changes**:
    *   Instantiate `self.conversation_store = ConversationStore(self.config.data_dir)` in `CloudOpsOrchestrator.__init__()` and pass `cluster=self.config.ecs_cluster` to `CloudWatchMCPServer`.
    *   Add conversation management methods:
        *   `list_conversations() -> dict[str, Any]`
        *   `create_conversation(title: str | None = None) -> dict[str, Any]`
        *   `get_conversation(conversation_id: str) -> dict[str, Any] | None`
        *   `delete_conversation(conversation_id: str) -> bool`
    *   Update `handle_chat(self, message: str, attachments: list[dict[str, Any]] | None = None, conversation_id: str | None = None) -> dict[str, Any]`:
        *   Resolve or create the `ConversationSession` via `self.conversation_store.get_or_create_active(conversation_id)`.
        *   Append the user `ChatMessageRecord` to the session.
        *   Execute `self.gemini.chat_with_tools(...)` against the 8-tool registry (`list_ecs_services`, `investigate_service`, `get_service_logs_and_metrics`, `diff_service_deployments`, `inspect_iam_and_guardrails`, `propose_service_remediation`, `generate_operational_artifacts`, `run_readonly_aws_cli`).
        *   Inside the orchestrator's `tool_executor(tool_name, args)`, snapshot `step_audit_start = len(self.cli_guard.audit_log)` before running the tool, summarize the tool's output into `output_summary` (`<= 600` chars), and map each new entry in `self.cli_guard.get_audit_slice(step_audit_start)` into an `ExecutedCommandRecord` (`tool_name`, `command`, `status` in `ALLOWED`/`REQUIRES_APPROVAL`/`BLOCKED`, `execution_mode` in `LIVE_AWS`/`SIMULATION`/`BLOCKED`, `output_summary`, `timestamp`).
        *   Enrich generated artifacts with `url` (`/artifacts/<name>`), `preview_url` (`/artifacts/<name>`), and `download_url` (`/artifacts/<name>?download=1`), attach any created `proposal` (`PENDING_APPROVAL`) and `artifacts` to the assistant `ChatMessageRecord`, persist to `self.conversation_store`, and return `{ "reply": ..., "conversation_id": ..., "message": ..., "conversation": ..., "incident": ..., "blocked": ... }`.
    *   Update `approve_remediation(self, incident_id: str, approver: str = "ops-engineer@team", conversation_id: str | None = None) -> IncidentRecord` and `reject_remediation(self, incident_id: str, reason: str = "Rejected by human operator", conversation_id: str | None = None) -> IncidentRecord`:
        *   Capture `audit_start = len(self.cli_guard.audit_log)` before executing the approved command and `VerifierAgent.verify_remediation()`.
        *   Update the existing inline proposal status via `self.conversation_store.update_proposal_status(...)` and append a follow-up assistant verification `ChatMessageRecord` (containing the executed write + verification commands in `executed_commands`, the `VerificationResult`, and updated artifacts) to the conversation thread, while still returning the `IncidentRecord` instance for full backward compatibility.

#### 10. `src/cloud_ops/api/server.py` (Modify)

*   **Type / Scope**: Standard-Library HTTP API & Static Server.
*   **Rationale**: Add `/api/conversations` CRUD routes (`GET`, `POST`, `DELETE`) and wire `conversation_id` through `/api/chat`, `/api/incidents/<id>/approve`, and `/api/incidents/<id>/reject`.
*   **Proposed Changes**:
    *   Add `do_DELETE(self)` to `CloudOpsRequestHandler` and add `"DELETE"` to `Access-Control-Allow-Methods` in `_send_cors_headers()`.
    *   In `do_GET(self)`:
        *   Route `GET /api/conversations` to `self.orchestrator.list_conversations()`.
        *   Route `GET /api/conversations/<conversation_id>` to `self.orchestrator.get_conversation(conversation_id)` (404 if not found).
    *   In `do_POST(self)`:
        *   Route `POST /api/conversations` to `self.orchestrator.create_conversation(title=body.get("title"))`.
        *   Route `POST /api/conversations/<conversation_id>/messages` to `self.orchestrator.handle_chat(message=..., attachments=..., conversation_id=conversation_id)`.
        *   Update `POST /api/chat` to pass `conversation_id=body.get("conversation_id")`.
        *   Update `POST /api/incidents/<id>/approve` and `reject` to pass `conversation_id=body.get("conversation_id")`.
    *   In `do_DELETE(self)`:
        *   Route `DELETE /api/conversations/<conversation_id>` to `self.orchestrator.delete_conversation(conversation_id)`.

#### 11. `src/cloud_ops/ui/index.html` (Modify)

*   **Type / Scope**: Web Console HTML Entrypoint.
*   **Rationale**: Replace the multi-panel telemetry dashboard and fault-injector bar with a two-column conversational chatbot layout (`<aside class="chat-sidebar">` + `<main class="chat-main">`).
*   **Proposed Changes**:
    *   Remove the 5-stage agent pipeline bar, the 3-service telemetry/fault-injection grid, and the 7-card right-hand inspector column.
    *   Define `<div class="app-layout">` containing:
        *   `<aside class="chat-sidebar" id="chatSidebar">`: Brand header, `<button id="btnNewChat">+ New Chat</button>`, scrollable `<nav id="conversationList" aria-label="Conversation History"></nav>`, and connection status footer (`#connectionBadge`).
        *   `<main class="chat-main">`: Mobile/compact top bar with sidebar toggle and current conversation title (`#activeConvTitle`), scrollable message container (`#chatScrollWrap`) housing the welcome empty state (`#emptyChatState` with 4 starter prompt cards) and `#chatMessages`, and bottom composer (`<footer class="chat-composer">`) with `#attachmentStrip`, hidden `<input type="file" id="fileInput" multiple>`, `<button id="btnAttach">`, auto-expanding `<textarea id="chatInput">`, and `<button id="btnSend">`.
        *   Native `<dialog id="imagePreviewDialog" closedby="any">` for full-size inspection of inline charts or attached architecture diagrams.

#### 12. `src/cloud_ops/ui/styles.css` (Modify)

*   **Type / Scope**: Stylesheet for Two-Column Conversational Interface.
*   **Rationale**: Eliminate horizontal viewport overflow and style the ChatGPT-like sidebar, centered `820px` conversation stream, collapsible `<details class="cmd-drawer">` command execution drawers, inline SVG chart figures, download pills, and inline approval cards.
*   **Proposed Changes**:
    *   Define `grid-template-columns: 260px minmax(0, 1fr)` on `.app-layout` (`height: 100dvh; overflow: hidden`) with responsive collapse below `860px`.
    *   Apply `text-wrap: balance` to headings (`h1, h2, h3, .welcome-title, .proposal-title`) and `text-wrap: pretty` to message body copy (`.msg-body p, .msg-body li`) per `modern-web-guidance`.
    *   Style `.cmd-drawer` (`<details>` / `<summary>`), `.cmd-item` (monospace command string, status badges `ALLOWED`/`BLOCKED`, mode badges `LIVE_AWS`/`SIMULATION`, collapsible output), `.inline-approval-card`, `.inline-chart-card`, and `.artifact-pills`.

#### 13. `src/cloud_ops/ui/app.js` (Modify)

*   **Type / Scope**: Frontend Conversation State, Markdown/Artifact Renderer & API Client.
*   **Rationale**: Drive multi-conversation switching, new chat creation, file/image attachment encoding, inline collapsible command drawers, inline chart rendering, and inline human approval buttons against `/api/conversations` and `/api/incidents/<id>/approve|reject`.
*   **Proposed Changes**:
    *   Maintain `appState = { conversations: [], activeConversationId: null, activeConversation: null, attachments: [], sending: false }` and preserve `resolveApiUrl(path)` (`?api=` override support).
    *   Implement `loadConversations()`, `selectConversation(id)`, `createNewConversation()`, and `deleteConversation(id)`.
    *   Implement `renderConversation(conv)` to toggle `#emptyChatState` vs. `#chatMessages` and render each `ChatMessageRecord`:
        *   Assistant messages render: (1) `<details class="cmd-drawer">` if `executed_commands.length > 0`, (2) formatted Markdown narrative, (3) inline `.svg` chart previews (`resolveApiUrl(art.preview_url || art.url)`) and `.xlsx`/`.docx`/`.md` download cards (`resolveApiUrl(art.download_url || `${art.url}?download=1`)`) if `artifacts.length > 0`, and (4) the inline approval card if `proposal` is present (`PENDING_APPROVAL` shows **✓ Approve & Execute** and **✕ Cancel** buttons; `APPROVED_AND_EXECUTED` shows verification badge).
    *   Implement `handleInlineApprove(incidentId, conversationId)` and `handleInlineReject(incidentId, conversationId)` which call `/api/incidents/${incidentId}/approve` (or `reject`) with `{ conversation_id: conversationId }` and re-render the updated conversation thread.

#### 14. `tests/test_cloud_ops.py` & `tests/test_api_server.py` (Modify)

*   **Type / Scope**: Automated Unit & Integration Test Suites.
*   **Rationale**: Verify all existing tests continue to pass while adding coverage for dynamic arbitrary ECS service names, `ConversationStore` persistence, `/api/conversations` CRUD endpoints, per-message `executed_commands` drawers, and inline approval updates.
*   **Proposed Changes**:
    *   In `tests/test_cloud_ops.py`: Add `test_07_dynamic_arbitrary_ecs_service_and_conversation_store` verifying that querying an arbitrary external service (e.g., `"checkout-payment-ecs"`) does not raise `ValueError`, populates `executed_commands` on the assistant message, and persists to `conversations.json`.
    *   In `tests/test_api_server.py`: Add `test_06_conversations_crud_and_inline_approval_flow` testing `GET /api/conversations`, `POST /api/conversations`, `POST /api/conversations/<id>/messages`, inline approval with `conversation_id`, and `DELETE /api/conversations/<id>`.

### Verification Plan

*   **Unit & Integration Tests**: Run `C:\googie\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v` (all existing 11 tests + 2 new test suites = 13 tests must pass with 0 errors).
*   **Live Local Server & Vertex AI ADC Health Check**: Start `.\run_local.ps1` and verify `GET http://127.0.0.1:8000/api/health` and `GET http://127.0.0.1:8000/api/conversations` return `200 OK`.
*   **Playwright Visual & Interactive E2E Check**: Launch headless Chromium/Edge via Playwright (`1440x900` and `390x844`), verify zero horizontal scroll overflow (`document.documentElement.scrollWidth <= window.innerWidth`), send a multi-turn diagnostic + chart/report request, expand and retract the `<details class="cmd-drawer">` command drawer, and capture verification screenshots.

--------------------------------------------------------------------------------

<!-- Provenance: Indicates initial structural drafting via EGM. -->

*Drafted with EGM*
<!-- Provenance: Indicates the document passed the adversarial Goldfish review. -->

*Reviewed with EGM*




