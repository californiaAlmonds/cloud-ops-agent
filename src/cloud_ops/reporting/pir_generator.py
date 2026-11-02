"""Generate OpenXML (.docx) and Markdown (.md) Post-Incident Reports (PIR) with zero external packages."""

from __future__ import annotations

from pathlib import Path
import re
import time
from xml.sax.saxutils import escape
import zipfile

from cloud_ops.schemas import ArtifactRecord, IncidentRecord, utc_now_iso

_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")

DOCX_CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

DOCX_ROOT_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

DOCX_DOCUMENT_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

DOCX_STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:rPr><w:sz w:val="22"/><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:pPr><w:spacing w:before="240" w:after="120"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="32"/><w:color w:val="0F172A"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/>
    <w:pPr><w:spacing w:before="200" w:after="80"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="26"/><w:color w:val="1E3A8A"/></w:rPr>
  </w:style>
</w:styles>"""


def _w_para(text: str, style: str = "Normal", bold_prefix: str = "") -> str:
    safe_text = escape(text)
    if bold_prefix:
        safe_prefix = escape(bold_prefix)
        return (
            f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
            f'<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">{safe_prefix}</w:t></w:r>'
            f'<w:r><w:t xml:space="preserve">{safe_text}</w:t></w:r></w:p>'
        )
    return (
        f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
        f'<w:r><w:t xml:space="preserve">{safe_text}</w:t></w:r></w:p>'
    )


def generate_post_incident_report(
    incident: IncidentRecord,
    output_dir: Path | str,
) -> tuple[ArtifactRecord, ArtifactRecord]:
    """Generate both an OpenXML .docx Word document and a formatted .md Post-Incident Report."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_id = incident.incident_id.lower().replace(" ", "_")

    docx_filename = f"incident_{safe_id}_pir.docx"
    md_filename = f"incident_{safe_id}_pir.md"
    docx_path = out_dir / docx_filename
    md_path = out_dir / md_filename

    inv = incident.investigation
    prop = incident.proposal
    ver = incident.verification

    pre_m = (inv.metrics_snapshot if inv else {}) or {}
    post_m = (ver.post_metrics if ver else {}) or {}

    # 1. Build Markdown content
    hyp_lines: list[str] = []
    if inv and inv.hypotheses:
        for h in inv.hypotheses:
            ev_text = "; ".join(h.evidence) if h.evidence else "Inspected CloudWatch & ECS telemetry"
            hyp_lines.append(f"- **[{h.status}] {h.id} — {h.title}**: {h.description} *(Evidence: {ev_text})*")
    else:
        hyp_lines.append("- No hypotheses recorded.")

    diff_lines: list[str] = []
    if inv and inv.task_def_diff:
        td = inv.task_def_diff
        diff_lines.append(
            f"- **Active Revision**: `{td.active_revision}` vs **Healthy Baseline**: `{td.baseline_revision}`"
        )
        for k, v in td.changed_env.items():
            diff_lines.append(f"- `{k}`: `{v.get('baseline', '')}` -> `{v.get('active', '')}`")
    else:
        diff_lines.append("- No task definition diff recorded.")

    audit_lines: list[str] = []
    for a in incident.audit_trail:
        audit_lines.append(
            f"| `{a.get('timestamp', '')}` | `{a.get('actor', 'agent')}` | **{a.get('step', '')}** | {a.get('detail', '')} |"
        )

    md_content = f"""# Post-Incident Report (PIR): {incident.incident_id}

**Title:** {incident.title}  
**Service:** `{incident.service}`  
**Scenario:** `{incident.scenario}`  
**Status:** `{incident.status}`  
**Created At:** `{incident.created_at}`  
**Updated At:** `{incident.updated_at}`  

---

## 1. Executive Summary

{inv.summary if inv else incident.title}

## 2. Root Cause & Task Definition Diff

**Root Cause:** {inv.root_cause if inv else "Pending investigation"}

### Task Definition Drift
{chr(10).join(diff_lines)}

## 3. Hypotheses Evaluated (Course Adjustment Trace)

{chr(10).join(hyp_lines)}

## 4. Historical Memory Correlation

{f"Matched past incident **{inv.matched_past_incident.get('incident_id')}** ({inv.matched_past_incident.get('title')}): {inv.matched_past_incident.get('resolution')}" if (inv and inv.matched_past_incident) else "No prior matching incident in Incident Memory."}

## 5. Guardrailed Remediation & Human Approval

- **Proposal ID:** `{prop.proposal_id if prop else 'N/A'}`
- **Action:** {prop.title if prop else 'N/A'}
- **MCP Tool:** `{prop.mcp_tool if prop else 'N/A'}`
- **AWS CLI Equivalent:** `{prop.aws_cli_equivalent if prop else 'N/A'}`
- **Blast Radius:** {prop.blast_radius if prop else 'N/A'}
- **Rollback Plan:** {prop.rollback_plan if prop else 'N/A'}
- **Approval Status:** `{prop.status if prop else 'N/A'}`

## 6. Post-Remediation Verification Metrics

| Metric | Pre-Remediation | Post-Remediation | SLA Target |
| :--- | :--- | :--- | :--- |
| **Error Rate (%)** | `{pre_m.get('error_rate_pct', 0.0)}%` | `{post_m.get('error_rate_pct', 0.0) if post_m else 'Pending'}%` | `< 1.0%` |
| **P95 Latency (ms)** | `{pre_m.get('p95_latency_ms', 0)} ms` | `{post_m.get('p95_latency_ms', 0) if post_m else 'Pending'} ms` | `<= 100 ms` |
| **Memory RSS (MB)** | `{pre_m.get('memory_rss_mb', 0)} MB` | `{post_m.get('memory_rss_mb', 0) if post_m else 'Pending'} MB` | `< 435 MB (85%)` |
| **Queue Depth** | `{pre_m.get('queue_depth', 0)}` | `{post_m.get('queue_depth', 0) if post_m else 'Pending'}` | `< 20` |

**Verifier Summary:** {ver.summary if ver else "Verification pending remediation execution."}

## 7. Incident Timeline & Audit Trail

| Timestamp | Actor | Step | Details |
| :--- | :--- | :--- | :--- |
{chr(10).join(audit_lines) if audit_lines else "| - | - | - | - |"}
"""
    md_path.write_text(md_content, encoding="utf-8")

    # 2. Build OpenXML .docx document
    paragraphs: list[str] = [
        _w_para(f"Post-Incident Report (PIR): {incident.incident_id}", style="Heading1"),
        _w_para(f" {incident.title}", bold_prefix="Title:"),
        _w_para(f" {incident.service} (Scenario: {incident.scenario})", bold_prefix="Service:"),
        _w_para(f" {incident.status}", bold_prefix="Status:"),
        _w_para(f" {incident.created_at} -> {incident.updated_at}", bold_prefix="Window (UTC):"),
        _w_para("1. Executive Summary", style="Heading2"),
        _w_para(inv.summary if inv else incident.title),
        _w_para("2. Confirmed Root Cause & Task Definition Diff", style="Heading2"),
        _w_para(inv.root_cause if inv else "Pending investigation", bold_prefix="Root Cause: "),
    ]
    if inv and inv.task_def_diff:
        td = inv.task_def_diff
        paragraphs.append(
            _w_para(
                f"Active Revision {td.active_revision} vs Healthy Baseline {td.baseline_revision}",
                bold_prefix="ECS Revision Comparison: ",
            )
        )
        for k, v in td.changed_env.items():
            paragraphs.append(
                _w_para(f"{v.get('baseline', '')} -> {v.get('active', '')}", bold_prefix=f"  {k}: ")
            )

    paragraphs.append(_w_para("3. Investigator Hypotheses (Including Ruled-Out Hypotheses)", style="Heading2"))
    if inv and inv.hypotheses:
        for h in inv.hypotheses:
            paragraphs.append(
                _w_para(
                    f" {h.description} (Evidence: {'; '.join(h.evidence)})",
                    bold_prefix=f"[{h.status}] {h.id} {h.title}:",
                )
            )

    paragraphs.append(_w_para("4. Guardrailed Remediation & Approval Gate", style="Heading2"))
    if prop:
        paragraphs.append(_w_para(f" {prop.title} ({prop.mcp_tool})", bold_prefix="Action:"))
        paragraphs.append(_w_para(f" {prop.aws_cli_equivalent}", bold_prefix="AWS CLI:"))
        paragraphs.append(_w_para(f" {prop.blast_radius}", bold_prefix="Blast Radius:"))
        paragraphs.append(_w_para(f" {prop.rollback_plan}", bold_prefix="Rollback Plan:"))

    paragraphs.append(_w_para("5. Independent Verifier Results", style="Heading2"))
    if ver:
        paragraphs.append(_w_para(ver.summary))
        paragraphs.append(
            _w_para(
                f" Error Rate {pre_m.get('error_rate_pct', 0.0)}% -> {post_m.get('error_rate_pct', 0.0)}% | "
                f"P95 Latency {pre_m.get('p95_latency_ms', 0)}ms -> {post_m.get('p95_latency_ms', 0)}ms",
                bold_prefix="Telemetry Comparison:",
            )
        )
    else:
        paragraphs.append(_w_para("Verification pending human approval and execution."))

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body>{"".join(paragraphs)}'
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>'
        "</w:body></w:document>"
    )

    with zipfile.ZipFile(docx_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", DOCX_CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", DOCX_ROOT_RELS_XML)
        zf.writestr("word/_rels/document.xml.rels", DOCX_DOCUMENT_RELS_XML)
        zf.writestr("word/styles.xml", DOCX_STYLES_XML)
        zf.writestr("word/document.xml", document_xml)

    now_iso = utc_now_iso()
    docx_record = ArtifactRecord(
        name=docx_filename,
        kind="docx",
        path=str(docx_path.resolve()),
        url=f"/api/artifacts/{docx_filename}",
        size_bytes=docx_path.stat().st_size,
        created_at=now_iso,
    )
    md_record = ArtifactRecord(
        name=md_filename,
        kind="markdown",
        path=str(md_path.resolve()),
        url=f"/api/artifacts/{md_filename}",
        size_bytes=md_path.stat().st_size,
        created_at=now_iso,
    )
    return docx_record, md_record


def create_custom_document_report(
    title: str,
    content_markdown: str,
    output_dir: Path | str,
    fmt: str = "docx",
    filename: str | None = None,
) -> list[ArtifactRecord]:
    """Convert agent-authored Markdown report content into a downloadable .docx and/or .md file."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    epoch_ms = int(time.time() * 1000)
    slug = _SAFE_FILENAME_RE.sub("_", (filename or title or "report").lower()).strip("_")[:36] or "report"
    slug = re.sub(r"\.(docx|md)$", "", slug)

    norm_fmt = (fmt or "docx").strip().lower()
    want_docx = norm_fmt in ("docx", "word", "both")
    want_md = norm_fmt in ("md", "markdown", "both")
    if not want_docx and not want_md:
        want_docx = True

    clean_title = (title or "AWS Operational Report").strip()
    body_text = (content_markdown or "").strip()
    if not body_text.startswith("#"):
        full_md = f"# {clean_title}\n\n{body_text}\n"
    else:
        full_md = f"{body_text}\n"

    now_iso = utc_now_iso()
    records: list[ArtifactRecord] = []

    if want_docx:
        docx_filename = f"{slug}_{epoch_ms}.docx"
        docx_path = out_dir / docx_filename
        paragraphs: list[str] = []
        for raw_line in full_md.splitlines():
            line = raw_line.strip()
            if not line or line == "---":
                continue
            clean_line = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
            clean_line = re.sub(r"`(.*?)`", r"\1", clean_line)
            if clean_line.startswith("# "):
                paragraphs.append(_w_para(clean_line[2:].strip(), style="Heading1"))
            elif clean_line.startswith("## ") or clean_line.startswith("### "):
                heading_txt = re.sub(r"^#+\s*", "", clean_line)
                paragraphs.append(_w_para(heading_txt, style="Heading2"))
            elif clean_line.startswith("- ") or clean_line.startswith("* "):
                bullet_body = clean_line[2:].strip()
                if ":" in bullet_body:
                    prefix, rest = bullet_body.split(":", 1)
                    paragraphs.append(_w_para(rest, bold_prefix=f"• {prefix}:"))
                else:
                    paragraphs.append(_w_para(f"• {bullet_body}"))
            else:
                paragraphs.append(_w_para(clean_line))

        document_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body>{"".join(paragraphs)}'
            '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>'
            "</w:body></w:document>"
        )
        with zipfile.ZipFile(docx_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", DOCX_CONTENT_TYPES_XML)
            zf.writestr("_rels/.rels", DOCX_ROOT_RELS_XML)
            zf.writestr("word/_rels/document.xml.rels", DOCX_DOCUMENT_RELS_XML)
            zf.writestr("word/styles.xml", DOCX_STYLES_XML)
            zf.writestr("word/document.xml", document_xml)

        records.append(
            ArtifactRecord(
                name=docx_filename,
                kind="docx",
                path=str(docx_path.resolve()),
                url=f"/api/artifacts/{docx_filename}?v={epoch_ms}",
                size_bytes=docx_path.stat().st_size,
                created_at=now_iso,
            )
        )

    if want_md:
        md_filename = f"{slug}_{epoch_ms}.md"
        md_path = out_dir / md_filename
        md_path.write_text(full_md, encoding="utf-8")
        records.append(
            ArtifactRecord(
                name=md_filename,
                kind="markdown",
                path=str(md_path.resolve()),
                url=f"/api/artifacts/{md_filename}?v={epoch_ms}",
                size_bytes=md_path.stat().st_size,
                created_at=now_iso,
            )
        )

    return records
