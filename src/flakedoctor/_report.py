"""Report rendering: the terminal block and the flakedoctor-report v1 JSON.

The terminal block IS the product — it is designed to be screenshot into a
GitHub issue or Slack thread. Plain text, no color dependencies.

flakedoctor-report v1 is a stated contract: additive-only within version 1;
consumers must ignore unknown keys.
"""

from __future__ import annotations

import os
import textwrap

from ._diagnose import Diagnosis

WIDTH = 72
_TITLE = " flakedoctor "


def _bar(title: str = "") -> str:
    if not title:
        return "━" * WIDTH
    return "━" * 3 + title + "━" * max(0, WIDTH - 3 - len(title))


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return "…" + text[-(limit - 1) :]


def render_block(diagnosis: Diagnosis, python_version: str, platform_name: str) -> str:
    d = diagnosis
    lines: list[str] = []
    lines.append(_bar(_TITLE))
    lines.append(f" {_truncate(d.nodeid, WIDTH - 2)}")
    lines.append(
        f" py{python_version} · {platform_name} · {d.total_runs} runs · {d.elapsed:.1f}s"
    )
    lines.append("")
    lines.append(f" DIAGNOSIS  {d.headline}")
    for para in d.explanation.split("\n"):
        for wrapped in textwrap.wrap(para, width=WIDTH - 3) or [""]:
            lines.append(f"   {wrapped}")
    if d.evidence:
        lines.append("")
        label_width = WIDTH - 2 - 14  # room for "runs   failed"
        lines.append(f" {'EVIDENCE':<{label_width}}  runs  failed")
        for row in d.evidence:
            label = _truncate(row.label, label_width - 1)
            line = f"   {label:<{label_width - 2}} {row.runs:>4} {row.failed:>7}"
            if row.note:
                line += f"   {row.note}"
            lines.append(line)
    if d.repro_command:
        lines.append("")
        note = d.repro.get("note", "") if d.repro else ""
        lines.append(f" REPRO ({note})" if note else " REPRO")
        lines.append(f"   {d.repro_command}")
        marker = d.repro.get("marker") if d.repro else None
        if marker:
            lines.append("   or, to make it reproduce on every run until fixed:")
            lines.append(f"   {marker}")
    if d.warnings:
        lines.append("")
        lines.append(" WARNINGS")
        for warning in d.warnings:
            wrapped_lines = textwrap.wrap(warning, width=WIDTH - 5) or [""]
            for i, wrapped in enumerate(wrapped_lines):
                lines.append(("   - " if i == 0 else "     ") + wrapped)
    lines.append(_bar())
    return "\n".join(lines)


def json_payload(
    diagnosis: Diagnosis,
    tool_version: str,
    python_version: str,
    platform_name: str,
) -> dict:
    d = diagnosis
    repro = None
    if d.repro is not None:
        repro = dict(d.repro)
        repro["command"] = d.repro_command
    return {
        "format": "flakedoctor-report",
        "version": 1,
        "tool": {"name": "pytest-flakedoctor", "version": tool_version},
        "env": {
            "python": python_version,
            "platform": platform_name,
            "hashseed_inherited": os.environ.get("PYTHONHASHSEED"),
        },
        "nodeid": d.nodeid,
        "verdict": d.verdict,
        "claim": d.claim,
        "headline": d.headline,
        "explanation": d.explanation,
        "evidence": [
            {"label": row.label, "runs": row.runs, "failed": row.failed, "note": row.note}
            for row in d.evidence
        ],
        "repro": repro,
        "warnings": list(d.warnings),
        "stats": d.stats,
        "elapsed_seconds": round(d.elapsed, 2),
        "total_runs": d.total_runs,
    }
