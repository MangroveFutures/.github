#!/usr/bin/env python3
"""Read and check the `next:` line on a work item (Rosie, 19 Nov 2026; home#1518; handbook/reference/next-field.md).

Every open work item says what happens next, as exactly one of four kinds, on one line of its body:

    next: now                                         the owner acts on the next dispatcher run
    next: scheduled 2026-11-24 because <reason>       the owner acts on or after that date; the reason is required
    next: trigger <event> <argument>                  the owner acts when the named event has happened
    next: blocked <owner/repo#N or URL>               waits on that issue or PR; the owner acts when it closes

Trigger events (a closed list, each checkable from GitHub without judgement):
    issue-closed <ref>    the referenced issue is closed
    pr-merged <ref>       the referenced pull request is merged
    label <name>          this item carries that label
    rosie-replied         the newest comment is from Rosie (fails the agent-footer test of the agent-sweep skill)

A bare timer ("within 3 days", "untouched for N days") is not a kind: it names no event that anyone acts on.
The owner is the item's first `agent:<name>` label. Whether that owner can be woken is the dispatcher's
config (handbook/reference/dispatch.yaml), not this module.

Standard library only. Pure functions; no network.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

KINDS = ("now", "scheduled", "trigger", "blocked")
TRIGGERS = ("issue-closed", "pr-merged", "label", "rosie-replied")
LINE = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*)?next:(?:\*\*)?\s*(.*?)\s*$", re.MULTILINE)  # lower case only: prose "Next:" is not the field
SECTION = re.compile(r"^ {0,3}#{1,3}\s+Next\s*\n+\s*([^\n#][^\n]*)", re.MULTILINE)
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})\b")
REF = re.compile(r"^(?:https://github\.com/)?([\w.-]+/[\w.-]+)?(?:#|/(?:issues|pull)/)(\d+)$")


@dataclass
class Next:
    kind: str | None          # one of KINDS, or None when missing or invalid
    raw: str = ""             # the text after `next:`
    error: str = ""           # why it is invalid; empty when valid
    date: dt.date | None = None
    reason: str = ""
    event: str = ""           # trigger event name
    arg: str = ""             # trigger argument, or the blocker reference
    repo: str = ""            # resolved owner/repo of a reference
    number: int = 0           # resolved number of a reference

    @property
    def ok(self) -> bool:
        return self.kind is not None and not self.error


def _strip_code(body: str) -> str:
    out, fenced = [], False
    for line in COMMENT.sub("", body or "").splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            out.append(line)
    return "\n".join(out)


def resolve_ref(text: str, default_repo: str) -> tuple[str, int] | None:
    """`#12`, `home#12`, `MangroveFutures/home#12` or a GitHub URL -> (owner/repo, 12)."""
    t = text.strip().rstrip(".,;)")
    m = re.match(r"^([\w.-]+)#(\d+)$", t)          # short form: home#12
    if m and "/" not in m.group(1):
        owner = default_repo.split("/")[0]
        return f"{owner}/{m.group(1)}", int(m.group(2))
    m = REF.match(t)
    if not m:
        return None
    return (m.group(1) or default_repo), int(m.group(2))


def parse(body: str, default_repo: str = "MangroveFutures/home") -> Next:
    """Find the one `next:` line and check it. More than one line is an error: an item has one next step."""
    text = _strip_code(body)
    lines = LINE.findall(text)
    if not lines:  # an issue made from the form has a "### Next" section instead of a `next:` line
        m = SECTION.search(text)
        value = m.group(1).strip() if m else ""
        if value and value.casefold() != "_no response_":
            lines = [value]
    if not lines:
        return Next(None, error="missing: no `next:` line")
    if len(lines) > 1:
        return Next(None, raw=lines[0], error=f"{len(lines)} `next:` lines; an item has exactly one")
    raw = lines[0]
    word, _, rest = raw.partition(" ")
    word, rest = word.lower().rstrip(":"), rest.strip()
    if word not in KINDS:
        return Next(None, raw=raw, error=f"unknown kind {word!r}; use one of {', '.join(KINDS)}")
    if word == "now":
        return Next("now", raw=raw, reason=rest)
    if word == "scheduled":
        m = DATE.match(rest)
        if not m:
            return Next(None, raw=raw, error="scheduled needs a date as YYYY-MM-DD")
        try:
            date = dt.date.fromisoformat(m.group(1))
        except ValueError:
            return Next(None, raw=raw, error=f"not a real date: {m.group(1)}")
        reason = re.sub(r"(?i)^\s*(because|reason:|—|-|:)\s*", "", rest[m.end():]).strip()
        if len(reason.split()) < 3:
            return Next(None, raw=raw, error="scheduled needs a reason after the date (why that date)")
        return Next("scheduled", raw=raw, date=date, reason=reason)
    if word == "trigger":
        event, _, arg = rest.partition(" ")
        event, arg = event.lower(), arg.strip()
        if event not in TRIGGERS:
            return Next(None, raw=raw, error=f"unknown trigger {event!r}; use one of {', '.join(TRIGGERS)}")
        n = Next("trigger", raw=raw, event=event, arg=arg)
        if event in ("issue-closed", "pr-merged"):
            ref = resolve_ref(arg, default_repo)
            if not ref:
                return Next(None, raw=raw, error=f"{event} needs an issue or PR reference such as home#12")
            n.repo, n.number = ref
        elif event == "label" and not arg:
            return Next(None, raw=raw, error="label needs a label name")
        return n
    # blocked
    ref = resolve_ref(rest.split()[0] if rest else "", default_repo)
    if not ref:
        return Next(None, raw=raw, error="blocked needs a link to the blocking issue or PR, such as home#12")
    return Next("blocked", raw=raw, arg=rest.split()[0], repo=ref[0], number=ref[1])


def owner(labels: list[str]) -> str | None:
    for name in labels:
        if name.lower().startswith("agent:"):
            return name.split(":", 1)[1].strip().lower() or None
    return None


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys
    ap = argparse.ArgumentParser(description="Parse the next: line of an issue body.")
    ap.add_argument("--body-file", default="-")
    ap.add_argument("--repo", default="MangroveFutures/home")
    a = ap.parse_args(argv)
    body = sys.stdin.read() if a.body_file == "-" else open(a.body_file, encoding="utf-8").read()
    n = parse(body, a.repo)
    d = {k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in n.__dict__.items()}
    print(json.dumps(d, indent=2))
    return 0 if n.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
