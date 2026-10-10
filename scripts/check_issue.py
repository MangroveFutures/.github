#!/usr/bin/env python3
"""Check an issue body against the ORG-0006 work-item standard.

The spec is the issue forms in ``org-dotgithub/ISSUE_TEMPLATE/*.yml`` (in MangroveFutures/home) or
``ISSUE_TEMPLATE/*.yml`` (in the synced MangroveFutures/.github). Nothing here restates the
standard: each form field's ``label`` is the ``### `` heading the checker looks for, and
``validations.required`` says whether it must be filled. GitHub renders a form-created issue in
exactly that shape (``### <label>`` then the answer, or ``_No response_``), and agents write the
same headings, so one checker reads both.

Three rules cannot be written in GitHub's form schema, so they live here, and only here:

1. "Context and links" must contain at least one link (a URL, a Markdown link, or a GitHub
   reference such as ``#12`` or ``owner/repo#12``), per home ``principles.md`` §1.
2. Task: "Decision records this relies on" is required when "Where to look" touches
   ``migrations/``, ``models.py`` or a dependency file (epoch RFC-0005 §4), unless the body already
   cites a decision record (``ADR-0038``, ``ORG-0006``, ``RFC-0005``) somewhere else.
3. Bet: "Predictions and measures" is required when the issue is labelled ``pdsa`` (ORG-0006).

Which form applies, in order: a ``<!-- form: NAME -->`` marker in the body; a label a form adds
(``bug``, ``decision``, ``bet``); the GitHub issue type ``Epic`` means "not checked" (ORG-0006 has no
epic form); the headings present (the form whose own headings appear); the issue type ``Bug``;
otherwise ``task``.

Usage:
    check_issue.py --body-file body.md [--label bug] [--issue-type Task] [--type decision] [--json]
    echo "$BODY" | check_issue.py --body-file -

Exit codes: 0 meets the standard (or is not checked), 1 does not, 2 usage error.
Standard library plus PyYAML only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

NO_RESPONSE = "_no response_"
SKIPPED_ISSUE_TYPES = {"epic"}
ISSUE_TYPE_TO_FORM = {"bug": "bug"}
DEFAULT_FORM = "task"
SKILL_HINT = (
    "How to write it: the work-item-writing skill (MangroveFutures/home "
    ".claude/skills/work-item-writing/SKILL.md)."
)

# Conditional rules (see module docstring). Paths that mean "touches schema or dependencies".
SCHEMA_OR_DEPS = re.compile(
    r"(migrations/|models\.py|pyproject\.toml|requirements[\w.-]*\.txt|package\.json|uv\.lock|"
    r"poetry\.lock|package-lock\.json|pnpm-lock\.yaml)",
    re.IGNORECASE,
)
LINK = re.compile(
    r"(https?://\S+"  # a URL (Markdown links contain one too)
    r"|(?<![\w/])[\w.-]+/[\w.-]+#\d+"  # owner/repo#12
    r"|(?<![\w&/])#\d+\b)",  # #12 (GitHub autolinks it)
)
DECISION_REF = re.compile(r"\b(ADR|ORG|RFC)-?\s?\d{3,4}\b", re.IGNORECASE)
MARKER = re.compile(r"<!--\s*form:\s*([\w-]+)\s*-->", re.IGNORECASE)
HEADING = re.compile(r"^ {0,3}(#{1,3})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
RULE = re.compile(r"^ {0,3}([-*_])( *\1){2,} *$")  # a thematic break, e.g. --- before a footer
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def default_forms_dir() -> Path:
    here = Path(__file__).resolve().parent
    for candidate in (here.parent / "org-dotgithub" / "ISSUE_TEMPLATE", here.parent / "ISSUE_TEMPLATE"):
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "No ISSUE_TEMPLATE directory found next to this script; pass --forms-dir."
    )


@dataclass
class Field:
    id: str
    label: str
    required: bool
    description: str = ""


@dataclass
class Form:
    name: str
    labels: list[str]
    fields: list[Field]

    def field(self, field_id: str) -> Field | None:
        return next((f for f in self.fields if f.id == field_id), None)


@dataclass
class Problem:
    heading: str
    reason: str
    way_out: str

    def text(self) -> str:
        return f"`### {self.heading}`: {self.reason} {self.way_out}"


@dataclass
class Result:
    form: str | None
    how_found: str
    problems: list[Problem] = field(default_factory=list)

    @property
    def checked(self) -> bool:
        return self.form is not None

    @property
    def ok(self) -> bool:
        return not self.problems

    def report(self) -> str:
        if not self.checked:
            return f"Not checked: {self.how_found}."
        if self.ok:
            return f"Meets the {self.form} standard ({self.how_found})."
        lines = [f"Does not meet the {self.form} standard ({self.how_found}). Missing or empty:"]
        lines += [f"- {p.text()}" for p in self.problems]
        lines.append(SKILL_HINT)
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {
            "form": self.form,
            "how_found": self.how_found,
            "checked": self.checked,
            "ok": self.ok,
            "problems": [p.__dict__ for p in self.problems],
        }


def load_forms(forms_dir: Path) -> dict[str, Form]:
    forms: dict[str, Form] = {}
    for path in sorted(forms_dir.glob("*.y*ml")):
        if path.stem == "config":
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        fields = []
        for element in data.get("body", []):
            if element.get("type") == "markdown" or "id" not in element:
                continue
            attrs = element.get("attributes", {})
            fields.append(
                Field(
                    id=element["id"],
                    label=str(attrs.get("label", "")).strip(),
                    required=bool((element.get("validations") or {}).get("required", False)),
                    description=str(attrs.get("description", "")).strip(),
                )
            )
        labels = data.get("labels") or []
        if isinstance(labels, str):
            labels = [s.strip() for s in labels.split(",") if s.strip()]
        forms[path.stem] = Form(name=path.stem, labels=list(labels), fields=fields)
    if not forms:
        raise FileNotFoundError(f"No issue forms found in {forms_dir}")
    return forms


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().rstrip(":").strip().casefold()


def parse_sections(body: str) -> dict[str, str]:
    """Map each level-1 to level-3 heading (normalised) to the text under it.

    A section runs to the next heading of level 1-3, or to a thematic break (``---``), which is how
    agent bodies separate a "Generated by" footer. Headings inside fenced code blocks are text.
    If a heading appears twice, the first one wins (that is the one GitHub's form wrote).
    """
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    fence: str | None = None
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        fence_match = FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
        outside = not (fence or fence_match)
        heading = HEADING.match(line) if outside else None
        if outside and RULE.match(line) and current is not None:
            if current not in sections:
                sections[current] = "\n".join(buffer)
            current, buffer = None, []
            continue
        if heading:
            if current is not None and current not in sections:
                sections[current] = "\n".join(buffer)
            current, buffer = _norm(heading.group(2)), []
        elif current is not None:
            buffer.append(line)
    if current is not None and current not in sections:
        sections[current] = "\n".join(buffer)
    return sections


def is_empty(content: str | None) -> bool:
    if content is None:
        return True
    text = COMMENT.sub("", content).strip()
    return not text or text.casefold() == NO_RESPONSE


def has_link(content: str) -> bool:
    return bool(LINK.search(COMMENT.sub("", content)))


def choose_form(
    body: str,
    labels: list[str],
    issue_type: str | None,
    forms: dict[str, Form],
    sections: dict[str, str],
) -> tuple[str | None, str]:
    marker = MARKER.search(body or "")
    if marker and marker.group(1).lower() in forms:
        return marker.group(1).lower(), f"marker <!-- form: {marker.group(1).lower()} -->"
    lowered = {label.casefold() for label in labels}
    for name, form in forms.items():
        if any(label.casefold() in lowered for label in form.labels):
            return name, f"label `{next(l for l in form.labels if l.casefold() in lowered)}`"
    itype = (issue_type or "").strip().casefold()
    if itype in SKIPPED_ISSUE_TYPES:
        return None, f"issue type {issue_type} is a container; ORG-0006 has no form for it"
    # Headings that belong to exactly one form identify it.
    owners: dict[str, set[str]] = {}
    for name, form in forms.items():
        for f in form.fields:
            owners.setdefault(_norm(f.label), set()).add(name)
    scores = {name: 0 for name in forms}
    for heading in sections:
        if len(owners.get(heading, ())) == 1:
            scores[next(iter(owners[heading]))] += 1
    best = max(scores.values(), default=0)
    leaders = [name for name, score in scores.items() if score == best]
    if best > 0 and len(leaders) == 1:
        return leaders[0], "its headings"
    if itype in ISSUE_TYPE_TO_FORM and ISSUE_TYPE_TO_FORM[itype] in forms:
        return ISSUE_TYPE_TO_FORM[itype], f"issue type {issue_type}"
    return DEFAULT_FORM, "default"


def _way_out(f: Field) -> str:
    hint = f" ({f.description})" if f.description else ""
    return f"Add a `### {f.label}` section with an answer{hint}."


def check(
    body: str,
    labels: list[str] | None = None,
    issue_type: str | None = None,
    forms: dict[str, Form] | None = None,
    form_name: str | None = None,
) -> Result:
    labels = labels or []
    forms = forms if forms is not None else load_forms(default_forms_dir())
    sections = parse_sections(body)
    if form_name:
        if form_name not in forms:
            raise ValueError(f"Unknown form {form_name!r}; known: {', '.join(sorted(forms))}")
        name, how = form_name, "given"
    else:
        name, how = choose_form(body, labels, issue_type, forms, sections)
    result = Result(form=name, how_found=how)
    if name is None:
        return result
    form = forms[name]
    lowered_labels = {label.casefold() for label in labels}

    required_ids = {f.id for f in form.fields if f.required}
    where = form.field("where")
    if name == "task" and form.field("adr") and where:
        touches = SCHEMA_OR_DEPS.search(sections.get(_norm(where.label), "") or "")
        # A decision record cited anywhere in the body satisfies the rule (the point is the citation).
        if touches and not DECISION_REF.search(COMMENT.sub("", body or "")):
            required_ids.add("adr")
    if name == "bet" and form.field("predictions") and "pdsa" in lowered_labels:
        required_ids.add("predictions")

    why_required = {
        "adr": ' Required because "Where to look" touches schema or dependencies (RFC-0005 §4).',
        "predictions": " Required because the issue is labelled `pdsa`.",
    }
    for f in form.fields:
        if f.id not in required_ids:
            continue
        content = sections.get(_norm(f.label))
        because = "" if f.required else why_required.get(f.id, "")
        if content is None:
            result.problems.append(Problem(f.label, "missing." + because, _way_out(f)))
        elif is_empty(content):
            result.problems.append(Problem(f.label, "empty." + because, _way_out(f)))
        elif f.id == "context" and not has_link(content):
            result.problems.append(
                Problem(
                    f.label,
                    "has no link.",
                    "Add at least one link (a URL, or a reference such as #12 or owner/repo#12), "
                    "each with a one-line summary beside it.",
                )
            )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--body-file", required=True, help="File with the issue body, or - for stdin.")
    parser.add_argument("--label", action="append", default=[], help="An issue label (repeatable).")
    parser.add_argument("--issue-type", help="The GitHub issue type, e.g. Task, Bug, Epic.")
    parser.add_argument("--type", dest="form", help="Force a form: task, bug, decision or bet.")
    parser.add_argument("--forms-dir", type=Path, help="Directory holding the issue form YAML files.")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON.")
    args = parser.parse_args(argv)
    try:
        body = sys.stdin.read() if args.body_file == "-" else Path(args.body_file).read_text(encoding="utf-8")
        forms = load_forms(args.forms_dir or default_forms_dir())
        result = check(body, args.label, args.issue_type, forms, args.form)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"check_issue: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), indent=2) if args.json else result.report())
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
