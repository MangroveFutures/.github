#!/usr/bin/env python3
"""Claude Code PreToolUse hook: stop an agent creating an issue that fails the ORG-0006 standard.

The agent-side gate. The GitHub Action (issue-standard.yml) is the backstop that sees every issue
however it was made; this hook catches an agent's issue before it exists, and tells the agent what
to add. It reads the same spec through the same checker (scripts/check_issue.py).

What it inspects (Claude Code passes the pending tool call as JSON on stdin, per
https://code.claude.com/docs/en/hooks):

- Bash: `gh issue create`, `gh issue edit --add-label ready`, `gh api` POSTs to
  `repos/OWNER/REPO/issues` (and to `.../issues/N/labels` adding `ready`), and `curl` POSTs to the
  same REST path.
- MCP: any server's `issue_write` tool (tool_name `mcp__<server>__issue_write`) with
  method `create`, or method `update` adding the `ready` label.

What it allows without checking: an issue labelled `needs-grooming` or `spike` (capture stays free,
ORG-0006; a `needs-grooming` capture missing Source, origin with a date, or a Parent gets a warning
in `additionalContext`, never a block), and `gh issue create --web` (the browser shows the form).

How it answers: nothing (exit 0) to allow; a JSON `permissionDecision: "deny"` with the reason to
block; `"ask"` when it cannot see the body (for example an edit adding `ready` when the issue
cannot be fetched), so a person decides.

When the checker cannot load (PyYAML missing, the forms missing or unreadable): it fails closed
ONLY for the calls it gates, an issue create or an edit that adds `ready`. Everything else that
touches issues (`gh issue list`/`view`/`comment`/`close`, `gh api` GETs, MCP updates that do not add
`ready`) goes through, because nothing about them needs the checker.

Known limits, said plainly: it reads the command text, not a real shell. A body built at run time
(`--body "$VAR"`, a script that calls the API itself, `bash -c '...'`) is invisible to it; where
it can tell it cannot see the body it denies with the way out (write the body to a file and use
`--body-file`). Anything it cannot see is still checked by the Action after creation.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_check  # noqa: E402  (standard library only, so capture advice works without PyYAML)

IMPORT_ERROR: Exception | None = None
try:
    import check_issue  # noqa: E402  (needs PyYAML; if missing, only gated calls are denied)
except ImportError as _import_error:  # pragma: no cover - exercised only without PyYAML
    check_issue = None
    IMPORT_ERROR = _import_error

CAPTURE_LABELS = {"needs-grooming", "spike"}
READY = "ready"
ISSUE_ISH = re.compile(r"\bgh\b[\s\S]*\b(issue|api)\b|api\.github\.com")
SKILL = (
    "the work-item-writing skill (.claude/skills/work-item-writing/SKILL.md; source "
    "MangroveFutures/home), which has a template for each type"
)
ISSUES_PATH = re.compile(r"^/?repos/([^/\s]+)/([^/\s]+)/issues/?$")
LABELS_PATH = re.compile(r"^/?repos/([^/\s]+)/([^/\s]+)/issues/(\d+)/labels/?$")
API_ISSUES_URL = re.compile(r"api\.github\.com/repos/[^/\s]+/[^/\s]+/issues/?(?:[\s'\"?]|$)")
SUBST_HEREDOC = re.compile(
    r"""(["']?)\$\(\s*cat\s+<<-?\s*(['"]?)(\w+)\2[^\n]*\n(.*?)\n[ \t]*\3[ \t]*\n?\s*\)\1""",
    re.DOTALL,
)
PLAIN_HEREDOC = re.compile(r"""<<-?\s*(['"]?)(\w+)\1[^\n]*\n(.*?)\n[ \t]*\2[ \t]*(?=\n|$)""", re.DOTALL)


class Unseen(Exception):
    """The hook cannot see what the issue body will be."""


class CheckerUnavailable(Exception):
    """A gated call (create, or adding `ready`) reached the check, but the checker did not load."""


def require_checker(forms) -> None:
    if check_issue is None or forms is None:
        raise CheckerUnavailable()


# --- decisions -------------------------------------------------------------------------------


def deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def ask(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                   "permissionDecisionReason": reason}}


def advise(text: str) -> dict:
    """Advice added to an allowed call. No permissionDecision, so the normal permission flow runs."""
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}}


def capture_advice(body: str, names: set[str]) -> dict | None:
    """Shadow mode (like ORG-0006's graduation): tell an agent what its `needs-grooming` capture is
    missing, never stop it. A `spike` is discovery work, not a capture, so it gets no advice."""
    if "needs-grooming" not in names:
        return None
    gaps = capture_check.capture_gaps(body)
    return advise(capture_check.warning_text(gaps)) if gaps else None


def verdict_for(body: str, labels: list[str], issue_type: str | None, forms, *, promoting: bool = False) -> dict | None:
    names = {label.casefold() for label in labels}
    if not promoting and names & CAPTURE_LABELS and READY not in names:
        return capture_advice(body, names)  # capture: always allowed; at most advice
    require_checker(forms)
    result = check_issue.check(body, labels, issue_type, forms)
    if result.ok:
        return None
    what = "label this issue `ready`" if promoting else "create this issue"
    lines = [f"Blocked by the ORG-0006 issue standard: I will not {what} because it does not meet the "
             f"{result.form} standard ({result.how_found}). Missing or empty:"]
    lines += [f"- {p.text()}" for p in result.problems]
    lines.append(f"Fix: add those `### ` sections to the body. See {SKILL}.")
    if not promoting:
        lines.append("If this is only a capture (an idea, not ready work), add the `needs-grooming` "
                     "label instead and it will go through.")
    return deny("\n".join(lines))


def cannot_see(why: str) -> dict:
    return deny(
        f"Blocked by the ORG-0006 issue standard hook: {why} so I cannot check the body. Write the "
        "body to a file and use `gh issue create --body-file FILE` (or pass the body literally), "
        f"then try again. See {SKILL}."
    )


# --- fetching an existing issue (for edits that add `ready`) -------------------------------------


def fetch_issue(repo: str | None, number: str, cwd: str | None) -> dict:
    repo = repo or repo_from_git(cwd)
    if not repo:
        raise Unseen("cannot tell which repository the issue is in")
    try:
        out = subprocess.run(["gh", "api", f"repos/{repo}/issues/{number}"], capture_output=True,
                             text=True, timeout=20, check=True).stdout
        return json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise Unseen(f"could not fetch {repo}#{number} ({exc.__class__.__name__})") from exc


def repo_from_git(cwd: str | None) -> str | None:
    try:
        url = subprocess.run(["git", "-C", cwd or ".", "remote", "get-url", "origin"], capture_output=True,
                             text=True, timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?/?$", url)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def promote_check(repo: str | None, number: str, cwd: str | None, forms, body: str | None = None,
                  extra_labels: list[str] | None = None) -> dict | None:
    require_checker(forms)  # before any network call: without the checker the answer is deny anyway
    try:
        issue = fetch_issue(repo, number, cwd)
    except Unseen as exc:
        if body is None:
            return ask(f"This adds the `ready` label to #{number}, but {exc}, so the ORG-0006 hook cannot "
                       "check it. Adding `ready` says the Definition of Ready is met (RFC-0005 §3); confirm it is, and that any item needing Rosie's yes has `approved`.")
        issue = {}
    labels = [l["name"] for l in issue.get("labels", [])] + list(extra_labels or []) + [READY]
    issue_type = (issue.get("type") or {}).get("name")
    return verdict_for(body if body is not None else issue.get("body") or "", labels, issue_type, forms,
                       promoting=True)


# --- MCP ---------------------------------------------------------------------------------------


def from_mcp(tool_input: dict, cwd: str | None, forms) -> dict | None:
    method = str(tool_input.get("method", "")).lower()
    labels = [str(l) for l in tool_input.get("labels") or []]
    issue_type = tool_input.get("type")
    if method == "create":
        return verdict_for(str(tool_input.get("body") or ""), labels, issue_type, forms)
    if method == "update" and READY in {l.casefold() for l in labels}:
        repo = f"{tool_input.get('owner')}/{tool_input.get('repo')}" if tool_input.get("owner") else None
        return promote_check(repo, str(tool_input.get("issue_number")), cwd, forms,
                             body=tool_input.get("body"), extra_labels=labels)
    return None


# --- Bash --------------------------------------------------------------------------------------


def split_command(command: str) -> tuple[list[list[str]], dict[str, str], list[str]]:
    """Return (segments, heredoc placeholders, stdin heredocs). Raises ValueError on bad quoting."""
    placeholders: dict[str, str] = {}

    def keep(match: re.Match) -> str:
        key = f"__HEREDOC_{len(placeholders)}__"
        placeholders[key] = match.group(4)
        return key

    text = SUBST_HEREDOC.sub(keep, command)
    stdin_docs = [m.group(3) for m in PLAIN_HEREDOC.finditer(text)]
    text = PLAIN_HEREDOC.sub(" ", text)
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    lexer.commenters = ""
    segments: list[list[str]] = [[]]
    for token in lexer:
        if token and set(token) <= set(";&|"):
            segments.append([])
        else:
            segments[-1].append(token)
    cleaned = []
    for seg in segments:
        while seg and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[0]):
            seg = seg[1:]  # leading VAR=value assignments
        if seg:
            cleaned.append(seg)
    return cleaned, placeholders, stdin_docs


def options(args: list[str], spec: dict[str, str]) -> tuple[dict[str, list[str]], list[str]]:
    """Tiny flag parser: spec maps every spelling to a canonical name ('' value = boolean flag)."""
    found: dict[str, list[str]] = {}
    positional: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        name, _, inline = arg.partition("=") if arg.startswith("--") else (arg, "", "")
        if name in spec:
            canon, takes_value = spec[name].rstrip(":"), spec[name].endswith(":")
            if takes_value:
                if inline:
                    value = inline
                else:
                    i += 1
                    value = args[i] if i < len(args) else ""
                found.setdefault(canon, []).append(value)
            else:
                found.setdefault(canon, []).append("true")
        else:
            positional.append(arg)
        i += 1
    return found, positional


RUNTIME = re.compile(r"\$[A-Za-z_({]|`")


def resolve(value: str, placeholders: dict[str, str]) -> str:
    """Substitute heredoc placeholders. A variable or command substitution means we cannot see it."""
    if RUNTIME.search(value):
        raise Unseen("the body is built when the command runs (it uses a variable or a substitution)")
    for key, content in placeholders.items():
        value = value.replace(key, content)
    return value


def read_file(path: str, cwd: str | None, stdin_docs: list[str]) -> str:
    if path == "-":
        if stdin_docs:
            return stdin_docs[0]
        raise Unseen("the body comes from standard input")
    full = Path(path) if Path(path).is_absolute() else Path(cwd or ".") / path
    try:
        return full.read_text(encoding="utf-8")
    except OSError as exc:
        raise Unseen(f"the body file {path} cannot be read ({exc.strerror})") from exc


def split_labels(values: list[str]) -> list[str]:
    return [part.strip() for value in values for part in value.split(",") if part.strip()]


GH_CREATE = {"-t": "title:", "--title": "title:", "-b": "body:", "--body": "body:", "-F": "file:",
             "--body-file": "file:", "-l": "label:", "--label": "label:", "-R": "repo:", "--repo": "repo:",
             "-w": "web", "--web": "web", "-T": "template:", "--template": "template:", "--type": "type:",
             "-a": "x:", "--assignee": "x:", "-m": "x:", "--milestone": "x:", "-p": "x:", "--project": "x:",
             "--recover": "x:", "-e": "editor", "--editor": "editor"}
GH_EDIT = {"--add-label": "add:", "-b": "body:", "--body": "body:", "-F": "file:", "--body-file": "file:",
           "-R": "repo:", "--repo": "repo:", "-t": "x:", "--title": "x:", "--remove-label": "x:",
           "--add-assignee": "x:", "--remove-assignee": "x:", "--add-project": "x:",
           "--remove-project": "x:", "-m": "x:", "--milestone": "x:", "--type": "x:"}
GH_API = {"-X": "method:", "--method": "method:", "-f": "raw:", "--raw-field": "raw:", "-F": "field:",
          "--field": "field:", "--input": "input:", "-H": "x:", "--header": "x:", "-q": "x:", "--jq": "x:",
          "-t": "x:", "--template": "x:", "--hostname": "x:", "-p": "x:", "--preview": "x:",
          "--paginate": "flag", "--silent": "flag", "-i": "flag", "--include": "flag", "--verbose": "flag",
          "--cache": "x:", "--slurp": "flag"}


def gh_issue_create(args, cwd, placeholders, stdin_docs, forms):
    opts, _ = options(args, GH_CREATE)
    if "web" in opts or "editor" in opts:
        return None  # a person fills the form in the browser or editor; the Action checks it after
    labels = split_labels(opts.get("label", []))
    if "body" in opts:
        body = resolve(opts["body"][-1], placeholders)
    elif "file" in opts:
        body = read_file(resolve(opts["file"][-1], placeholders), cwd, stdin_docs)
    elif {l.casefold() for l in labels} & CAPTURE_LABELS:
        return capture_advice("", {l.casefold() for l in labels})
    else:
        raise Unseen("no --body or --body-file was given")
    issue_type = opts.get("type", [None])[-1]
    return verdict_for(body, labels, issue_type, forms)


def gh_issue_edit(args, cwd, placeholders, stdin_docs, forms):
    opts, positional = options(args, GH_EDIT)
    added = split_labels(opts.get("add", []))
    if READY not in {l.casefold() for l in added}:
        return None
    body = None
    if "body" in opts:
        body = resolve(opts["body"][-1], placeholders)
    elif "file" in opts:
        body = read_file(resolve(opts["file"][-1], placeholders), cwd, stdin_docs)
    repo = opts.get("repo", [None])[-1]
    verdicts = []
    for ref in positional or [""]:
        match = re.search(r"(?:github\.com/([^/]+/[^/]+)/issues/)?#?(\d+)$", ref)
        if not match:
            return ask("This adds the `ready` label, but the ORG-0006 hook cannot tell which issue.")
        verdicts.append(promote_check(match.group(1) or repo, match.group(2), cwd, forms, body=body,
                                      extra_labels=added))
    return next((v for v in verdicts if v), None)


def api_fields(opts, placeholders, cwd, stdin_docs) -> dict:
    data: dict = {}
    if "input" in opts:
        raw = read_file(resolve(opts["input"][-1], placeholders), cwd, stdin_docs)
        try:
            loaded = json.loads(raw)
        except ValueError as exc:
            raise Unseen("the --input file is not JSON") from exc
        data.update(loaded if isinstance(loaded, dict) else {})
    for kind in ("raw", "field"):
        for item in opts.get(kind, []):
            key, _, value = item.partition("=")
            value = resolve(value, placeholders)
            if kind == "field" and value.startswith("@"):
                value = read_file(value[1:], cwd, stdin_docs)
            if key.endswith("[]"):
                data.setdefault(key[:-2], []).append(value)
            else:
                data[key] = value
    return data


def gh_api(args, cwd, placeholders, stdin_docs, forms):
    opts, positional = options(args, GH_API)
    endpoint = next((p for p in positional if "repos/" in p), "")
    has_fields = any(k in opts for k in ("raw", "field", "input"))
    method = opts.get("method", ["POST" if has_fields else "GET"])[-1].upper()
    if method != "POST":
        return None
    if ISSUES_PATH.match(endpoint):
        owner, repo = ISSUES_PATH.match(endpoint).groups()
        data = api_fields(opts, placeholders, cwd, stdin_docs)
        labels = [str(l) for l in data.get("labels", [])] if isinstance(data.get("labels"), list) else []
        return verdict_for(str(data.get("body") or ""), labels, data.get("type"), forms)
    if LABELS_PATH.match(endpoint):
        owner, repo, number = LABELS_PATH.match(endpoint).groups()
        data = api_fields(opts, placeholders, cwd, stdin_docs)
        labels = data.get("labels", [])
        labels = labels if isinstance(labels, list) else [labels]
        if READY in {str(l).casefold() for l in labels}:
            return promote_check(f"{owner}/{repo}", number, cwd, forms)
    return None


def curl_post(command: str, segment: list[str], placeholders, forms):
    joined = " ".join(segment)
    if not API_ISSUES_URL.search(joined + " "):
        return None
    opts, _ = options(segment[1:], {"-d": "data:", "--data": "data:", "--data-raw": "data:",
                                    "--data-binary": "data:", "-X": "method:", "--request": "method:"})
    if "data" not in opts:
        return None
    raw = resolve(opts["data"][-1], placeholders)
    if raw.startswith("@"):
        raise Unseen("curl reads the body from a file")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise Unseen("the curl data is not literal JSON") from exc
    labels = [str(l) for l in data.get("labels", [])] if isinstance(data.get("labels"), list) else []
    return verdict_for(str(data.get("body") or ""), labels, data.get("type"), forms)


def from_bash(command: str, cwd: str | None, forms) -> dict | None:
    if not ISSUE_ISH.search(command):
        return None  # fast path: nothing about issues here
    try:
        segments, placeholders, stdin_docs = split_command(command)
    except ValueError:
        if re.search(r"gh\s+issue\s+create|gh\s+api\b[^\n]*issues", command):
            return cannot_see("the command's quoting could not be read")
        return None
    for seg in segments:
        try:
            if seg[:3] == ["gh", "issue", "create"]:
                verdict = gh_issue_create(seg[3:], cwd, placeholders, stdin_docs, forms)
            elif seg[:3] == ["gh", "issue", "edit"]:
                verdict = gh_issue_edit(seg[3:], cwd, placeholders, stdin_docs, forms)
            elif seg[:2] == ["gh", "api"]:
                verdict = gh_api(seg[2:], cwd, placeholders, stdin_docs, forms)
            elif seg[0] == "curl":
                verdict = curl_post(command, seg, placeholders, forms)
            else:
                verdict = None
        except Unseen as exc:
            return cannot_see(str(exc))
        if verdict:
            return verdict
    return None


# --- entry point -------------------------------------------------------------------------------


def decide(event: dict, forms) -> dict | None:
    tool = str(event.get("tool_name", ""))
    tool_input = event.get("tool_input") or {}
    cwd = event.get("cwd")
    if tool == "Bash":
        return from_bash(str(tool_input.get("command", "")), cwd, forms)
    if re.fullmatch(r"mcp__.+__issue_write", tool):
        return from_mcp(tool_input, cwd, forms)
    return None


def relevant(event: dict) -> bool:
    """Cheap test, before any parsing or YAML loading: could this call touch an issue?"""
    tool = str(event.get("tool_name", ""))
    if tool == "Bash":
        return bool(ISSUE_ISH.search(str((event.get("tool_input") or {}).get("command", ""))))
    return bool(re.fullmatch(r"mcp__.+__issue_write", tool))



GH_ISSUE_GATED = re.compile(r"\bgh\s+issue\s+(?:create\b|edit\b.*--add-label\b.*\bready\b)")
API_GATED_PATH = re.compile(r"repos/[^/\s]+/[^/\s]+/issues(?:/\d+/labels)?/?(?=[\s'\"?]|$)")
API_WRITES = re.compile(
    r"(?:^|\s)(?:-X|--method|--request)[\s=]*['\"]?(?:POST|PUT)\b"
    r"|(?:^|\s)(?:-f|-F|--field|--raw-field|--input|-d|--data|--data-raw|--data-binary)(?=[\s=]|$)",
    re.IGNORECASE,
)


def looks_gated(event: dict) -> bool:
    """Conservative text test, used only when the hook itself crashed: could this call create an
    issue or add `ready`? Reads (list, view, GETs), comments and closes answer False."""
    tool = str(event.get("tool_name", ""))
    tool_input = event.get("tool_input") or {}
    if tool == "Bash":
        for segment in re.split(r"[;&|\n]+", str(tool_input.get("command", ""))):
            if GH_ISSUE_GATED.search(segment):
                return True
            if re.search(r"\b(?:gh\s+api|curl)\b", segment) and API_GATED_PATH.search(segment) \
                    and API_WRITES.search(segment):
                return True
        return False
    if re.fullmatch(r"mcp__.+__issue_write", tool):
        method = str(tool_input.get("method", "")).lower()
        labels = {str(l).casefold() for l in tool_input.get("labels") or []}
        return method == "create" or (method == "update" and READY in labels)
    return False


def unavailable(why: str) -> dict:
    return deny(
        f"The ORG-0006 issue standard checker is not available ({why}), so this hook cannot check an "
        "issue being created or labelled `ready`, and blocks only those. Reads, comments and closes "
        "still go through. Fix: install PyYAML (`python3 -m pip install pyyaml`) and check the body with "
        "`python3 scripts/check_issue.py --body-file FILE`; or create it as a capture labelled "
        f"`needs-grooming`. See {SKILL}."
    )


def load_forms_or_none() -> tuple[object | None, str]:
    if check_issue is None:
        return None, f"{IMPORT_ERROR.__class__.__name__}: {IMPORT_ERROR}" if IMPORT_ERROR else "not importable"
    try:
        return check_issue.load_forms(check_issue.default_forms_dir()), ""
    except Exception as exc:  # noqa: BLE001 - missing or unreadable forms
        return None, f"{exc.__class__.__name__}: {exc}"


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    if not relevant(event):
        return 0
    forms, why = load_forms_or_none()
    try:
        verdict = decide(event, forms)
    except CheckerUnavailable:
        verdict = unavailable(why)
    except Exception as exc:  # noqa: BLE001 - a crash must not silently let an issue through
        if looks_gated(event):
            verdict = deny(f"The ORG-0006 issue standard hook failed ({exc.__class__.__name__}: {exc}), so "
                           "it cannot confirm this issue meets the standard. Check the body with "
                           "`python3 scripts/check_issue.py --body-file FILE` and report the hook failure.")
        else:
            verdict = None  # a read, comment or close: nothing to check, so a hook bug must not block it
    if verdict:
        print(json.dumps(verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
