"""The two things this tool produces: a brief for the reviewing agent, and the
review guide a person reads on the pull request or merge request."""

from __future__ import annotations

import re

from .tiers import TIERS, Change, Config, Decision, Match, Outcome, Review, plural

MARKER = "<!-- review-tiers"
_ACTION = re.compile(r"<!-- review-tiers action=(\w+) -->")

MAX_HIGH = 20
MAX_MEDIUM = 10
MAX_FINDINGS = 20
MAX_FOCUS = 10


def marker(action: str) -> str:
    return f"{MARKER} action={action} -->"


def previous_action(body: str) -> str | None:
    """The action recorded in an earlier guide comment, if that is what ``body`` is."""
    found = _ACTION.match(body or "")
    return found.group(1) if found else None


# --------------------------------------------------------------------- untrusted text


def code(text: str, limit: int = 120) -> str:
    """Show untrusted text (a path, a login, a commit) as an inline code span."""
    cleaned = "".join(ch for ch in str(text) if ch.isprintable()).replace("`", "'")
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "…"
    return f"`{cleaned}`"


def plain(text: str, limit: int = 300) -> str:
    """Make free text from a reviewing agent safe to place inside a comment.

    The result is always one line, so nothing in it can start a line: on GitLab a
    comment line beginning with "/" is run as a quick action (such as /approve).
    Mentions are broken so the text cannot notify people, and HTML is escaped.
    """
    cleaned = "".join(ch if ch.isprintable() else " " for ch in str(text))
    cleaned = " ".join(cleaned.split())
    cleaned = cleaned.replace("<", "&lt;").replace(">", "&gt;")
    cleaned = re.sub(r"@(?=\w)", "@​", cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "…"
    return cleaned


def _cap(text: str) -> str:
    """Upper-case the first letter only (str.capitalize would lower-case the rest)."""
    return text[:1].upper() + text[1:]


def _sentence(text: str) -> str:
    return text if text.endswith((".", "?", "!")) else text + "."


def _where(path: str | None, line: int | None) -> str:
    if not path:
        return ""
    return code(f"{path}:{line}" if line else path)


# ------------------------------------------------------------------------------ brief


def brief(config: Config, host_name: str, repo: str, change: Change, decision: Decision) -> dict:
    """Everything a reviewing agent needs to start: the tier, and where to look.

    This names paths, so it is for the agent and the people who can already see the
    change. It is not the aggregate-only summary that ``publish`` prints.
    """
    grouped = decision.by_tier()
    ordered = [match for tier in reversed(TIERS) for match in grouped[tier]]
    return {
        "schema": 1,
        "host": host_name,
        "repo": repo,
        "change": change.number,
        "head_sha": change.head_sha,
        "target_branch": change.target_branch,
        "mode": config.mode,
        "tier": decision.tier,
        "path_tier": decision.path_tier,
        "reviewers": decision.reviewers,
        "escalations": list(decision.escalations),
        "changed_files": decision.changed_files,
        "changed_lines": decision.changed_lines,
        "files_truncated": change.files_truncated,
        "files": [
            {
                "path": match.path,
                "tier": match.tier,
                "area": match.area,
                "check": match.check,
                "rule": match.pattern,
                "additions": match.additions,
                "deletions": match.deletions,
                "size_known": match.size_known,
            }
            for match in ordered
        ],
        "review_result": {
            "note": "Hand back a JSON object with these keys to `publish --review`. "
            "You may raise the tier; you cannot lower it.",
            "reviewed_sha": change.head_sha,
            "tier": None,
            "tier_reason": "",
            "summary": "",
            "findings": [],
            "focus": [],
        },
    }


# ------------------------------------------------------------------------------ guide


def _size(match: Match) -> str:
    if not match.size_known:
        return " (size not shown by the host)"
    return f" (+{match.additions} −{match.deletions})"


def _file_line(match: Match) -> str:
    line = f"- {code(match.path)}{_size(match)}"
    hint = " ".join(_sentence(part) for part in (match.area, match.check) if part)
    if hint:
        line += f": {hint}"
    return line


def _file_section(title: str, matches: list[Match], limit: int) -> list[str]:
    if not matches:
        return []
    lines = ["", f"**{title}**", ""]
    lines += [_file_line(match) for match in matches[:limit]]
    if len(matches) > limit:
        lines.append(f"- …and {len(matches) - limit} more")
    return lines


def _headline(
    config: Config,
    change: Change,
    outcome: Outcome,
    notify: tuple[str, ...],
    files_listed: bool,
) -> str:
    reasons = "; ".join(outcome.reasons)
    if outcome.action == "merge":
        sha = code((change.head_sha or "")[:7])
        return (
            f"**Clear.** {_cap(outcome.tier)} risk, reviewed clean at {sha}, checks green. "
            "Merging without a person."
        )
    if outcome.action == "hold":
        who = " ".join(f"@{login}" for login in notify)
        return f"**Needs a person{': ' + who if who else ''}.** {_cap(reasons)}."
    if outcome.action == "wait":
        return f"**Waiting.** {_cap(reasons)}. Nothing to do yet."
    if outcome.reviewers == 0:
        if outcome.review_state == "clean":
            return (
                "**Recommendation:** a light review is enough. No sensitive path is "
                "touched and the agent review found nothing important."
            )
        return "**Recommendation:** a light review is enough. No sensitive path is touched."
    start = " Start with the files listed first." if files_listed else ""
    return f"**Recommendation:** {plural(outcome.reviewers, 'reviewer')}.{start}"


def _review_section(change: Change, outcome: Outcome, review: Review | None) -> list[str]:
    if outcome.review_state == "none":
        return ["", "No agent review was supplied: this guide comes from changed paths only."]
    if outcome.review_state == "stale" or review is None:
        return ["", "The agent review on hand is of an older commit, so it is not shown."]

    lines = ["", f"**Agent review** of {code((change.head_sha or '')[:7])}", ""]
    if review.summary:
        lines.append(f"- Summary: {plain(review.summary, 600)}")
    if not review.findings:
        lines.append("- No findings.")
    ordered = sorted(review.findings, key=lambda f: f.severity != "important")
    for finding in ordered[:MAX_FINDINGS]:
        label = "Important" if finding.severity == "important" else "Nit"
        where = _where(finding.path, finding.line)
        text = f"- {label}: {where + ' ' if where else ''}{plain(finding.title, 160)}"
        if finding.detail:
            text += f". {plain(finding.detail)}"
        lines.append(text)
    if len(review.findings) > MAX_FINDINGS:
        lines.append(f"- …and {len(review.findings) - MAX_FINDINGS} more")

    if review.focus:
        lines += ["", "**Where the agent says to look**", ""]
        for item in review.focus[:MAX_FOCUS]:
            lines.append(f"- {_where(item.path, item.line)}: {plain(item.why)}")
    return lines


def render_guide(
    config: Config,
    host,
    change: Change,
    decision: Decision,
    outcome: Outcome,
    review: Review | None = None,
    notify: tuple[str, ...] = (),
) -> str:
    """The comment a person reads. One comment per change, updated in place."""
    grouped = decision.by_tier()
    ranked = bool(grouped["high"] or grouped["medium"])
    lines = [
        marker(outcome.action),
        f"### Review guide: {outcome.tier} risk",
        "",
        _headline(config, change, outcome, notify, ranked),
    ]

    lines += _file_section("High risk: read first", grouped["high"], MAX_HIGH)
    lines += _file_section("Medium risk", grouped["medium"], MAX_MEDIUM)
    if grouped["low"] and not ranked and outcome.tier != "low":
        # Low by path but raised (size, a label or the agent): show what changed.
        lines += _file_section("Changed files (low risk by path)", grouped["low"], MAX_MEDIUM)
    elif grouped["low"]:
        lines += ["", f"**Low risk: skim.** {plural(len(grouped['low']), 'file')}."]

    lines += _review_section(change, outcome, review)

    why = [f"- {_cap(escalation)}." for escalation in decision.escalations]
    if outcome.raised_by_review and review:
        reason = plain(review.tier_reason) or "no reason given"
        why.append(f"- Raised to {outcome.tier} by the agent review: {reason}")
    if why:
        lines += ["", "**Why this tier**", "", *why]

    lines += ["", "---"]
    where = code(config.config_path or "the tier map")
    branch = code(change.target_branch) if change.target_branch else "the target branch"
    footer = f"Tiers come from {where} on {branch}, plus the agent review."
    if config.post_labels:
        disagree = code(config.label("disagree", host.label_separator))
        footer += f" Think the tier is wrong? Add the {disagree} label and say why."
    lines.append(footer)
    return "\n".join(lines) + "\n"
