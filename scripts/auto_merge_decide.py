#!/usr/bin/env python3
"""Decide whether a pull request may switch on auto-merge (ORG-0005 section 1, ORG-0023).

Called by the reusable workflow MangroveFutures/.github/.github/workflows/auto-merge.yml. The rule is
COMPUTED from the file list, never judged:

- documents: every changed file is a document path (docs, handbook, research, playbook, agent
  memory) with a document extension, and none is on the deny list (workflows, code, migrations,
  settings, decisions, ADRs, RFCs, CLAUDE.md, AGENTS.md, principles.md, scheduled-review tripwire files).
- dependabot-patch: the author is dependabot[bot], every updated dependency is a semver patch, and
  the files are only manifests and lockfiles.

It refuses (fails closed) when: the PR is a draft, from a fork, labelled `no-auto-merge`, the base
branch has no required status check (auto-merge would then merge at once, skipping checks), the file
list is empty or too long to trust, or anything could not be read. A PR that earlier got auto-merge
from this workflow and no longer qualifies has it switched off again.

Environment (set by the workflow): GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_EVENT_PATH, GITHUB_API_URL,
GITHUB_OUTPUT, GITHUB_STEP_SUMMARY; UPDATES_JSON (dependabot/fetch-metadata `updated-dependencies-json`).
Standard library only. Outputs: merge (true|false), kind, reason, disable (true|false), number.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import sys
import urllib.request
from dataclasses import dataclass

OPT_OUT_LABEL = "no-auto-merge"
BOT = "github-actions[bot]"
DEPENDABOT = "dependabot[bot]"
MAX_FILES = 300  # GitHub lists at most 3000; beyond a few hundred a "document" PR is not one.

DOC_PREFIXES = ("docs/", "handbook/", "research/", "playbook/")
DOC_FILES = {"changelog.md", "readme.md"}
AGENT_DOC = re.compile(r"^agents/[a-z0-9_-]+/(memory|notes|learnings)\.md$")
DOC_EXTENSIONS = (".md", ".txt", ".png", ".jpg", ".jpeg", ".svg", ".csv")
# Checked on the lower-cased path, before the allow rules; a match always refuses.
DENY_PREFIXES = (
    ".github/", "org-dotgithub/", ".claude/", "settings/", "migrations/", "supabase/", "src/",
    "scripts/", "tests/", "decisions/", "skills/", "docs/adr/", "docs/rfc/", "docs/reviews/scheduled/",
)
DENY_FILES = {"claude.md", "agents.md", "principles.md"}

MANIFESTS = {"pyproject.toml", "uv.lock", "poetry.lock", "pipfile.lock", "package.json", "package-lock.json"}
MANIFEST_RE = re.compile(r"^requirements[\w.-]*\.txt$")


@dataclass
class Decision:
    merge: bool
    kind: str
    reason: str


def _doc_path_problem(path: str) -> str | None:
    """None if `path` is a document path, else why not."""
    if not path or path.startswith("/") or ".." in path.split("/") or "//" in path:
        return f"{path}: odd path"
    p = posixpath.normpath(path).lower()
    if p in DENY_FILES or p.startswith(DENY_PREFIXES):
        return f"{path}: on the deny list (code, workflow, settings, migration, decision or rule file)"
    if not p.endswith(DOC_EXTENSIONS):
        return f"{path}: not a document file type"
    if p.startswith(DOC_PREFIXES) or p in DOC_FILES or AGENT_DOC.match(p):
        return None
    return f"{path}: not under a document path"


def _manifest_problem(path: str) -> str | None:
    name = posixpath.basename(path).lower()
    if ".." in path.split("/") or path.startswith("/"):
        return f"{path}: odd path"
    if path.lower().startswith((".github/", "org-dotgithub/", "src/", "scripts/", "tests/", "migrations/", "supabase/")):
        return f"{path}: not a manifest or lockfile"
    if name in MANIFESTS or MANIFEST_RE.match(name):
        return None
    return f"{path}: not a manifest or lockfile"


def has_required_checks(rules) -> bool:
    for rule in rules or []:
        if rule.get("type") == "required_status_checks":
            if (rule.get("parameters") or {}).get("required_status_checks"):
                return True
    return False


def decide(pr: dict, files: list[dict], updates, rules) -> Decision:
    no = lambda why: Decision(False, "", why)  # noqa: E731
    if pr.get("draft"):
        return no("draft pull request")
    if any(label.get("name") == OPT_OUT_LABEL for label in pr.get("labels") or []):
        return no(f"labelled {OPT_OUT_LABEL}")
    head = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    base = ((pr.get("base") or {}).get("repo") or {}).get("full_name")
    if not head or head != base:
        return no("branch is not in this repository (fork or deleted)")
    if not has_required_checks(rules):
        return no("the base branch has no required status check, so auto-merge would skip the checks")
    if not files:
        return no("no files listed")
    if len(files) >= MAX_FILES:
        return no(f"{len(files)} files is too many to treat as a document change")

    paths = []
    for entry in files:
        if entry.get("status") == "removed":
            return no(f"{entry.get('filename')}: deletes a file")
        paths.append(entry.get("filename", ""))
        if entry.get("previous_filename"):
            paths.append(entry["previous_filename"])

    if ((pr.get("user") or {}).get("login")) == DEPENDABOT:
        if not updates or any(u.get("updateType") != "version-update:semver-patch" for u in updates):
            return no("not every update is a semver patch (or no metadata)")
        for path in paths:
            problem = _manifest_problem(path)
            if problem:
                return no(problem)
        return Decision(True, "dependabot-patch", f"patch update(s) to {len(updates)} package(s), manifests only")

    for path in paths:
        problem = _doc_path_problem(path)
        if problem:
            return no(problem)
    return Decision(True, "documents", f"{len(files)} file(s), all document paths")


def should_disable(pr: dict, decision: Decision) -> bool:
    """Switch auto-merge off only if this workflow switched it on and the PR no longer qualifies."""
    if decision.merge:
        return False
    enabled_by = ((pr.get("auto_merge") or {}).get("enabled_by") or {}).get("login")
    return enabled_by == BOT


# ---- thin network wrapper (exercised by the workflow itself) ----

def _get(url: str, token: str):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _write(path: str | None, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)


def main() -> int:
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
        number = json.load(fh)["pull_request"]["number"]
    pr = {}
    decision = Decision(False, "", "")
    try:
        pr = _get(f"{api}/repos/{repo}/pulls/{number}", token)
        files, page = [], 1
        while True:
            chunk = _get(f"{api}/repos/{repo}/pulls/{number}/files?per_page=100&page={page}", token)
            files += chunk
            if len(chunk) < 100 or page >= 4:
                break
            page += 1
        rules = _get(f"{api}/repos/{repo}/rules/branches/{pr['base']['ref']}", token)
        updates = json.loads(os.environ["UPDATES_JSON"]) if os.environ.get("UPDATES_JSON") else None
        decision = decide(pr, files, updates, rules)
    except Exception as exc:  # fail closed: a read error never enables auto-merge
        decision = Decision(False, "", f"could not read the pull request: {type(exc).__name__}: {exc}")
    disable = should_disable(pr, decision)
    out = os.environ.get("GITHUB_OUTPUT")
    reason = decision.reason.replace("\n", " ")
    _write(out, f"merge={'true' if decision.merge else 'false'}\nkind={decision.kind}\n"
                f"disable={'true' if disable else 'false'}\nnumber={number}\nreason={reason}\n")
    _write(os.environ.get("GITHUB_STEP_SUMMARY"),
           f"### Auto-merge: {'ON' if decision.merge else 'not enabled'}\n\n{reason}\n")
    print(f"merge={decision.merge} kind={decision.kind} disable={disable}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
