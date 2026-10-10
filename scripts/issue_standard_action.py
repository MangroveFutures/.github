#!/usr/bin/env python3
"""GitHub Action driver for the ORG-0006 issue standard. Runs scripts/check_issue.py on issues.

Called by the reusable workflow MangroveFutures/.github/.github/workflows/issue-standard.yml.
It never closes an issue. What it does, and nothing else:

- An issue fails the check: add `needs-grooming` and post ONE comment saying exactly what is
  missing (edited in place on later runs, never duplicated).
- A failing issue carries `ready`: remove `ready` and say why (the hard gate at the commitment
  point, RFC-0005 §2) -- only with READY_GATE=enforce. In shadow mode (the default until ORG-0006
  graduation: 10+ runs, Rosie's spot-check, a record in docs/learnings.md) it only says it would have.
- An issue now passes: update the comment to say so, and remove `needs-grooming` only if this
  workflow added it (a person's `needs-grooming` is left alone).
- Issues labelled `spike` are discovery work and are not checked until they are labelled `ready`.

Environment (set by the workflow): GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_EVENT_NAME,
GITHUB_EVENT_PATH, GITHUB_API_URL; READY_GATE (shadow|enforce, default shadow); SWEEP_SCOPE
(ready|all, default ready); FORMS_DIR (optional). Standard library plus PyYAML (via check_issue).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_issue  # noqa: E402

MARKER = "<!-- issue-standard -->"
ADDED_MARKER = "<!-- issue-standard:added-needs-grooming -->"
BOT_LOGIN = "github-actions[bot]"
NEEDS_GROOMING = "needs-grooming"
READY = "ready"
DISCOVERY_LABELS = {"spike"}
STANDARD_LINK = (
    "https://github.com/MangroveFutures/home/blob/main/decisions/"
    "ORG-0006-work-items-one-standard-enforced-and-pdsa.md"
)


@dataclass
class Plan:
    add_labels: list[str] = field(default_factory=list)
    remove_labels: list[str] = field(default_factory=list)
    comment: str | None = None  # new text for the one comment; None = leave it alone
    summary: str = ""


def plan(
    result: check_issue.Result,
    labels: list[str],
    existing_comment: str | None,
    ready_gate: str = "shadow",
) -> Plan:
    """Decide what to do. Pure: no network, so it is unit-tested directly."""
    names = {label.casefold() for label in labels}
    is_ready = READY in names
    out = Plan()
    if names & DISCOVERY_LABELS and not is_ready:
        out.summary = "skipped: discovery work (spike), not checked until labelled ready"
        return out

    failing = result.checked and not result.ok
    if failing:
        we_added = NEEDS_GROOMING not in names or (existing_comment or "").find(ADDED_MARKER) >= 0
        if NEEDS_GROOMING not in names:
            out.add_labels.append(NEEDS_GROOMING)
        lines = [MARKER]
        if we_added:
            lines.append(ADDED_MARKER)
        lines.append(f"**This issue does not meet the {result.form} standard yet** ([ORG-0006]({STANDARD_LINK})).")
        lines.append("")
        lines.append("Missing or empty:")
        lines += [f"- {p.text()}" for p in result.problems]
        lines.append("")
        if is_ready and ready_gate == "enforce":
            out.remove_labels.append(READY)
            lines.append(
                "**I removed the `ready` label.** An issue is ready only when an agent with no other "
                "context could pick it up and do it, and that needs every section above. Add them, "
                "then add `ready` again."
            )
        elif is_ready:
            lines.append(
                "Shadow mode: this check would have removed the `ready` label. It is only reporting "
                "for now (RFC-0005 §6)."
            )
        else:
            lines.append(
                "Nothing is blocked: capture is welcome as it is. These sections are needed before "
                "the issue can be labelled `ready`."
            )
        lines.append("")
        lines.append(
            f"How to fix it: edit the issue body and add the sections, each as a `### ` heading "
            f"(the same headings the issue form uses). Which form I checked against was decided by "
            f"{result.how_found}; a `<!-- form: task -->` line (or bug, decision, bet) sets it. "
            "This comment updates itself when the issue is edited."
        )
        text = "\n".join(lines)
        out.comment = None if _same(text, existing_comment) else text
        out.summary = "fails: " + ", ".join(p.heading for p in result.problems)
        return out

    # Passing, or not checked (for example an epic).
    out.summary = "passes" if result.checked else "not checked: " + result.how_found
    if existing_comment and "does not meet" in existing_comment:
        if ADDED_MARKER in existing_comment and NEEDS_GROOMING in names:
            out.remove_labels.append(NEEDS_GROOMING)
        what = (
            f"**This issue now meets the {result.form} standard** ([ORG-0006]({STANDARD_LINK}))."
            if result.checked
            else f"This issue is no longer checked ({result.how_found})."
        )
        removed = " I removed the `needs-grooming` label I had added." if NEEDS_GROOMING in out.remove_labels else ""
        out.comment = f"{MARKER}\n{what}{removed}"
    return out


def _same(new: str, old: str | None) -> bool:
    return old is not None and old.strip() == new.strip()


class GitHub:
    def __init__(self, token: str, repo: str, api: str = "https://api.github.com") -> None:
        self.token, self.repo, self.api = token, repo, api.rstrip("/")

    def call(self, method: str, path: str, data: dict | list | None = None):
        url = path if path.startswith("http") else f"{self.api}/repos/{self.repo}{path}"
        body = json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return (json.loads(raw) if raw else None), response.headers.get("Link", "")

    def paged(self, path: str):
        url = f"{self.api}/repos/{self.repo}{path}"
        while url:
            items, link = self.call("GET", url)
            yield from items
            url = next(
                (p.split(";")[0].strip()[1:-1] for p in link.split(",") if 'rel="next"' in p), None
            )

    def bot_comment(self, number: int) -> dict | None:
        for comment in self.paged(f"/issues/{number}/comments?per_page=100"):
            if (comment.get("user") or {}).get("login") == BOT_LOGIN and MARKER in (comment.get("body") or ""):
                return comment
        return None


def handle(gh: GitHub, issue: dict, forms: dict, ready_gate: str) -> str:
    number = issue["number"]
    labels = [label["name"] for label in issue.get("labels", [])]
    issue_type = (issue.get("type") or {}).get("name")
    result = check_issue.check(issue.get("body") or "", labels, issue_type, forms)
    existing = gh.bot_comment(number)
    todo = plan(result, labels, existing and existing.get("body"), ready_gate)
    if todo.add_labels:
        gh.call("POST", f"/issues/{number}/labels", {"labels": todo.add_labels})
    for label in todo.remove_labels:
        try:
            gh.call("DELETE", f"/issues/{number}/labels/{urllib.parse.quote(label)}")
        except urllib.error.HTTPError as exc:  # already gone: nothing to do
            if exc.code != 404:
                raise
    if todo.comment is not None:
        if existing:
            gh.call("PATCH", f"/issues/comments/{existing['id']}", {"body": todo.comment})
        else:
            gh.call("POST", f"/issues/{number}/comments", {"body": todo.comment})
    line = f"#{number}: {todo.summary}"
    print(line)
    return line


def main() -> int:
    env = os.environ
    gh = GitHub(env["GITHUB_TOKEN"], env["GITHUB_REPOSITORY"], env.get("GITHUB_API_URL", "https://api.github.com"))
    forms_dir = Path(env["FORMS_DIR"]) if env.get("FORMS_DIR") else check_issue.default_forms_dir()
    forms = check_issue.load_forms(forms_dir)
    ready_gate = env.get("READY_GATE", "shadow").strip().lower()
    if ready_gate not in {"enforce", "shadow"}:
        print(f"READY_GATE must be enforce or shadow, not {ready_gate!r}", file=sys.stderr)
        return 2
    event_name = env.get("GITHUB_EVENT_NAME", "")
    lines: list[str] = []
    if event_name == "issues":
        event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        # Re-read the issue: the event may be stale by the time this runs (labels change fast), and
        # the REST issue carries its issue type, which decides whether an epic is checked at all.
        fresh, _ = gh.call("GET", f"/issues/{event['issue']['number']}")
        issue = fresh or event["issue"]
        if issue.get("state") != "open" or issue.get("pull_request"):
            print("Not an open issue; nothing to do.")
            return 0
        lines.append(handle(gh, issue, forms, ready_gate))
    else:  # schedule or workflow_dispatch: sweep
        scope = env.get("SWEEP_SCOPE", "ready").strip().lower()
        query = "/issues?state=open&per_page=100" + ("&labels=ready" if scope != "all" else "")
        for issue in gh.paged(query):
            if issue.get("pull_request"):
                continue
            lines.append(handle(gh, issue, forms, ready_gate))
        if not lines:
            lines.append(f"No open issues in scope ({scope}).")
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle_:
            handle_.write("### Issue standard (ORG-0006)\n\n" + "\n".join(f"- {l}" for l in lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
