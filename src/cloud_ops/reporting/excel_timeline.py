"""Generate genuine OpenXML (.xlsx) incident timelines using only Python's standard library."""

from __future__ import annotations

from pathlib import Path
import re
import time
from typing import Any
from xml.sax.saxutils import escape
import zipfile

from cloud_ops.schemas import ArtifactRecord, IncidentRecord, utc_now_iso

_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


def _col_letter(col_idx_zero_based: int) -> str:
    """Convert 0-based column index to Excel column letter (A, B, ..., Z, AA...)."""
    result = ""
    n = col_idx_zero_based + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def _build_sheet_xml(rows: list[list[Any]], col_widths: list[int] | None = None) -> str:
    """Build valid OpenXML worksheet XML using inlineStr and numeric cells."""
    cols_xml = ""
    if col_widths:
        col_defs = [
            f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
            for i, w in enumerate(col_widths)
        ]
        cols_xml = f"<cols>{''.join(col_defs)}</cols>"

    row_elements: list[str] = []
    for r_idx, row in enumerate(rows, start=1):
        cell_elements: list[str] = []
        style_idx = "1" if r_idx == 1 else "0"
        for c_idx, val in enumerate(row):
            cell_ref = f"{_col_letter(c_idx)}{r_idx}"
            if isinstance(val, (int, float)) and not isinstance(val, bool) and r_idx > 1:
                cell_elements.append(f'<c r="{cell_ref}" s="{style_idx}"><v>{val}</v></c>')
            else:
                safe_text = escape(str(val if val is not None else ""))
                cell_elements.append(
                    f'<c r="{cell_ref}" s="{style_idx}" t="inlineStr">'
                    f'<is><t xml:space="preserve">{safe_text}</t></is></c>'
                )
        row_elements.append(f'<row r="{r_idx}">{"".join(cell_elements)}</row>')

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"{cols_xml}"
        f'<sheetData>{"".join(row_elements)}</sheetData>'
        "</worksheet>"
    )


CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet3.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""

ROOT_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

WORKBOOK_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Incident Summary" sheetId="1" r:id="rId1"/>
    <sheet name="Event &amp; Log Timeline" sheetId="2" r:id="rId2"/>
    <sheet name="Hypotheses &amp; Metrics" sheetId="3" r:id="rId3"/>
  </sheets>
</workbook>"""

WORKBOOK_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet3.xml"/>
  <Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2">
    <font><sz val="11"/><color theme="1"/><name val="Calibri"/></font>
    <font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>
  </fonts>
  <fills count="3">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF1E293B"/></patternFill></fill>
  </fills>
  <borders count="1">
    <border><left/><right/><top/><bottom/><diagonal/></border>
  </borders>
  <cellStyleXfs count="1">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>
  </cellStyleXfs>
  <cellXfs count="2">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
  </cellXfs>
</styleSheet>"""


def generate_excel_timeline(
    incident: IncidentRecord,
    output_dir: Path | str,
) -> ArtifactRecord:
    """Generate a 3-sheet OpenXML .xlsx workbook detailing the incident timeline and metrics."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_id = incident.incident_id.lower().replace(" ", "_")
    filename = f"incident_{safe_id}_timeline.xlsx"
    out_path = out_dir / filename

    inv = incident.investigation
    prop = incident.proposal
    ver = incident.verification

    # Sheet 1: Incident Summary
    sheet1_rows: list[list[Any]] = [
        ["Field", "Value", "Notes"],
        ["Incident ID", incident.incident_id, "Primary incident identifier"],
        ["Title", incident.title, "Operational incident headline"],
        ["Target Service", incident.service, "AWS ECS Fargate microservice"],
        ["Fault Scenario", incident.scenario, "Active or remediated scenario"],
        ["Current Status", incident.status, "Lifecycle status"],
        ["Created At (UTC)", incident.created_at, "Detection timestamp"],
        ["Updated At (UTC)", incident.updated_at, "Last state transition"],
        [
            "Root Cause",
            inv.root_cause if inv else "Pending investigation",
            "Confirmed by InvestigatorAgent",
        ],
        [
            "Active vs Baseline Task Def",
            (
                f"{inv.task_def_diff.active_revision} (baseline: {inv.task_def_diff.baseline_revision})"
                if (inv and inv.task_def_diff)
                else "N/A"
            ),
            "ECS task definition comparison",
        ],
        [
            "Matched Historical Incident",
            (
                f"{inv.matched_past_incident.get('incident_id')} - {inv.matched_past_incident.get('title')}"
                if (inv and inv.matched_past_incident)
                else "None"
            ),
            "Incident Memory match",
        ],
        [
            "Proposed Remediation",
            prop.title if prop else "N/A",
            prop.aws_cli_equivalent if prop else "",
        ],
        [
            "Verification Outcome",
            (
                f"VERIFIED (Post Error Rate: {ver.post_metrics.get('error_rate_pct', 0.0)}%)"
                if (ver and ver.verified)
                else ("NOT YET VERIFIED" if not ver else "FAILED / ROLLED BACK")
            ),
            ver.summary if ver else "Awaiting remediation execution",
        ],
    ]

    # Sheet 2: Event & Log Timeline
    sheet2_rows: list[list[Any]] = [
        [
            "Timestamp (UTC)",
            "Phase / Source",
            "Level / Status",
            "Event / Action",
            "HTTP Status",
            "Latency (ms)",
            "Details / Message",
        ]
    ]
    for audit_entry in incident.audit_trail:
        sheet2_rows.append(
            [
                audit_entry.get("timestamp", incident.created_at),
                audit_entry.get("actor", "cloud-ops-agent"),
                audit_entry.get("status", "AUDIT"),
                audit_entry.get("step", "lifecycle_event"),
                200,
                0,
                audit_entry.get("detail", ""),
            ]
        )

    if inv and inv.sample_logs:
        for log_ev in inv.sample_logs[:15]:
            msg = log_ev.get("message") or (log_ev.get("error") or {}).get("message") or log_ev.get("event", "")
            sheet2_rows.append(
                [
                    log_ev.get("timestamp", incident.created_at),
                    f"CloudWatch ({incident.service} pre-fix)",
                    log_ev.get("level", "INFO"),
                    log_ev.get("event", "tick"),
                    int(log_ev.get("http_status", 200) or 200),
                    int(log_ev.get("latency_ms", 0) or 0),
                    msg,
                ]
            )

    if ver and ver.post_logs:
        for log_ev in ver.post_logs[:8]:
            msg = log_ev.get("message") or log_ev.get("event", "healthy tick")
            sheet2_rows.append(
                [
                    log_ev.get("timestamp", ver.timestamp),
                    f"Verifier ({incident.service} post-fix)",
                    log_ev.get("level", "INFO"),
                    log_ev.get("event", "tick"),
                    int(log_ev.get("http_status", 200) or 200),
                    int(log_ev.get("latency_ms", 0) or 0),
                    msg,
                ]
            )

    # Sheet 3: Hypotheses & Metrics
    sheet3_rows: list[list[Any]] = [
        ["Category", "Identifier / Metric", "Status / Pre-Fix", "Post-Fix / Target", "Evidence & Notes"]
    ]
    if inv:
        for hyp in inv.hypotheses:
            sheet3_rows.append(
                [
                    "Hypothesis",
                    f"{hyp.id}: {hyp.title}",
                    hyp.status,
                    "Evaluated",
                    f"{hyp.description} | Evidence: {'; '.join(hyp.evidence)}",
                ]
            )
        pre_m = inv.metrics_snapshot or {}
        post_m = ver.post_metrics if ver else {}
        sheet3_rows.extend(
            [
                [
                    "Metric",
                    "Error Rate (%)",
                    float(pre_m.get("error_rate_pct", 0.0)),
                    float(post_m.get("error_rate_pct", 0.0)) if post_m else "Pending",
                    "SLA Target < 1.0%",
                ],
                [
                    "Metric",
                    "P95 Latency (ms)",
                    int(pre_m.get("p95_latency_ms", 0)),
                    int(post_m.get("p95_latency_ms", 0)) if post_m else "Pending",
                    "SLA Target <= 100ms",
                ],
                [
                    "Metric",
                    "Container Memory RSS (MB)",
                    int(pre_m.get("memory_rss_mb", 0)),
                    int(post_m.get("memory_rss_mb", 0)) if post_m else "Pending",
                    "Container Limit: 512MB",
                ],
                [
                    "Metric",
                    "Queue Depth",
                    int(pre_m.get("queue_depth", 0)),
                    int(post_m.get("queue_depth", 0)) if post_m else "Pending",
                    "Worker backlog depth",
                ],
            ]
        )

    with zipfile.ZipFile(out_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", ROOT_RELS_XML)
        zf.writestr("xl/workbook.xml", WORKBOOK_XML)
        zf.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS_XML)
        zf.writestr("xl/styles.xml", STYLES_XML)
        zf.writestr("xl/worksheets/sheet1.xml", _build_sheet_xml(sheet1_rows, [28, 52, 45]))
        zf.writestr(
            "xl/worksheets/sheet2.xml",
            _build_sheet_xml(sheet2_rows, [24, 28, 16, 28, 14, 14, 70]),
        )
        zf.writestr(
            "xl/worksheets/sheet3.xml",
            _build_sheet_xml(sheet3_rows, [18, 34, 20, 20, 75]),
        )

    return ArtifactRecord(
        name=filename,
        kind="excel",
        path=str(out_path.resolve()),
        url=f"/api/artifacts/{filename}",
        size_bytes=out_path.stat().st_size,
        created_at=utc_now_iso(),
    )


def create_custom_excel_workbook(
    title: str,
    sheets: list[dict[str, Any]],
    output_dir: Path | str,
    filename: str | None = None,
) -> ArtifactRecord:
    """Create a dynamic OpenXML .xlsx workbook from agent-supplied sheet definitions."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    epoch_ms = int(time.time() * 1000)
    if not filename:
        slug = _SAFE_FILENAME_RE.sub("_", (title or "report").lower()).strip("_")[:36] or "report"
        filename = f"excel_{slug}_{epoch_ms}.xlsx"
    elif not filename.endswith(".xlsx"):
        filename = f"{filename}.xlsx"
    out_path = out_dir / filename

    clean_sheets: list[dict[str, Any]] = []
    for idx, s in enumerate(sheets or []):
        if not isinstance(s, dict):
            continue
        raw_name = str(s.get("name") or f"Sheet{idx + 1}")[:31]
        safe_sheet_name = re.sub(r"[\[\]:*?/\\]", "_", raw_name).strip() or f"Sheet{idx + 1}"
        headers = [str(h) for h in (s.get("headers") or [])]
        rows_in = s.get("rows") or []
        all_rows: list[list[Any]] = []
        if headers:
            all_rows.append(headers)
        for r in rows_in:
            if isinstance(r, list):
                all_rows.append(r)
            elif isinstance(r, dict) and headers:
                all_rows.append([r.get(h, "") for h in headers])
        if not all_rows:
            all_rows = [["Info"], [f"No rows provided for {safe_sheet_name}"]]
        clean_sheets.append({"name": safe_sheet_name, "rows": all_rows})

    if not clean_sheets:
        clean_sheets = [{"name": "Report", "rows": [["Title"], [title or "AWS Report"]]}]

    override_parts = [
        '  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
    ]
    sheet_tags: list[str] = []
    rel_tags: list[str] = []

    for i, s in enumerate(clean_sheets, start=1):
        override_parts.append(
            f'  <Override PartName="/xl/worksheets/sheet{i}.xml" '
            f'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
        sheet_tags.append(
            f'    <sheet name="{escape(s["name"])}" sheetId="{i}" r:id="rId{i}"/>'
        )
        rel_tags.append(
            f'  <Relationship Id="rId{i}" '
            f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{i}.xml"/>'
        )

    style_rid = len(clean_sheets) + 1
    rel_tags.append(
        f'  <Relationship Id="rId{style_rid}" '
        f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        f'Target="styles.xml"/>'
    )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        '  <Default Extension="xml" ContentType="application/xml"/>\n'
        + "\n".join(override_parts)
        + "\n</Types>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">\n'
        "  <sheets>\n"
        + "\n".join(sheet_tags)
        + "\n  </sheets>\n</workbook>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        + "\n".join(rel_tags)
        + "\n</Relationships>"
    )

    with zipfile.ZipFile(out_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", ROOT_RELS_XML)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/styles.xml", STYLES_XML)
        for i, s in enumerate(clean_sheets, start=1):
            col_cnt = max((len(r) for r in s["rows"]), default=1)
            widths = [26 if c < col_cnt - 1 else 64 for c in range(col_cnt)]
            zf.writestr(f"xl/worksheets/sheet{i}.xml", _build_sheet_xml(s["rows"], widths))

    return ArtifactRecord(
        name=filename,
        kind="excel",
        path=str(out_path.resolve()),
        url=f"/api/artifacts/{filename}?v={epoch_ms}",
        size_bytes=out_path.stat().st_size,
        created_at=utc_now_iso(),
    )
