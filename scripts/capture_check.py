#!/usr/bin/env python3
"""What an agent-made capture should carry, and the template that shows how (home #426).

ORG-0006 keeps capture free: a bare issue labelled `needs-grooming` is always filed. This module
never decides that. It only names what a capture is missing so the agent can add it at the moment
it writes the capture, which is cheaper than Doug reconstructing it at grooming. The issue hook
turns the gaps into a warning (shadow mode: advice, never a block); the same template is in the
work-item-writing skill and in the web form org-dotgithub/ISSUE_TEMPLATE/capture.yml.

Standard library only, so the warning still works when PyYAML (needed by check_issue.py) is absent.

Limits, said plainly: it checks that the headings exist, are not empty, and that the origin has an
absolute date. It cannot tell whether the agent or session named is real, or whether the Source
links are the right ones.
"""

from __future__ import annotations

import re

CAPTURE_TEMPLATE = """\
### What
(one or two sentences: what is going on, or what should exist)
### Why it matters
(what goes wrong, or what is gained)
### Source
(links, file paths, issue numbers, session or turn ids; a few words beside each)
### Where it came from
(agent or person, session, absolute date, for example: Doug (agent), session abc123, 19 Oct 2026)
### Parent epic or decision
(#123 or owner/repo#45; write "none known" if you do not know)
### Suggested next step
(what you would do next, and who)"""

HEADING = re.compile(r"^ {0,3}#{1,3}\s+(.*?)\s*#*\s*$")
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
DATE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}\b",
    re.IGNORECASE,
)
PARENT_LINE = re.compile(r"^\s*(?:[-*]\s*)?\**parent\**\s*:\s*\S", re.IGNORECASE | re.MULTILINE)


def _sections(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    current, buf = None, []
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        m = HEADING.match(line)
        if m:
            if current is not None and current not in out:
                out[current] = "\n".join(buf)
            current, buf = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(":").casefold(), []
        elif current is not None:
            buf.append(line)
    if current is not None and current not in out:
        out[current] = "\n".join(buf)
    return out


def _filled(text: str | None) -> bool:
    cleaned = COMMENT.sub("", text or "").strip()
    return bool(cleaned) and cleaned.casefold() != "_no response_"


def _find(sections: dict[str, str], prefix: str) -> str | None:
    return next((v for k, v in sections.items() if k.startswith(prefix)), None)


def capture_gaps(body: str) -> list[str]:
    """Names of what the capture lacks: any of "Source", "Where it came from", "Parent"."""
    sections = _sections(body)
    gaps = []
    if not _filled(_find(sections, "source")):
        gaps.append("Source")
    origin = _find(sections, "where it came from")
    if not (_filled(origin) and DATE.search(COMMENT.sub("", origin))):
        gaps.append("Where it came from")
    if not (_filled(_find(sections, "parent")) or PARENT_LINE.search(COMMENT.sub("", body or ""))):
        gaps.append("Parent")
    return gaps


def warning_text(gaps: list[str]) -> str:
    needs = {
        "Source": "`### Source` (links, file paths, issue or turn ids)",
        "Where it came from": "`### Where it came from` (agent, session and an absolute date)",
        "Parent": "`### Parent epic or decision` (or a `Parent: #123` line)",
    }
    lines = [
        "ORG-0006 capture note (advice only, the capture is not blocked and will be filed as written): "
        "this `needs-grooming` capture lacks:",
        *[f"- {needs[g]}" for g in gaps],
        "Why: Doug grooms from what is in the issue; each missing item is something he must dig out of "
        "sessions later. Way out: if you know them, add them now by editing the issue body (or "
        "creating it again with the template below). If you do not know Source or Parent, write \"unknown\" under "
        "its heading, which is better than silence; the origin you always know (you are the agent). Template to paste:",
        "",
        CAPTURE_TEMPLATE,
    ]
    return "\n".join(lines)
