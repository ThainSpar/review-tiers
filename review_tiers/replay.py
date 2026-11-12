"""Replay the tier map over merged pull requests and print a review baseline.

    review-tiers replay --host github --repo OWNER/REPO --days 90

For each pull request merged in the window it asks what the tier map would say today,
and sets that beside what actually happened: who reviewed it, whether anyone asked
for changes, whether it was later reverted.

The default report holds counts, shares and times only. It names no pull request,
author, head branch or path, so the numbers can be quoted in a proposal. --tuning adds
a section that does name paths and pull request numbers, for the people who maintain
the tier map; keep that part inside the repository's own tools.

GitHub only in this version.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .hosts.github import FILE_LIMIT, change_from_api
from .http import Api, quote
from .tiers import TIERS, Config, ConfigError, classify_change

REVERTS_NUMBER = re.compile(r"\bReverts\s+(?:[\w.-]+/[\w.-]+)?#(\d+)", re.IGNORECASE)
REVERT_TITLE = re.compile(r"""^Revert\s+["“'](?P<title>.+)["”']\s*(?:\(#\d+\))?\s*$""")
DECISIVE = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")


def parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def hours_between(start: datetime, end: datetime) -> float:
    return max((end - start).total_seconds(), 0.0) / 3600


# ----------------------------------------------------------------------------- facts


@dataclass
class ReviewFacts:
    human_reviewers: int = 0
    human_approvals: int = 0
    human_changes_requested: bool = False
    human_feedback: bool = False
    agent_reviews: int = 0
    review_rounds: int = 0
    stale_approval: bool | None = None  # None when no person approved
    first_human_review_at: datetime | None = None


def summarize_reviews(
    reviews: list[dict], author: str, config: Config, head_sha: str | None
) -> ReviewFacts:
    """Reduce a pull request's reviews to the facts the baseline needs.

    A human review is one from an account that is neither a bot nor listed under
    authors.agents, and is not the pull request's author. Feedback means a person
    requested changes or submitted review comments.
    """
    facts = ReviewFacts()
    submitted = [
        review
        for review in reviews
        if review.get("state") not in (None, "PENDING") and review.get("submitted_at")
    ]
    submitted.sort(key=lambda review: review["submitted_at"])
    facts.review_rounds = len(
        {review.get("commit_id") for review in submitted if review.get("commit_id")}
    )

    latest_decision: dict[str, dict] = {}
    humans: set[str] = set()
    for review in submitted:
        user = review.get("user") or {}
        login = (user.get("login") or "").lower()
        if config.is_agent(login, (user.get("type") or "").lower() == "bot"):
            facts.agent_reviews += 1
            continue
        if not login or login == author.lower():
            continue
        humans.add(login)
        state = review["state"]
        if facts.first_human_review_at is None:
            facts.first_human_review_at = parse_time(review["submitted_at"])
        if state == "CHANGES_REQUESTED":
            facts.human_changes_requested = True
            facts.human_feedback = True
        elif state == "COMMENTED":
            facts.human_feedback = True
        if state in DECISIVE:
            latest_decision[login] = review

    facts.human_reviewers = len(humans)
    approvals = [review for review in latest_decision.values() if review["state"] == "APPROVED"]
    facts.human_approvals = len(approvals)
    if approvals and head_sha:
        facts.stale_approval = all(review.get("commit_id") != head_sha for review in approvals)
    return facts


def find_reverts(pulls: list[dict]) -> tuple[set[int], set[int]]:
    """Return (numbers of reverted pull requests, numbers of the reverting ones).

    A revert is recognised from GitHub's "Reverts #N" text in the body, or from a
    title of the form Revert "original title" matched to an earlier pull request.
    """
    by_number = {pull["number"]: pull for pull in pulls}
    by_title: dict[str, list[dict]] = {}
    for pull in pulls:
        title = re.sub(r"\s*\(#\d+\)\s*$", "", pull.get("title") or "").strip()
        by_title.setdefault(title, []).append(pull)

    reverted: set[int] = set()
    reverting: set[int] = set()
    for pull in pulls:
        target: int | None = None
        found = REVERTS_NUMBER.search(pull.get("body") or "")
        if found and int(found.group(1)) != pull["number"]:
            target = int(found.group(1))
        else:
            titled = REVERT_TITLE.match((pull.get("title") or "").strip())
            if titled:
                reverting.add(pull["number"])
                earlier = [
                    other
                    for other in by_title.get(titled.group("title").strip(), [])
                    if other["number"] != pull["number"]
                    and other.get("merged_at", "") <= pull.get("merged_at", "")
                ]
                if earlier:
                    target = max(earlier, key=lambda other: other["merged_at"])["number"]
        if target is not None:
            reverting.add(pull["number"])
            if target in by_number:
                reverted.add(target)
    return reverted, reverting


@dataclass
class Record:
    number: int
    tier: str
    low_by_path: bool
    no_human_needed: bool
    author_is_agent: bool
    changed_lines: int
    hours_to_merge: float
    hours_to_first_human_review: float | None
    reviews: ReviewFacts
    reverted: bool = False
    is_revert: bool = False
    unmatched: list[str] = field(default_factory=list)
    rules_hit: list[str] = field(default_factory=list)


def build_record(pull: dict, files: list[dict], reviews: list[dict], config: Config) -> Record:
    # A merged pull request is judged as ready for review, whatever it was earlier.
    view = change_from_api({**pull, "draft": False}, files)
    decision = classify_change(config, view)
    created, merged = parse_time(pull["created_at"]), parse_time(pull["merged_at"])
    facts = summarize_reviews(reviews, view.author, config, view.head_sha)
    first = facts.first_human_review_at
    return Record(
        number=pull["number"],
        tier=decision.tier,
        low_by_path=decision.path_tier == "low",
        no_human_needed=decision.no_human_needed,
        author_is_agent=decision.author_is_agent,
        changed_lines=decision.changed_lines,
        hours_to_merge=hours_between(created, merged),
        hours_to_first_human_review=hours_between(created, first) if first else None,
        reviews=facts,
        unmatched=decision.unmatched(),
        rules_hit=sorted({match.pattern for match in decision.matches if match.pattern}),
    )


# ------------------------------------------------------------------------- aggregate


def share(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def percentile(values: list[float], fraction: float) -> float | None:
    """Linear-interpolated percentile; ``fraction`` is between 0 and 1."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 2)


def _counted(records: list[Record], test) -> dict:
    count = sum(1 for record in records if test(record))
    return {"count": count, "share": share(count, len(records))}


def aggregate(records: list[Record], config: Config) -> dict:
    """Turn per-pull-request records into the numbers the report prints."""
    total = len(records)
    tiers = {}
    for tier in TIERS:
        group = [record for record in records if record.tier == tier]
        tiers[tier] = {
            "count": len(group),
            "share": share(len(group), total),
            "median_hours_to_merge": median([record.hours_to_merge for record in group]),
            "p90_hours_to_merge": percentile([record.hours_to_merge for record in group], 0.9),
            "median_changed_lines": median([float(record.changed_lines) for record in group]),
            "no_human_review": _counted(group, lambda r: r.reviews.human_reviewers == 0),
            "reverted": _counted(group, lambda r: r.reverted),
        }

    low = [record for record in records if record.tier == "low"]
    approved = [record for record in records if record.reviews.stale_approval is not None]
    first_review = [
        record.hours_to_first_human_review
        for record in records
        if record.hours_to_first_human_review is not None
    ]
    result = {
        "merged": total,
        "tiers": tiers,
        "no_person": {
            "low_tier": _counted(records, lambda r: r.tier == "low"),
            "eligible": _counted(records, lambda r: r.no_human_needed),
            "trusted_list_empty": not config.trusted_authors,
            "raised_from_low_by_size_or_label": sum(
                1 for record in records if record.low_by_path and record.tier != "low"
            ),
        },
        "low_tier_checks": {
            "count": len(low),
            "human_requested_changes": _counted(low, lambda r: r.reviews.human_changes_requested),
            "human_left_feedback": _counted(low, lambda r: r.reviews.human_feedback),
            "reverted": _counted(low, lambda r: r.reverted),
        },
        "today": {
            "median_hours_to_merge": median([record.hours_to_merge for record in records]),
            "p90_hours_to_merge": percentile([record.hours_to_merge for record in records], 0.9),
            "median_hours_to_first_human_review": median(first_review),
            "no_human_review": _counted(records, lambda r: r.reviews.human_reviewers == 0),
            "human_feedback": _counted(records, lambda r: r.reviews.human_feedback),
            "stale_approval": {
                "count": sum(1 for record in approved if record.reviews.stale_approval),
                "share": share(
                    sum(1 for record in approved if record.reviews.stale_approval),
                    len(approved),
                ),
                "of_approved": len(approved),
            },
            "over_round_cap": _counted(
                records, lambda r: r.reviews.review_rounds > config.max_review_rounds
            ),
            "agent_authored": _counted(records, lambda r: r.author_is_agent),
            "reverted": _counted(records, lambda r: r.reverted),
            "revert_pull_requests": sum(1 for record in records if record.is_revert),
        },
    }
    return result


def breaker(summary: dict, max_revert_rate: float, min_sample: int) -> tuple[bool, str]:
    """Should the no-person path be switched off? Judged on low-tier reverts."""
    checks = summary["low_tier_checks"]
    count, reverted = checks["count"], checks["reverted"]["count"]
    if count < min_sample:
        return False, (
            f"Circuit breaker: not judged. {count} low-tier pull requests in the "
            f"window, fewer than the minimum sample of {min_sample}."
        )
    rate = reverted / count
    if rate > max_revert_rate:
        return True, (
            f"Circuit breaker: TRIPPED. {reverted} of {count} low-tier pull requests "
            f"were reverted ({rate:.1%}), over the limit of {max_revert_rate:.1%}."
        )
    return False, (
        f"Circuit breaker: clear. {reverted} of {count} low-tier pull requests were "
        f"reverted ({rate:.1%}), within the limit of {max_revert_rate:.1%}."
    )


# ----------------------------------------------------------------------------- report


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}" if value in (0, 1) else f"{value:.1%}"


def num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:g}"


def of(counted: dict, whole: int) -> str:
    return f"{counted['count']} of {whole} ({pct(counted['share'])})"


def render_report(summary: dict, repo_label: str, days: int, base: str, config: Config) -> str:
    total = summary["merged"]
    lines = [
        f"# Review baseline: last {days} days",
        "",
        f"{total} pull requests merged into `{base}` in {repo_label}, replayed "
        f"against the tier map as it is today "
        f"(`{config.config_path}`).",
        "",
    ]
    if not total:
        lines.append("No merged pull requests in the window, so there is nothing to report.")
        return "\n".join(lines) + "\n"

    lines += [
        "## What the tier map would have said",
        "",
        "| Tier | Pull requests | Share | Median hours open | p90 hours open "
        "| Median changed lines | No human review | Later reverted |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for tier in TIERS:
        row = summary["tiers"][tier]
        lines.append(
            f"| {tier} | {row['count']} | {pct(row['share'])} "
            f"| {num(row['median_hours_to_merge'])} "
            f"| {num(row['p90_hours_to_merge'])} "
            f"| {num(row['median_changed_lines'])} "
            f"| {pct(row['no_human_review']['share'])} "
            f"| {row['reverted']['count']} |"
        )

    zero, low = summary["no_person"], summary["low_tier_checks"]
    lines += ["", "## What auto mode would let through", ""]
    lines.append(f"- Low tier: {of(zero['low_tier'], total)}.")
    if zero["raised_from_low_by_size_or_label"]:
        lines.append(
            f"- Low by path but raised to medium for size or by label: "
            f"{zero['raised_from_low_by_size_or_label']}."
        )
    if zero["trusted_list_empty"]:
        lines.append(
            "- Eligible to merge with no human: 0. The trusted list is empty, so "
            "nobody is eligible yet."
        )
    else:
        lines.append(
            f"- Eligible to merge with no human under the current trusted list: "
            f"{of(zero['eligible'], total)}."
        )
    if low["count"]:
        lines.append(
            f"- Of the {low['count']} low-tier pull requests, a person requested "
            f"changes on {low['human_requested_changes']['count']}, "
            f"left review comments on "
            f"{low['human_left_feedback']['count']}, and {low['reverted']['count']} "
            f"{'was' if low['reverted']['count'] == 1 else 'were'} later reverted."
        )

    today = summary["today"]
    stale = today["stale_approval"]
    lines += [
        "",
        "## Review today",
        "",
        f"- Median hours from open to merge: {num(today['median_hours_to_merge'])} "
        f"(p90 {num(today['p90_hours_to_merge'])}).",
        f"- Median hours to the first human review: "
        f"{num(today['median_hours_to_first_human_review'])}.",
        f"- Merged with no human review: {of(today['no_human_review'], total)}.",
        f"- A person requested changes or left review comments: "
        f"{of(today['human_feedback'], total)}.",
        f"- Approved, but not on the commit that merged: {stale['count']} of "
        f"{stale['of_approved']} approved ({pct(stale['share'])}).",
        f"- More than {config.max_review_rounds} review rounds: "
        f"{of(today['over_round_cap'], total)}.",
        f"- Opened by an agent account: {of(today['agent_authored'], total)}.",
        f"- Later reverted: {of(today['reverted'], total)}.",
    ]

    lines += [
        "",
        "## How to read this",
        "",
        "- Tiers come from changed paths and size under today's map, applied to "
        "history. A path map cannot see that a change is behind a flag or "
        "internal-only.",
        "- A human review is one from an account that is not a bot, not listed under "
        "`authors.agents`, and not the author. An agent working under a person's own "
        "account counts as that person.",
        '- Reverts are found from GitHub\'s "Reverts #N" text or a `Revert "..."` '
        "title, and only when both pull requests merged inside the window. A revert "
        "pushed straight to the branch is not seen.",
    ]
    return "\n".join(lines) + "\n"


def render_tuning(records: list[Record], top: int = 15) -> str:
    """Paths and pull request numbers for tuning the map. Not for sharing outside."""
    unmatched: Counter[str] = Counter()
    for record in records:
        for path in record.unmatched:
            parts = path.split("/")
            unmatched["/".join(parts[:2]) + ("/" if len(parts) > 2 else "")] += 1
    rules: Counter[str] = Counter()
    for record in records:
        rules.update(record.rules_hit)
    low = [record for record in records if record.tier == "low"]

    def numbers(test) -> str:
        found = [f"#{record.number}" for record in low if test(record)]
        return ", ".join(found) if found else "none"

    lines = [
        "## Tuning (internal: names paths and pull requests)",
        "",
        "Changed files no rule matched, by top two path segments:",
        "",
    ]
    lines += [f"- `{prefix}`: {count}" for prefix, count in unmatched.most_common(top)] or [
        "- none"
    ]
    lines += ["", "Rules by number of pull requests they matched:", ""]
    lines += [f"- `{pattern}`: {count}" for pattern, count in rules.most_common(top)] or ["- none"]
    lines += [
        "",
        "Low-tier pull requests worth reading before trusting the map:",
        "",
        "- A person requested changes: " + numbers(lambda r: r.reviews.human_changes_requested),
        "- Later reverted: " + numbers(lambda r: r.reverted),
    ]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------ fetch


def list_merged(api: Api, repo: str, base: str, since: datetime, max_prs: int | None) -> list[dict]:
    """Merged pull requests into ``base`` since ``since``, newest first."""
    found: list[dict] = []
    page = 1
    while True:
        batch = (
            api.get(
                f"repos/{repo}/pulls?state=closed&base={quote(base)}"
                f"&sort=updated&direction=desc&per_page=100&page={page}"
            )
            or []
        )
        for pull in batch:
            if pull.get("merged_at") and parse_time(pull["merged_at"]) >= since:
                found.append(pull)
        # Sorted by last update: once a whole page is older than the window, stop.
        if len(batch) < 100 or parse_time(batch[-1]["updated_at"]) < since:
            break
        page += 1
    found.sort(key=lambda pull: pull["merged_at"], reverse=True)
    return found[:max_prs] if max_prs else found


def fetch_record(api: Api, repo: str, pull: dict, config: Config) -> Record:
    number = pull["number"]
    files = api.get_all(f"repos/{repo}/pulls/{number}/files", limit=FILE_LIMIT)
    reviews = api.get_all(f"repos/{repo}/pulls/{number}/reviews")
    return build_record(pull, files, reviews, config)


def collect(
    api: Api, repo: str, pulls: list[dict], config: Config, workers: int, progress=None
) -> list[Record]:
    reverted, reverting = find_reverts(pulls)
    records: list[Record] = []
    with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
        futures = [pool.submit(fetch_record, api, repo, pull, config) for pull in pulls]
        for done, future in enumerate(futures, start=1):
            record = future.result()
            record.reverted = record.number in reverted
            record.is_revert = record.number in reverting
            records.append(record)
            if progress and (done % 25 == 0 or done == len(futures)):
                progress(done, len(futures))
    return records


# ---------------------------------------------------------------------------- command


def add_parser(commands) -> None:
    from .cli import DEFAULT_CONFIG, cmd_replay

    parser = commands.add_parser("replay", help="replay the tier map over merged history (GitHub)")
    parser.add_argument("--host", required=True, choices=["github"])
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--base-url", help="API address")
    parser.add_argument("--token-env", help="environment variable holding the token")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"tier map path in the repo ({DEFAULT_CONFIG})",
    )
    parser.add_argument("--config-file", help="read the tier map from this local file instead")
    parser.add_argument("--days", type=int, default=90, help="window to replay (default 90)")
    parser.add_argument("--base", help="base branch (default: the repository's default branch)")
    parser.add_argument("--max-prs", type=int, help="replay only the newest N merged pull requests")
    parser.add_argument("--workers", type=int, default=4, help="parallel API readers (default 4)")
    parser.add_argument("--json", metavar="FILE", help="also write the numbers as JSON")
    parser.add_argument(
        "--name-repo",
        action="store_true",
        help="print the repository name in the report",
    )
    parser.add_argument(
        "--tuning",
        action="store_true",
        help="add the internal section that names paths and pull request numbers",
    )
    parser.add_argument(
        "--breaker-max-revert-rate",
        type=float,
        metavar="RATE",
        help="exit 3 when more than this share of low-tier pull requests was "
        "reverted (0.05 means 5%%)",
    )
    parser.add_argument(
        "--breaker-min-sample",
        type=int,
        default=20,
        help="low-tier pull requests needed before the breaker is judged (default 20)",
    )
    parser.set_defaults(run=cmd_replay, now=None)


def run(args, env: dict, transport=None) -> int:
    from pathlib import Path

    from .cli import make_host
    from .tiers import parse_config

    host = make_host(args, env, transport)
    api, repo = host.api, args.repo
    now = args.now or datetime.now(UTC)

    def progress(done: int, total: int) -> None:
        print(f"  read {done} of {total} pull requests", file=sys.stderr)

    base = args.base or api.get(f"repos/{repo}")["default_branch"]
    if args.config_file:
        try:
            text = Path(args.config_file).read_text(encoding="utf-8")
        except OSError as error:
            raise ConfigError(f"cannot read {args.config_file}: {error}") from error
    else:
        text = host.read_file(args.config, base)
        if text is None:
            raise ConfigError(
                f"no tier map at {args.config} on branch {base}. "
                "Pass --config-file to replay a local draft."
            )
    config = parse_config(text, config_path=args.config)

    pulls = list_merged(api, repo, base, now - timedelta(days=args.days), args.max_prs)
    print(f"{len(pulls)} merged pull requests to replay", file=sys.stderr)
    records = collect(api, repo, pulls, config, args.workers, progress)

    summary = aggregate(records, config)
    label = f"`{repo}`" if args.name_repo else "this repository"
    report = render_report(summary, label, args.days, base, config)

    tripped = False
    if args.breaker_max_revert_rate is not None:
        tripped, message = breaker(summary, args.breaker_max_revert_rate, args.breaker_min_sample)
        summary["breaker"] = {"tripped": tripped, "message": message}
        report += f"\n## Circuit breaker\n\n{message}\n"
    if args.tuning:
        report += "\n" + render_tuning(records)

    print(report)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({"days": args.days, **summary}, handle, indent=2)
            handle.write("\n")
    return 3 if tripped else 0
