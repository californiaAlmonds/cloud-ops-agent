/**
 * Cloud Ops Agent — Two-Column Conversational AWS Assistant Frontend
 * Supports multi-turn conversation history, collapsible AWS/MCP command drawers,
 * inline SVG charts, Excel/Word report downloads, and inline human approval cards.
 */

const state = {
  conversations: [],
  activeConversationId: null,
  activeConversation: null,
  attachments: [],
  sending: false,
  sidebarCollapsed: false,
};

// Keep alias for any external callers inspecting window.appState
window.appState = state;

// ---------------------------------------------------------------------------
// API Base URL Resolution (?api= query parameter / storage override)
// ---------------------------------------------------------------------------
const API_BASE_URL = (() => {
  try {
    const params = new URLSearchParams(window.location.search);
    const fromQuery = params.get("api");
    if (fromQuery) {
      const cleaned = fromQuery.replace(/\/+$/, "");
      window.localStorage.setItem("CLOUD_OPS_API_BASE", cleaned);
      window.sessionStorage.setItem("CLOUD_OPS_API_BASE", cleaned);
      return cleaned;
    }
    return (
      window.CLOUD_OPS_API_BASE ||
      window.sessionStorage.getItem("CLOUD_OPS_API_BASE") ||
      window.localStorage.getItem("CLOUD_OPS_API_BASE") ||
      ""
    );
  } catch {
    return "";
  }
})();

function resolveApiUrl(path) {
  if (!path) return "";
  if (!API_BASE_URL || /^https?:\/\//i.test(path) || path.startsWith("data:")) {
    return path;
  }
  return `${API_BASE_URL}${path.startsWith("/") ? path : `/${path}`}`;
}

async function apiGet(path) {
  const res = await fetch(resolveApiUrl(path), {
    headers: { Accept: "application/json" },
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || `GET ${path} failed (${res.status})`);
  }
  return res.json();
}

async function apiPost(path, payload = {}) {
  const res = await fetch(resolveApiUrl(path), {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || `POST ${path} failed (${res.status})`);
  }
  return res.json();
}

async function apiDelete(path) {
  const res = await fetch(resolveApiUrl(path), {
    method: "DELETE",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || `DELETE ${path} failed (${res.status})`);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// Light-Dismiss Fallback for <dialog closedby="any">
// ---------------------------------------------------------------------------
function initModernDialogs() {
  const dialogs = document.querySelectorAll("dialog");
  dialogs.forEach((dialog) => {
    if (!("closedBy" in HTMLDialogElement.prototype)) {
      dialog.addEventListener("click", (event) => {
        if (event.target !== dialog) return;
        const rect = dialog.getBoundingClientRect();
        const isInDialog =
          rect.top <= event.clientY &&
          event.clientY <= rect.top + rect.height &&
          rect.left <= event.clientX &&
          event.clientX <= rect.left + rect.width;
        if (!isInDialog) {
          dialog.close();
        }
      });
    }
  });
}

function openImageLightbox(src, title = "Chart & Diagram Preview") {
  const dialog = document.getElementById("imagePreviewDialog");
  const img = document.getElementById("previewDialogImg");
  const titleEl = document.getElementById("previewDialogTitle");
  if (!dialog || !img) return;
  img.src = src;
  if (titleEl) titleEl.textContent = title || "Chart & Diagram Preview";
  dialog.showModal();
}

// ---------------------------------------------------------------------------
// Formatting & Markdown Renderer
// ---------------------------------------------------------------------------
function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatRelativeTime(isoString) {
  if (!isoString) return "";
  const dt = new Date(isoString);
  if (Number.isNaN(dt.getTime())) return "";
  const diffSec = Math.max(0, Math.floor((Date.now() - dt.getTime()) / 1000));
  if (diffSec < 45) return "just now";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
  return dt.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function formatFileSize(bytes) {
  const num = Number(bytes || 0);
  if (!num || num < 1024) return `${num || 0} B`;
  if (num < 1024 * 1024) return `${(num / 1024).toFixed(1)} KB`;
  return `${(num / (1024 * 1024)).toFixed(1)} MB`;
}

function formatInlineMarkdown(text) {
  return escapeHtml(text)
    .replace(
      /\[([^\]]+)\]\(([^)]+)\)/g,
      (_, label, rawHref) => {
        const href = resolveApiUrl(rawHref.trim());
        const isDownload = href.includes("download=1") || /\.(docx|xlsx|svg|md)$/i.test(href);
        const extraAttrs = isDownload
          ? ' download target="_blank" rel="noopener"'
          : ' target="_blank" rel="noopener"';
        return `<a class="msg-link" href="${href}"${extraAttrs}>${label}</a>`;
      }
    )
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>");
}

function renderMarkdown(rawText) {
  if (!rawText) return "";
  const lines = String(rawText).replace(/\r\n/g, "\n").split("\n");
  const htmlBlocks = [];
  let inCodeBlock = false;
  let codeLines = [];
  let listType = null; // "ul" | "ol"
  let listItems = [];
  let tableRows = [];

  const flushList = () => {
    if (listType && listItems.length > 0) {
      htmlBlocks.push(`<${listType}>${listItems.join("")}</${listType}>`);
    }
    listType = null;
    listItems = [];
  };

  const flushTable = () => {
    if (tableRows.length === 0) return;
    const parsedRows = tableRows
      .filter((row) => !/^\s*\|?[\s:-]+\|[\s|:-]*$/.test(row))
      .map((row) =>
        row
          .trim()
          .replace(/^\|/, "")
          .replace(/\|$/, "")
          .split("|")
          .map((cell) => formatInlineMarkdown(cell.trim()))
      );
    if (parsedRows.length > 0) {
      const headerCells = parsedRows[0].map((c) => `<th>${c}</th>`).join("");
      const bodyRows = parsedRows
        .slice(1)
        .map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`)
        .join("");
      htmlBlocks.push(
        `<div class="msg-table-wrap"><table class="msg-table"><thead><tr>${headerCells}</tr></thead><tbody>${bodyRows}</tbody></table></div>`
      );
    }
    tableRows = [];
  };

  for (const line of lines) {
    const trimmed = line.trim();

    if (trimmed.startsWith("```")) {
      flushList();
      flushTable();
      if (!inCodeBlock) {
        inCodeBlock = true;
        codeLines = [];
      } else {
        htmlBlocks.push(
          `<pre tabindex="0"><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`
        );
        inCodeBlock = false;
        codeLines = [];
      }
      continue;
    }

    if (inCodeBlock) {
      codeLines.push(line);
      continue;
    }

    // Markdown table detection
    if (trimmed.startsWith("|") && trimmed.endsWith("|") && trimmed.includes("|")) {
      flushList();
      tableRows.push(trimmed);
      continue;
    } else if (tableRows.length > 0) {
      flushTable();
    }

    if (!trimmed) {
      flushList();
      continue;
    }

    // Horizontal rule
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
      flushList();
      htmlBlocks.push('<hr class="msg-divider" />');
      continue;
    }

    // Headings
    const headingMatch = trimmed.match(/^(#{1,4})\s+(.*)$/);
    if (headingMatch) {
      flushList();
      const level = Math.min(4, Math.max(2, headingMatch[1].length));
      htmlBlocks.push(`<h${level}>${formatInlineMarkdown(headingMatch[2])}</h${level}>`);
      continue;
    }

    // Unordered list item
    const ulMatch = trimmed.match(/^[-*•]\s+(.*)$/);
    if (ulMatch) {
      if (listType !== "ul") {
        flushList();
        listType = "ul";
      }
      listItems.push(`<li>${formatInlineMarkdown(ulMatch[1])}</li>`);
      continue;
    }

    // Ordered list item
    const olMatch = trimmed.match(/^\d+\.\s+(.*)$/);
    if (olMatch) {
      if (listType !== "ol") {
        flushList();
        listType = "ol";
      }
      listItems.push(`<li>${formatInlineMarkdown(olMatch[1])}</li>`);
      continue;
    }

    flushList();
    htmlBlocks.push(`<p>${formatInlineMarkdown(trimmed)}</p>`);
  }

  if (inCodeBlock && codeLines.length > 0) {
    htmlBlocks.push(
      `<pre tabindex="0"><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`
    );
  }
  flushList();
  flushTable();

  return htmlBlocks.join("");
}

// ---------------------------------------------------------------------------
// Connection Health & Runtime Status
// ---------------------------------------------------------------------------
async function loadHealthStatus() {
  const dotEl = document.getElementById("connectionDot");
  const textEl = document.getElementById("connectionText");
  const modeEl = document.getElementById("runtimeModeText");

  try {
    const health = await apiGet("/api/health");
    const awsLabel = health.live_aws ? "Live AWS" : "AWS Connected";
    const llmMode =
      health.llm_auth_mode === "vertex-ai-adc"
        ? "Vertex AI Gemini"
        : health.llm_auth_mode === "gemini-api-key"
        ? "Gemini API"
        : "Gemini Engine";

    if (dotEl) {
      dotEl.className = "status-dot status-dot-online";
    }
    if (textEl) {
      textEl.textContent = `${awsLabel} • ${llmMode}`;
    }
    if (modeEl) {
      modeEl.textContent = `${health.model || "gemini-3.8-flash"} • ${
        health.cluster || "cloud-ops-demo-cluster"
      }`;
    }
  } catch {
    try {
      const st = await apiGet("/api/state");
      if (textEl) textEl.textContent = "Connected to AWS • Gemini";
      if (modeEl) {
        modeEl.textContent = `${st.gemini_model || "gemini-3.8-flash"} • ${
          st.region || "us-east-1"
        }`;
      }
    } catch {
      if (dotEl) dotEl.className = "status-dot status-dot-warn";
      if (textEl) textEl.textContent = "Offline / Reconnecting...";
    }
  }
}

// ---------------------------------------------------------------------------
// Conversation History CRUD & Sidebar Rendering
// ---------------------------------------------------------------------------
function renderSidebarList() {
  const listEl = document.getElementById("conversationList");
  if (!listEl) return;

  if (!state.conversations || state.conversations.length === 0) {
    listEl.innerHTML = `<div class="conv-empty-hint">No conversations yet. Ask a question to begin.</div>`;
    return;
  }

  listEl.innerHTML = state.conversations
    .map((conv) => {
      const isActive = conv.conversation_id === state.activeConversationId;
      const title = conv.title || "New Conversation";
      const timeLabel = formatRelativeTime(conv.updated_at || conv.created_at);
      const svcBadge = conv.active_service ? ` • ${conv.active_service}` : "";
      return `
        <div
          class="conv-item ${isActive ? "active" : ""}"
          data-conv-id="${escapeHtml(conv.conversation_id)}"
          role="button"
          tabindex="0"
          aria-current="${isActive ? "page" : "false"}"
        >
          <div class="conv-item-main">
            <span class="conv-item-title">${escapeHtml(title)}</span>
            <span class="conv-item-meta">${escapeHtml(timeLabel + svcBadge)}</span>
          </div>
          <button
            type="button"
            class="conv-delete-btn"
            data-delete-conv="${escapeHtml(conv.conversation_id)}"
            aria-label="Delete conversation ${escapeHtml(title)}"
          >
            ✕
          </button>
        </div>
      `;
    })
    .join("");

  listEl.querySelectorAll("[data-conv-id]").forEach((row) => {
    row.addEventListener("click", (e) => {
      if (e.target.closest("[data-delete-conv]")) return;
      const cid = row.getAttribute("data-conv-id");
      if (cid) {
        selectConversation(cid);
        closeMobileSidebar();
      }
    });
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        if (e.target.closest("[data-delete-conv]")) return;
        e.preventDefault();
        const cid = row.getAttribute("data-conv-id");
        if (cid) {
          selectConversation(cid);
          closeMobileSidebar();
        }
      }
    });
  });

  listEl.querySelectorAll("[data-delete-conv]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      const cid = btn.getAttribute("data-delete-conv");
      if (cid) {
        deleteConversation(cid, e);
      }
    });
  });
}

async function loadConversations() {
  try {
    const res = await apiGet("/api/conversations");
    state.conversations = res.conversations || [];
    const targetId =
      res.active_conversation_id ||
      (state.conversations[0] && state.conversations[0].conversation_id) ||
      null;

    renderSidebarList();

    if (targetId) {
      await selectConversation(targetId);
    } else {
      renderConversation(null);
    }
  } catch {
    renderSidebarList();
    renderConversation(null);
  }
}

async function createNewConversation() {
  try {
    const res = await apiPost("/api/conversations", {});
    const conv = res.conversation || res;
    if (conv && conv.conversation_id) {
      state.activeConversationId = conv.conversation_id;
      state.activeConversation = conv;
      state.conversations = [
        conv,
        ...state.conversations.filter((c) => c.conversation_id !== conv.conversation_id),
      ];
    }
  } catch {
    state.activeConversationId = null;
    state.activeConversation = null;
  }

  renderSidebarList();
  renderConversation(state.activeConversation);
  closeMobileSidebar();
  const input = document.getElementById("chatInput");
  if (input) input.focus();
}

async function selectConversation(conversationId) {
  if (!conversationId) {
    renderConversation(null);
    return;
  }
  state.activeConversationId = conversationId;
  renderSidebarList();

  try {
    const res = await apiGet(`/api/conversations/${encodeURIComponent(conversationId)}`);
    const conv = res.conversation || res;
    state.activeConversation = conv;
    state.activeConversationId = conv.conversation_id || conversationId;
    renderSidebarList();
    renderConversation(conv);
  } catch {
    renderConversation(null);
  }
}

async function deleteConversation(conversationId, event) {
  if (event) {
    event.stopPropagation();
    event.preventDefault();
  }
  try {
    await apiDelete(`/api/conversations/${encodeURIComponent(conversationId)}`);
  } catch {
    // Ignore if already removed
  }

  state.conversations = state.conversations.filter(
    (c) => c.conversation_id !== conversationId
  );

  if (state.activeConversationId === conversationId) {
    const nextConv = state.conversations[0];
    if (nextConv && nextConv.conversation_id) {
      await selectConversation(nextConv.conversation_id);
    } else {
      state.activeConversationId = null;
      state.activeConversation = null;
      renderSidebarList();
      renderConversation(null);
    }
  } else {
    renderSidebarList();
  }
}

// ---------------------------------------------------------------------------
// Conversation & Message Stream Rendering
// ---------------------------------------------------------------------------
function renderConversation(conv) {
  const titleEl = document.getElementById("activeConvTitle");
  const emptyStateEl = document.getElementById("emptyChatState");
  const messagesEl = document.getElementById("chatMessages");

  if (!messagesEl || !emptyStateEl) return;

  const messages = (conv && conv.messages) || [];
  if (titleEl) {
    titleEl.textContent = (conv && conv.title) || "New Conversation";
  }

  if (messages.length === 0) {
    emptyStateEl.classList.remove("hidden");
    messagesEl.innerHTML = "";
    return;
  }

  emptyStateEl.classList.add("hidden");
  messagesEl.innerHTML = "";

  messages.forEach((msg) => {
    const node =
      msg.role === "user" ? renderUserMessage(msg) : renderAssistantMessage(msg);
    messagesEl.appendChild(node);
  });

  bindMessageInteractions(messagesEl);
  scrollToBottom();
}

function renderUserMessage(msg) {
  const wrapper = document.createElement("article");
  wrapper.className = "msg-turn msg-turn-user";
  if (msg.message_id) wrapper.dataset.messageId = msg.message_id;

  const timeLabel = formatRelativeTime(msg.created_at);
  const attachments = msg.attachments || [];

  const attachmentsHtml =
    attachments.length > 0
      ? `<div class="msg-attachments-row">
          ${attachments
            .map((att) => {
              const isImg =
                (att.mime_type && att.mime_type.startsWith("image/")) ||
                (att.data_url && att.data_url.startsWith("data:image/"));
              const thumb =
                isImg && att.data_url
                  ? `<img src="${escapeHtml(att.data_url)}" alt="${escapeHtml(
                      att.filename
                    )}" class="msg-attachment-thumb" data-lightbox-src="${escapeHtml(
                      att.data_url
                    )}" data-lightbox-title="${escapeHtml(att.filename)}" />`
                  : `<span aria-hidden="true">📎</span>`;
              return `<span class="msg-attachment-chip">${thumb}<span>${escapeHtml(
                att.filename || "attachment"
              )}</span></span>`;
            })
            .join("")}
        </div>`
      : "";

  wrapper.innerHTML = `
    <div class="msg-header">
      <span class="msg-role-badge">You</span>
      ${timeLabel ? `<span>• ${escapeHtml(timeLabel)}</span>` : ""}
    </div>
    <div class="msg-bubble">
      ${attachmentsHtml}
      <div class="msg-narrative msg-body">
        <p>${formatInlineMarkdown(msg.content || "")}</p>
      </div>
    </div>
  `;
  return wrapper;
}

function renderCommandDrawer(commands) {
  if (!commands || commands.length === 0) return "";
  const count = commands.length;
  const plural = count === 1 ? "" : "s";

  const itemsHtml = commands
    .map((cmd, idx) => {
      const indexNum = cmd.index || idx + 1;
      const status = String(cmd.status || "ALLOWED").toUpperCase();
      const statusClass =
        status === "BLOCKED"
          ? "cmd-status-BLOCKED"
          : status === "REQUIRES_APPROVAL"
          ? "cmd-status-REQUIRES_APPROVAL"
          : "cmd-status-ALLOWED";
      const mode = cmd.execution_mode || "SIMULATION";

      return `
        <div class="cmd-item">
          <div class="cmd-item-header">
            <div class="cmd-item-left">
              <span class="cmd-index">#${escapeHtml(indexNum)}</span>
              <span class="cmd-tool-badge">${escapeHtml(cmd.tool_name || "aws_cli")}</span>
            </div>
            <div class="cmd-badges-right">
              <span class="cmd-status-badge ${statusClass}">${escapeHtml(status)}</span>
              <span class="cmd-mode-badge">${escapeHtml(mode)}</span>
            </div>
          </div>
          <code class="cmd-cli-code">${escapeHtml(cmd.command || "")}</code>
          ${
            cmd.output_summary
              ? `<div class="cmd-output-summary">${escapeHtml(cmd.output_summary)}</div>`
              : ""
          }
        </div>
      `;
    })
    .join("");

  return `
    <details class="cmd-drawer">
      <summary class="cmd-drawer-summary">
        <span class="cmd-summary-left">
          <span class="cmd-caret" aria-hidden="true"></span>
          <span class="cmd-label-closed">Executed ${count} AWS &amp; MCP command${plural} — click to view</span>
          <span class="cmd-label-open">Executed ${count} AWS &amp; MCP command${plural} — click to hide</span>
        </span>
        <span class="cmd-summary-hint">Guardrailed CLI Audit</span>
      </summary>
      <div class="cmd-drawer-body">
        ${itemsHtml}
      </div>
    </details>
  `;
}

function renderInlineArtifacts(artifacts) {
  if (!artifacts || artifacts.length === 0) return "";

  const chartArtifacts = artifacts.filter(
    (a) => a.kind === "chart" || (a.name && a.name.toLowerCase().endsWith(".svg"))
  );
  const fileArtifacts = artifacts.filter(
    (a) => !(a.kind === "chart" || (a.name && a.name.toLowerCase().endsWith(".svg")))
  );

  const chartsHtml = chartArtifacts
    .map((art) => {
      const previewUrl = resolveApiUrl(
        art.preview_url || art.url || `/artifacts/${encodeURIComponent(art.name)}`
      );
      const title = art.description || art.name || "Service Telemetry Chart (.svg)";
      return `
        <figure class="inline-chart-card">
          <div class="inline-chart-header">
            <figcaption class="inline-chart-title">📈 ${escapeHtml(title)}</figcaption>
            <button
              type="button"
              class="btn-expand-chart"
              data-lightbox-src="${escapeHtml(previewUrl)}"
              data-lightbox-title="${escapeHtml(title)}"
            >
              ⤢ Expand Chart
            </button>
          </div>
          <img
            src="${escapeHtml(previewUrl)}"
            alt="${escapeHtml(title)}"
            class="inline-svg-chart"
            data-lightbox-src="${escapeHtml(previewUrl)}"
            data-lightbox-title="${escapeHtml(title)}"
          />
        </figure>
      `;
    })
    .join("");

  const labelMap = {
    excel: { icon: "📊", label: "Excel Event Log (.xlsx)" },
    docx: { icon: "📄", label: "Word Incident Report (.docx)" },
    markdown: { icon: "📝", label: "Markdown Summary (.md)" },
  };

  const downloadsHtml =
    fileArtifacts.length > 0
      ? `<div class="artifact-download-grid artifact-pills">
          ${fileArtifacts
            .map((art) => {
              const info = labelMap[art.kind] || {
                icon: "📁",
                label: art.name || "Operational Artifact",
              };
              const baseUrl = art.url || `/artifacts/${encodeURIComponent(art.name)}`;
              const downloadUrl = resolveApiUrl(
                art.download_url ||
                  (baseUrl.includes("?") ? baseUrl : `${baseUrl}?download=1`)
              );
              return `
                <a
                  href="${escapeHtml(downloadUrl)}"
                  download="${escapeHtml(art.name || "")}"
                  class="artifact-download-card"
                >
                  <div class="artifact-card-left">
                    <span class="artifact-icon" aria-hidden="true">${info.icon}</span>
                    <div class="artifact-meta">
                      <span class="artifact-label">${escapeHtml(info.label)}</span>
                      <span class="artifact-filename">${escapeHtml(art.name || "")}</span>
                    </div>
                  </div>
                  <span class="artifact-size-badge">⬇ ${escapeHtml(
                    formatFileSize(art.size_bytes)
                  )}</span>
                </a>
              `;
            })
            .join("")}
        </div>`
      : "";

  return `<div class="msg-artifacts-section">${chartsHtml}${downloadsHtml}</div>`;
}

function renderInlineProposal(proposal, incidentId) {
  if (!proposal) return "";
  const status = String(proposal.status || "PENDING_APPROVAL").toUpperCase();
  const isPending = status === "PENDING_APPROVAL";
  const isApproved = status === "APPROVED" || status === "APPROVED_AND_EXECUTED";
  const cardStateClass = isPending
    ? "status-pending"
    : isApproved
    ? "status-approved"
    : "status-rejected";
  const pillStateClass = isPending ? "pending" : isApproved ? "approved" : "rejected";
  const resolvedIncidentId = incidentId || proposal.incident_id || "";

  return `
    <section class="inline-approval-card ${cardStateClass}" aria-label="Remediation Approval Request">
      <div class="approval-card-header">
        <h3 class="proposal-title">
          <span aria-hidden="true">${isApproved ? "✅" : isPending ? "🛡️" : "✕"}</span>
          <span>${escapeHtml(proposal.title || "Proposed AWS Service Change")}</span>
        </h3>
        <span class="proposal-status-pill ${pillStateClass}">${escapeHtml(status)}</span>
      </div>

      <p class="proposal-explanation">
        ${escapeHtml(
          proposal.blast_radius ||
            "This action modifies an Amazon ECS service and requires your explicit approval before executing."
        )}
      </p>

      <details class="tech-cmd-details">
        <summary>Inspect AWS CLI write command &amp; rollback plan</summary>
        <div class="tech-cmd-body">
          <div>
            <strong>MCP Write Tool:</strong> <code>${escapeHtml(
              proposal.mcp_tool || "update_ecs_service"
            )}</code>
          </div>
          <div>
            <strong>AWS CLI Command:</strong>
            <code class="cmd-cli-code">${escapeHtml(proposal.aws_cli_equivalent || "")}</code>
          </div>
          ${
            proposal.rollback_plan
              ? `<div><strong>Automatic Rollback Plan:</strong> ${escapeHtml(
                  proposal.rollback_plan
                )}</div>`
              : ""
          }
        </div>
      </details>

      ${
        isPending && resolvedIncidentId
          ? `<div class="approval-actions-row">
              <button
                type="button"
                class="btn-approve-inline"
                data-approve-incident="${escapeHtml(resolvedIncidentId)}"
              >
                ✓ Approve &amp; Execute
              </button>
              <button
                type="button"
                class="btn-reject-inline"
                data-reject-incident="${escapeHtml(resolvedIncidentId)}"
              >
                ✕ Cancel
              </button>
            </div>`
          : ""
      }
    </section>
  `;
}

function renderInlineVerification(verification) {
  if (!verification) return "";
  const verified = Boolean(verification.verified);
  const pre = verification.pre_metrics || {};
  const post = verification.post_metrics || {};
  const hasMetrics =
    pre.error_rate_pct !== undefined || post.error_rate_pct !== undefined;

  return `
    <div class="inline-verification-card ${verified ? "" : "failed"}">
      <div class="verification-header">
        <span>${
          verified
            ? "✅ Independent Verifier: Service Recovery Confirmed"
            : "⚠️ Independent Verifier: Post-Change Check Failed"
        }</span>
      </div>
      <div>${escapeHtml(verification.summary || "")}</div>
      ${
        hasMetrics
          ? `<div>
              <strong>Telemetry Shift:</strong>
              Error Rate <code>${escapeHtml(pre.error_rate_pct ?? 0)}% → ${escapeHtml(
              post.error_rate_pct ?? 0
            )}%</code> •
              P95 Latency <code>${escapeHtml(pre.p95_latency_ms ?? 0)}ms → ${escapeHtml(
              post.p95_latency_ms ?? 0
            )}ms</code>
            </div>`
          : ""
      }
    </div>
  `;
}

function renderAssistantMessage(msg) {
  const wrapper = document.createElement("article");
  wrapper.className = "msg-turn msg-turn-assistant";
  if (msg.message_id) wrapper.dataset.messageId = msg.message_id;

  const timeLabel = formatRelativeTime(msg.created_at);
  const cmdDrawerHtml = renderCommandDrawer(msg.executed_commands || []);
  const narrativeHtml = renderMarkdown(msg.content || "");
  const artifactsHtml = renderInlineArtifacts(msg.artifacts || []);
  const proposalHtml = renderInlineProposal(msg.proposal, msg.incident_id);
  const verificationHtml = renderInlineVerification(msg.verification);

  wrapper.innerHTML = `
    <div class="msg-header">
      <span class="msg-role-badge">⚡ Cloud Ops Agent</span>
      ${timeLabel ? `<span>• ${escapeHtml(timeLabel)}</span>` : ""}
    </div>
    <div class="msg-bubble">
      ${cmdDrawerHtml}
      <div class="msg-narrative msg-body">${narrativeHtml}</div>
      ${artifactsHtml}
      ${proposalHtml}
      ${verificationHtml}
    </div>
  `;
  return wrapper;
}

function bindMessageInteractions(container) {
  if (!container) return;

  container.querySelectorAll("[data-lightbox-src]").forEach((el) => {
    if (el.dataset.boundLightbox) return;
    el.dataset.boundLightbox = "1";
    el.addEventListener("click", () => {
      const src = el.getAttribute("data-lightbox-src");
      const title = el.getAttribute("data-lightbox-title");
      if (src) openImageLightbox(src, title);
    });
  });

  container.querySelectorAll("[data-approve-incident]").forEach((btn) => {
    if (btn.dataset.boundApprove) return;
    btn.dataset.boundApprove = "1";
    btn.addEventListener("click", () => {
      const incId = btn.getAttribute("data-approve-incident");
      if (incId) {
        handleInlineApprove(incId, state.activeConversationId, btn);
      }
    });
  });

  container.querySelectorAll("[data-reject-incident]").forEach((btn) => {
    if (btn.dataset.boundReject) return;
    btn.dataset.boundReject = "1";
    btn.addEventListener("click", () => {
      const incId = btn.getAttribute("data-reject-incident");
      if (incId) {
        handleInlineReject(incId, state.activeConversationId, btn);
      }
    });
  });
}

function scrollToBottom() {
  const wrap = document.getElementById("chatScrollWrap");
  if (!wrap) return;
  wrap.scrollTop = wrap.scrollHeight;
}

// ---------------------------------------------------------------------------
// Sending Messages & Inline Approval Actions
// ---------------------------------------------------------------------------
async function sendMessage(prefillText = null) {
  if (state.sending) return;
  const input = document.getElementById("chatInput");
  const rawText =
    typeof prefillText === "string" ? prefillText : input ? input.value : "";
  const message = rawText.trim();
  if (!message && state.attachments.length === 0) return;

  state.sending = true;
  const sendBtn = document.getElementById("btnSend");
  if (sendBtn) sendBtn.disabled = true;

  const currentAttachments = state.attachments.slice();
  state.attachments = [];
  renderAttachmentStrip();

  if (input && typeof prefillText !== "string") {
    input.value = "";
    autoResizeTextarea(input);
  }

  // Hide welcome empty state and optimistically append user bubble + thinking indicator
  const emptyStateEl = document.getElementById("emptyChatState");
  const messagesEl = document.getElementById("chatMessages");
  if (emptyStateEl) emptyStateEl.classList.add("hidden");

  const optimisticUserNode = renderUserMessage({
    role: "user",
    content: message || "(Attached operational files)",
    created_at: new Date().toISOString(),
    attachments: currentAttachments,
  });

  const thinkingNode = document.createElement("article");
  thinkingNode.className = "msg-turn msg-turn-assistant";
  thinkingNode.id = "thinkingTurnIndicator";
  thinkingNode.innerHTML = `
    <div class="msg-header">
      <span class="msg-role-badge">⚡ Cloud Ops Agent</span>
    </div>
    <div class="msg-bubble">
      <div class="thinking-indicator">
        <div class="thinking-dots" aria-hidden="true"><span></span><span></span><span></span></div>
        <span>Thinking &amp; querying AWS...</span>
      </div>
    </div>
  `;

  if (messagesEl) {
    messagesEl.appendChild(optimisticUserNode);
    messagesEl.appendChild(thinkingNode);
    bindMessageInteractions(messagesEl);
    scrollToBottom();
  }

  try {
    const payloadAttachments = currentAttachments.map((att) => ({
      filename: att.filename,
      mime_type: att.mime_type,
      content: att.content || "",
      preview_text: (att.content || "").slice(0, 300),
      data_url: att.data_url || "",
    }));

    const res = await apiPost("/api/chat", {
      message: message || "Analyze attached operational files",
      attachments: payloadAttachments,
      conversation_id: state.activeConversationId,
    });

    if (res.conversation_id) {
      state.activeConversationId = res.conversation_id;
    }

    if (res.conversation) {
      state.activeConversation = res.conversation;
      renderConversation(res.conversation);
    } else if (res.message && messagesEl) {
      thinkingNode.remove();
      const assistantNode = renderAssistantMessage(res.message);
      messagesEl.appendChild(assistantNode);
      bindMessageInteractions(messagesEl);
      scrollToBottom();
    } else {
      thinkingNode.remove();
      const fallbackNode = renderAssistantMessage({
        role: "assistant",
        content: res.reply || "Completed request.",
        created_at: new Date().toISOString(),
        executed_commands: res.executed_commands || [],
        incident_id: res.incident_id || null,
        proposal: (res.incident && res.incident.proposal) || res.proposal || null,
        verification:
          (res.incident && res.incident.verification) || res.verification || null,
        artifacts: (res.incident && res.incident.artifacts) || res.artifacts || [],
      });
      messagesEl.appendChild(fallbackNode);
      bindMessageInteractions(messagesEl);
      scrollToBottom();
    }

    // Refresh sidebar conversation summaries
    try {
      const listRes = await apiGet("/api/conversations");
      state.conversations = listRes.conversations || state.conversations;
      renderSidebarList();
    } catch {
      // Keep current sidebar list if endpoint unavailable
    }
  } catch (err) {
    thinkingNode.remove();
    if (messagesEl) {
      const errNode = renderAssistantMessage({
        role: "assistant",
        content: `**Request Error:** ${err.message}`,
        created_at: new Date().toISOString(),
      });
      messagesEl.appendChild(errNode);
      scrollToBottom();
    }
  } finally {
    state.sending = false;
    if (sendBtn) sendBtn.disabled = false;
  }
}

async function handleInlineApprove(incidentId, conversationId, buttonEl) {
  if (!incidentId) return;
  const cardEl = buttonEl ? buttonEl.closest(".inline-approval-card") : null;
  let progressEl = null;

  if (buttonEl) {
    buttonEl.disabled = true;
    buttonEl.textContent = "⏳ Executing Approved Change...";
  }
  if (cardEl) {
    const rejectBtn = cardEl.querySelector("[data-reject-incident]");
    if (rejectBtn) rejectBtn.disabled = true;
    progressEl = document.createElement("div");
    progressEl.className = "approval-progress-box";
    progressEl.innerHTML = `
      <div class="approval-progress-bar"><div class="approval-progress-fill" id="approvalProgressFill"></div></div>
      <div class="approval-progress-steps">
        <div class="approval-step active" data-step="1">1. Issuing single-use HMAC token &amp; executing AWS CLI change...</div>
        <div class="approval-step" data-step="2">2. Updating ECS service &amp; draining unhealthy task revision...</div>
        <div class="approval-step" data-step="3">3. Running Independent Verifier CloudWatch telemetry checks...</div>
      </div>
    `;
    cardEl.appendChild(progressEl);
  }

  const advanceStep = (stepNum, pct) => {
    if (!progressEl) return;
    const fill = progressEl.querySelector(".approval-progress-fill");
    if (fill) fill.style.width = `${pct}%`;
    progressEl.querySelectorAll(".approval-step").forEach((el) => {
      const s = Number(el.getAttribute("data-step"));
      el.classList.toggle("done", s < stepNum);
      el.classList.toggle("active", s === stepNum);
    });
  };

  try {
    advanceStep(1, 33);
    const apiPromise = apiPost(
      `/api/incidents/${encodeURIComponent(incidentId)}/approve`,
      {
        approver: "ops-engineer@cloudops.io",
        conversation_id: conversationId || state.activeConversationId,
      }
    );

    await new Promise((r) => setTimeout(r, 420));
    advanceStep(2, 68);
    await new Promise((r) => setTimeout(r, 420));
    advanceStep(3, 95);
    const [res] = await Promise.all([
      apiPromise,
      new Promise((r) => setTimeout(r, 380)),
    ]);
    advanceStep(4, 100);

    if (res.conversation) {
      state.activeConversation = res.conversation;
      renderConversation(res.conversation);
    } else if (state.activeConversationId) {
      await selectConversation(state.activeConversationId);
    }
  } catch (err) {
    if (progressEl) progressEl.remove();
    if (buttonEl) {
      buttonEl.disabled = false;
      buttonEl.textContent = "✓ Approve & Execute";
    }
    const messagesEl = document.getElementById("chatMessages");
    if (messagesEl) {
      messagesEl.appendChild(
        renderAssistantMessage({
          role: "assistant",
          content: `**Approval Failed:** ${err.message}`,
          created_at: new Date().toISOString(),
        })
      );
      scrollToBottom();
    }
  }
}

async function handleInlineReject(incidentId, conversationId, buttonEl) {
  if (!incidentId) return;
  if (buttonEl) {
    buttonEl.disabled = true;
    buttonEl.textContent = "Cancelling...";
  }
  try {
    const res = await apiPost(
      `/api/incidents/${encodeURIComponent(incidentId)}/reject`,
      {
        reason: "Cancelled by operator in chat",
        conversation_id: conversationId || state.activeConversationId,
      }
    );

    if (res.conversation) {
      state.activeConversation = res.conversation;
      renderConversation(res.conversation);
    } else if (state.activeConversationId) {
      await selectConversation(state.activeConversationId);
    }
  } catch (err) {
    if (buttonEl) {
      buttonEl.disabled = false;
      buttonEl.textContent = "✕ Cancel";
    }
  }
}

// ---------------------------------------------------------------------------
// Multimodal File & Drag-and-Drop Attachments
// ---------------------------------------------------------------------------
function renderAttachmentStrip() {
  const strip = document.getElementById("attachmentStrip");
  if (!strip) return;

  if (state.attachments.length === 0) {
    strip.classList.add("hidden");
    strip.innerHTML = "";
    return;
  }

  strip.classList.remove("hidden");
  strip.innerHTML = state.attachments
    .map((att, idx) => {
      const isImg =
        (att.mime_type && att.mime_type.startsWith("image/")) ||
        (att.data_url && att.data_url.startsWith("data:image/"));
      const thumb =
        isImg && att.data_url
          ? `<img src="${escapeHtml(att.data_url)}" alt="${escapeHtml(
              att.filename
            )}" class="attachment-preview-thumb" />`
          : `<span aria-hidden="true">📎</span>`;
      return `
        <div class="attachment-preview-item">
          ${thumb}
          <span>${escapeHtml(att.filename)}</span>
          <button
            type="button"
            class="attachment-remove-btn"
            data-remove-attachment="${idx}"
            aria-label="Remove ${escapeHtml(att.filename)}"
          >
            ✕
          </button>
        </div>
      `;
    })
    .join("");

  strip.querySelectorAll("[data-remove-attachment]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = Number(btn.getAttribute("data-remove-attachment"));
      if (!Number.isNaN(idx)) {
        state.attachments.splice(idx, 1);
        renderAttachmentStrip();
      }
    });
  });
}

function readSingleFile(file) {
  return new Promise((resolve) => {
    const isImage = file.type && file.type.startsWith("image/");
    const reader = new FileReader();

    if (isImage) {
      reader.onload = () => {
        resolve({
          filename: file.name,
          mime_type: file.type || "image/png",
          data_url: typeof reader.result === "string" ? reader.result : "",
          content: `[Attached Image: ${file.name} (${file.type || "image"}, ${
            file.size
          } bytes)]`,
        });
      };
      reader.onerror = () => {
        resolve({
          filename: file.name,
          mime_type: file.type || "image/png",
          data_url: "",
          content: `[Attached Image: ${file.name}]`,
        });
      };
      reader.readAsDataURL(file);
    } else {
      reader.onload = () => {
        const text = typeof reader.result === "string" ? reader.result : "";
        resolve({
          filename: file.name,
          mime_type: file.type || "text/plain",
          data_url: "",
          content: text.slice(0, 8000),
        });
      };
      reader.onerror = () => {
        resolve({
          filename: file.name,
          mime_type: file.type || "text/plain",
          data_url: "",
          content: `[Attached File: ${file.name}]`,
        });
      };
      reader.readAsText(file);
    }
  });
}

async function handleFilesSelected(fileList) {
  if (!fileList || fileList.length === 0) return;
  const files = Array.from(fileList);
  for (const file of files) {
    const item = await readSingleFile(file);
    state.attachments.push(item);
  }
  renderAttachmentStrip();
}

// ---------------------------------------------------------------------------
// Sidebar Collapse / Expand (Desktop Grid & Mobile Drawer) + Textarea Resize
// ---------------------------------------------------------------------------
function isMobileViewport() {
  return window.matchMedia("(max-width: 860px)").matches;
}

function syncSidebarAria() {
  const layout =
    document.getElementById("appLayout") || document.querySelector(".app-layout");
  const sidebar = document.getElementById("chatSidebar");
  const backdrop = document.getElementById("sidebarBackdrop");
  const btnToggle = document.getElementById("btnToggleSidebar");
  const btnClose = document.getElementById("btnCloseSidebar");

  const mobile = isMobileViewport();
  let expanded;
  if (mobile) {
    expanded = Boolean(sidebar && sidebar.classList.contains("open"));
    if (backdrop) {
      backdrop.hidden = !expanded;
    }
  } else {
    if (sidebar) sidebar.classList.remove("open");
    if (backdrop) backdrop.hidden = true;
    const collapsed = Boolean(
      state.sidebarCollapsed ||
        (layout && layout.classList.contains("sidebar-collapsed"))
    );
    expanded = !collapsed;
  }

  if (btnToggle) {
    btnToggle.setAttribute("aria-expanded", String(expanded));
    const actionLabel = expanded
      ? "Collapse conversation sidebar"
      : "Expand conversation sidebar";
    btnToggle.setAttribute("aria-label", actionLabel);
    btnToggle.setAttribute("title", actionLabel);
  }

  if (btnClose) {
    btnClose.setAttribute("aria-expanded", String(expanded));
    const closeLabel = mobile ? "Close sidebar" : "Collapse sidebar";
    btnClose.setAttribute("aria-label", closeLabel);
    btnClose.setAttribute("title", closeLabel);
  }
}

function collapseSidebar() {
  const layout =
    document.getElementById("appLayout") || document.querySelector(".app-layout");
  const sidebar = document.getElementById("chatSidebar");
  const backdrop = document.getElementById("sidebarBackdrop");

  if (isMobileViewport()) {
    if (sidebar) sidebar.classList.remove("open");
    if (backdrop) backdrop.hidden = true;
  } else {
    state.sidebarCollapsed = true;
    if (layout) layout.classList.add("sidebar-collapsed");
    if (sidebar) {
      sidebar.classList.remove("open");
      sidebar.classList.add("collapsed");
    }
    if (backdrop) backdrop.hidden = true;
  }
  syncSidebarAria();
}

function expandSidebar() {
  const layout =
    document.getElementById("appLayout") || document.querySelector(".app-layout");
  const sidebar = document.getElementById("chatSidebar");
  const backdrop = document.getElementById("sidebarBackdrop");

  if (isMobileViewport()) {
    if (sidebar) sidebar.classList.add("open");
    if (backdrop) backdrop.hidden = false;
  } else {
    state.sidebarCollapsed = false;
    if (layout) layout.classList.remove("sidebar-collapsed");
    if (sidebar) {
      sidebar.classList.remove("collapsed");
      sidebar.classList.remove("open");
    }
    if (backdrop) backdrop.hidden = true;
  }
  syncSidebarAria();
}

function toggleSidebar() {
  const layout =
    document.getElementById("appLayout") || document.querySelector(".app-layout");
  const sidebar = document.getElementById("chatSidebar");

  if (isMobileViewport()) {
    const isOpen = Boolean(sidebar && sidebar.classList.contains("open"));
    if (isOpen) {
      collapseSidebar();
    } else {
      expandSidebar();
    }
  } else {
    const isCollapsed = Boolean(
      state.sidebarCollapsed ||
        (layout && layout.classList.contains("sidebar-collapsed"))
    );
    if (isCollapsed) {
      expandSidebar();
    } else {
      collapseSidebar();
    }
  }
}

function openMobileSidebar() {
  expandSidebar();
}

function closeMobileSidebar() {
  const sidebar = document.getElementById("chatSidebar");
  const backdrop = document.getElementById("sidebarBackdrop");
  if (sidebar) sidebar.classList.remove("open");
  if (backdrop) backdrop.hidden = true;
  syncSidebarAria();
}

function autoResizeTextarea(textarea) {
  if (!textarea) return;
  textarea.style.height = "auto";
  textarea.style.height = `${Math.min(150, textarea.scrollHeight)}px`;
}

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", async () => {
  initModernDialogs();
  syncSidebarAria();
  window.addEventListener("resize", () => syncSidebarAria());

  // New chat buttons
  const btnNewChat = document.getElementById("btnNewChat");
  if (btnNewChat) {
    btnNewChat.addEventListener("click", () => createNewConversation());
  }
  const btnTopNewChat = document.getElementById("btnTopNewChat");
  if (btnTopNewChat) {
    btnTopNewChat.addEventListener("click", () => createNewConversation());
  }

  // Sidebar collapse / expand toggles (desktop + mobile)
  const btnToggleSidebar = document.getElementById("btnToggleSidebar");
  if (btnToggleSidebar) {
    btnToggleSidebar.addEventListener("click", () => toggleSidebar());
  }
  const btnCloseSidebar = document.getElementById("btnCloseSidebar");
  if (btnCloseSidebar) {
    btnCloseSidebar.addEventListener("click", () => collapseSidebar());
  }
  const sidebarBackdrop = document.getElementById("sidebarBackdrop");
  if (sidebarBackdrop) {
    sidebarBackdrop.addEventListener("click", () => closeMobileSidebar());
  }

  // Starter prompt cards
  document.querySelectorAll(".starter-card").forEach((card) => {
    card.addEventListener("click", () => {
      const prompt = card.getAttribute("data-prompt");
      if (prompt) {
        sendMessage(prompt);
      }
    });
  });

  // Composer form & textarea
  const chatForm = document.getElementById("chatForm");
  const chatInput = document.getElementById("chatInput");
  if (chatForm) {
    chatForm.addEventListener("submit", (e) => {
      e.preventDefault();
      sendMessage();
    });
  }
  if (chatInput) {
    chatInput.addEventListener("input", () => autoResizeTextarea(chatInput));
    chatInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });
  }

  // File attachment button & drag-and-drop
  const btnAttach = document.getElementById("btnAttach");
  const fileInput = document.getElementById("fileInput");
  const dropZone = document.getElementById("composerDropZone");

  if (btnAttach && fileInput) {
    btnAttach.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", async () => {
      await handleFilesSelected(fileInput.files);
      fileInput.value = "";
    });
  }

  if (dropZone) {
    dropZone.addEventListener("dragover", (e) => {
      e.preventDefault();
      dropZone.classList.add("drag-over");
    });
    dropZone.addEventListener("dragleave", () => {
      dropZone.classList.remove("drag-over");
    });
    dropZone.addEventListener("drop", async (e) => {
      e.preventDefault();
      dropZone.classList.remove("drag-over");
      if (e.dataTransfer && e.dataTransfer.files) {
        await handleFilesSelected(e.dataTransfer.files);
      }
    });
  }

  // Lightbox close button
  const btnCloseDialog = document.getElementById("btnCloseDialog");
  const previewDialog = document.getElementById("imagePreviewDialog");
  if (btnCloseDialog && previewDialog) {
    btnCloseDialog.addEventListener("click", (e) => {
      e.preventDefault();
      previewDialog.close();
    });
  }

  // Load connection status and persisted conversations in parallel
  await Promise.all([loadHealthStatus(), loadConversations()]);
});
