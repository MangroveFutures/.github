#!/usr/bin/env python3
"""Decide whether a pull request may switch on auto-merge (ORG-0005 section 1, ORG-0023).

Called by the reusable workflow MangroveFutures/.github/.github/workflows/auto-merge.yml. The rule is
COMPUTED from the file list, never judged:

- documents: every changed file is a document path (docs, handbook, research, playbook, agent
  memory) with a document extension, and none is on the deny list (workflows, code, migrations,
  settings, decisions, ADRs, RFCs, CLAUDE.md, AGENTS.md, principles.md, scheduled-review tripwire files).
  A repo may ADD document paths in `.github/auto-merge.yml` on its base branch (`doc_prefixes:` and
  `doc_files:` lists; see parse_config). It only adds: the deny list, the document file types and every
  other guard still apply, so a config can never make code, workflows, migrations or rule files merge.
  A config that cannot be read or understood refuses the pull request (fails closed).
- app-views (epoch only, Rosie 25 Oct 2026): as documents, plus files under src/epoch/apps/ that are not hidden,
  not secret-, token-, permission- or migration-named and not .sql/.pem/.key/.db; everything else on the
  deny list still refuses. Rollback: app versions are kept side by side (/v1, /v2); revert the squash commit.
- dependabot-patch: the author is dependabot[bot], every updated dependency is a semver patch, and
  the files are only manifests and lockfiles.

It refuses (fails closed) when: the PR is a draft, from a fork, labelled `no-auto-merge`, the base
branch has no required status check (auto-merge would then merge at once, skipping checks), the file
list is empty or too long to trust, or anything could not be read. A PR that earlier got auto-merge
from this workflow and no longer qualifies has it switched off again.

Environment (set by the workflow): GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_EVENT_PATH, GITHUB_API_URL,
GITHUB_OUTPUT, GITHUB_STEP_SUMMARY; UPDATES_JSON (dependabot/fetch-metadata `updated-dependencies-json`).
Standard library only. Outputs: merge (true|false), kind, reason, disable (true|false), number.
When it decides NOT to merge it also says why, as one pull request comment (edited in place, marked
MARKER) and in the job summary, so a passing job that armed nothing is never silent (home #1503).
"""

from __future__ import annotations

import base64
import json
import os
import posixpath
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

OPT_OUT_LABEL = "no-auto-merge"
CONFIG_PATH = ".github/auto-merge.yml"
MARKER = "<!-- auto-merge-decision -->"
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
# The app folder lives under src/, so it is carved out of the "src/" deny only for the repos in APP_PREFIXES.
DENY_FILES = {"claude.md", "agents.md", "principles.md"}

# Repo-specific route (Rosie, 25 Oct 2026): new views and features for the Epoch app. Epoch has one user and
# the app is versioned side by side (/v1, /v2), so a bad one is reverted, not recovered. Same deny list applies
# first (it blocks workflows, migrations, scripts, tests and decisions), plus the names below.
APP_PREFIXES = {"mangrovefutures/epoch": ("src/epoch/apps/",)}
APP_DENY_PARTS = ("secret", "token", "credential", "permission", "password", "migration")
APP_DENY_EXTENSIONS = (".sql", ".pem", ".key", ".p12", ".env", ".db", ".sqlite")

MANIFESTS = {"pyproject.toml", "uv.lock", "poetry.lock", "pipfile.lock", "package.json", "package-lock.json"}
MANIFEST_RE = re.compile(r"^requirements[\w.-]*\.txt$")


@dataclass
class Decision:
    merge: bool
    kind: str
    reason: str


class ConfigError(Exception):
    """The repo's .github/auto-merge.yml could not be understood."""


@dataclass(frozen=True)
class Config:
    prefixes: tuple = ()   # extra document folders, lower-case, each ending in "/"
    files: tuple = ()      # extra exact document paths, lower-case


def _bad_parts(parts) -> bool:
    return any(p in ("", ".", "..") for p in parts)


def parse_config(text: str) -> Config:
    """Parse the tiny config format (a YAML subset, standard library only).

        doc_prefixes:        # folders whose documents may auto-merge, each ending in "/"
          - lanes/
        doc_files:           # single files, exact path
          - NOTEBOOK.md

    Raises ConfigError on anything else. It only ADDS to the default list; decide() still applies the deny
    list and the document file types first.
    """
    lists = {"doc_prefixes": [], "doc_files": []}
    key = None
    for n, raw in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not raw[0].isspace():
            name = line.strip()
            if not name.endswith(":") or name[:-1] not in lists:
                raise ConfigError(f"line {n}: expected `doc_prefixes:` or `doc_files:`, got `{name}`")
            key = name[:-1]
            continue
        item = line.strip()
        if key is None or not item.startswith("- "):
            raise ConfigError(f"line {n}: expected a `- value` list item under a key")
        value = item[2:].strip().strip("'\"").strip().lower()
        odd = "*" in value or "\\" in value
        if key == "doc_prefixes":
            if not value.endswith("/") or odd or _bad_parts(value[:-1].split("/")):
                raise ConfigError(f"line {n}: `{value}` is not a folder path ending in `/` (no `..`, no `*`)")
        elif odd or _bad_parts(value.split("/")):
            raise ConfigError(f"line {n}: `{value}` is not a file path")
        lists[key].append(value)
    return Config(tuple(lists["doc_prefixes"]), tuple(lists["doc_files"]))


def _doc_path_problem(path: str, config: Config | None = None) -> str | None:
    """None if `path` is a document path, else why not."""
    if not path or path.startswith("/") or ".." in path.split("/") or "//" in path:
        return f"{path}: odd path"
    p = posixpath.normpath(path).lower()
    if p in DENY_FILES or p.startswith(DENY_PREFIXES):
        return f"{path}: on the deny list (code, workflow, settings, migration, decision or rule file)"
    if not p.endswith(DOC_EXTENSIONS):
        return f"{path}: not a document file type"
    extra_prefixes = config.prefixes if config else ()
    extra_files = config.files if config else ()
    if p.startswith(DOC_PREFIXES + extra_prefixes) or p in DOC_FILES or p in extra_files or AGENT_DOC.match(p):
        return None
    return f"{path}: not under a document path (the defaults plus this repo's {CONFIG_PATH}, if it has one)"


def _app_path_problem(path: str, prefixes: tuple[str, ...]) -> str | None:
    """None if `path` is an app view/feature file in this repo's app folder, else why not."""
    if not path or path.startswith("/") or ".." in path.split("/") or "//" in path:
        return f"{path}: odd path"
    p = posixpath.normpath(path).lower()
    if p != path.lower() or not p.startswith(prefixes):
        return f"{path}: not under the app folder"
    if any(seg.startswith(".") for seg in p.split("/")):
        return f"{path}: hidden file or folder"
    name = posixpath.basename(p)
    if p.endswith(APP_DENY_EXTENSIONS) or any(w in name for w in APP_DENY_PARTS):
        return f"{path}: secret-, permission- or database-like name"
    return None


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


def decide(pr: dict, files: list[dict], updates, rules, config=None) -> Decision:
    """`config` is a Config, None (defaults only), or a ConfigError (refuse: the repo's file is unusable)."""
    no = lambda why: Decision(False, "", why)  # noqa: E731
    if isinstance(config, ConfigError):
        return no(f"{CONFIG_PATH} could not be used ({config}); fix it or the pull request waits for a person")
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

    app_prefixes = APP_PREFIXES.get((base or "").lower(), ())
    uses_app = False
    for path in paths:
        problem = _doc_path_problem(path, config)
        if problem and app_prefixes and path.lower().startswith(app_prefixes):
            problem = _app_path_problem(path, app_prefixes)
            uses_app = uses_app or problem is None
        if problem:
            return no(problem)
    if uses_app:
        return Decision(True, "app-views", f"{len(files)} file(s), app views and documents only; versions kept, revert if wrong")
    return Decision(True, "documents", f"{len(files)} file(s), all document paths")


def comment_body(decision: Decision) -> str | None:
    """The pull request comment for a decision not to merge; None when it will merge."""
    if decision.merge:
        return None
    return (f"{MARKER}\n**Auto-merge not enabled.** {decision.reason}\n\n"
            "Document paths per repo: `.github/auto-merge.yml` (`handbook/reference/auto-merge.md`). "
            "Code, workflows, migrations and rule files never auto-merge. This comment is updated in place.")


def resolved_body(decision: Decision) -> str:
    return f"{MARKER}\n**Auto-merge enabled** ({decision.kind}): {decision.reason}"


def find_comment(comments) -> int | None:
    """Id of our earlier decision comment, so it is edited, never duplicated or mistaken for another bot's."""
    for c in comments or []:
        if MARKER in (c.get("body") or ""):
            return c.get("id")
    return None


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


def _send(method: str, url: str, token: str, data: dict):
    req = urllib.request.Request(url, data=json.dumps(data).encode(), method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_config(api: str, repo: str, ref: str, token: str):
    """Config from the BASE branch (never the pull request's own version). None if the repo has none."""
    try:
        blob = _get(f"{api}/repos/{repo}/contents/{CONFIG_PATH}?ref={ref}", token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    try:
        return parse_config(base64.b64decode(blob["content"]).decode("utf-8"))
    except ConfigError as exc:
        return exc
    except (ValueError, KeyError) as exc:
        return ConfigError(f"unreadable: {exc}")


def post_decision_comment(api: str, repo: str, number: int, pr: dict, decision: Decision, token: str) -> None:
    """Say why in a comment. Never fails the job: the summary and the log carry the reason too."""
    if pr.get("draft"):
        return
    try:
        existing = find_comment(_get(f"{api}/repos/{repo}/issues/{number}/comments?per_page=100", token))
        body = comment_body(decision) or (resolved_body(decision) if existing else None)
        if body is None:
            return
        if existing:
            _send("PATCH", f"{api}/repos/{repo}/issues/comments/{existing}", token, {"body": body})
        else:
            _send("POST", f"{api}/repos/{repo}/issues/{number}/comments", token, {"body": body})
    except Exception as exc:  # noqa: BLE001
        print(f"could not write the decision comment: {type(exc).__name__}: {exc}")


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
        config = fetch_config(api, repo, pr["base"]["ref"], token)
        decision = decide(pr, files, updates, rules, config)
    except Exception as exc:  # fail closed: a read error never enables auto-merge
        decision = Decision(False, "", f"could not read the pull request: {type(exc).__name__}: {exc}")
    disable = should_disable(pr, decision)
    out = os.environ.get("GITHUB_OUTPUT")
    reason = decision.reason.replace("\n", " ")
    _write(out, f"merge={'true' if decision.merge else 'false'}\nkind={decision.kind}\n"
                f"disable={'true' if disable else 'false'}\nnumber={number}\nreason={reason}\n")
    _write(os.environ.get("GITHUB_STEP_SUMMARY"),
           f"### Auto-merge: {'ON' if decision.merge else 'not enabled'}\n\n{reason}\n")
    if pr:
        post_decision_comment(api, repo, number, pr, decision, token)
    print(f"merge={decision.merge} kind={decision.kind} disable={disable}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
