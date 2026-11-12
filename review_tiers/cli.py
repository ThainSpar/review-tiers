"""Command line.

    review-tiers brief    Print the tier and the files to look at, as JSON, for an agent.
    review-tiers publish  Post the review guide; in auto mode, merge or hold.
    review-tiers explain  Show which rule decides the tier of given paths. Offline.
    review-tiers replay   Replay the tier map over merged history (GitHub).

The tier map is read from the change's target branch through the host's API, never
from the change itself, so a change cannot lower its own tier.
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # noqa: UP036 (a clear message on older Pythons)
    sys.exit("review-tiers needs Python 3.11 or newer")

import argparse
import json
import os
import time
from collections.abc import Callable
from pathlib import Path

from . import guide
from .hosts import NotSupported
from .hosts.github import GitHub
from .hosts.gitlab import GitLab
from .http import ApiError
from .tiers import (
    TIERS,
    Change,
    Config,
    ConfigError,
    Outcome,
    Review,
    ReviewError,
    classify_change,
    classify_path,
    decide,
    parse_config,
    parse_review,
)

DEFAULT_CONFIG = ".review-tiers.toml"
POLL_SECONDS = 15


# ------------------------------------------------------------------------------ setup


def make_host(args, env: dict, transport=None):
    """Build the host client from arguments and environment variables."""
    kwargs = {"transport": transport} if transport else {}
    if args.host == "github":
        token = env.get(args.token_env or "") or env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
        if not token:
            raise ConfigError("no token: set GITHUB_TOKEN, or name a variable with --token-env")
        base = args.base_url or env.get("GITHUB_API_URL") or "https://api.github.com"
        return GitHub(args.repo, token, base, **kwargs)
    token = env.get(args.token_env or "") or env.get("GITLAB_TOKEN")
    if not token:
        raise ConfigError("no token: set GITLAB_TOKEN, or name a variable with --token-env")
    base = args.base_url or env.get("GITLAB_URL") or env.get("CI_SERVER_URL")
    if not base:
        raise ConfigError("no GitLab address: pass --base-url or set GITLAB_URL")
    return GitLab(args.repo, token, base, **kwargs)


def load_config(args, host, change: Change) -> Config:
    """Read the tier map from a local file, or from the target branch via the API."""
    if args.config_file:
        try:
            text = Path(args.config_file).read_text(encoding="utf-8")
        except OSError as error:
            raise ConfigError(f"cannot read {args.config_file}: {error}") from error
        return parse_config(text, config_path=args.config)
    if not change.target_branch:
        raise ConfigError("the host did not say which branch the change targets")
    text = host.read_file(args.config, change.target_branch)
    if text is None:
        raise ConfigError(
            f"no tier map at {args.config} on branch {change.target_branch}. "
            "Add one there, or pass --config-file to try a local draft."
        )
    return parse_config(text, config_path=args.config)


def load_review(path: str | None) -> Review | None:
    if not path:
        return None
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        data = json.loads(text)
    except OSError as error:
        raise ReviewError(f"cannot read the review result: {error}") from error
    except ValueError as error:
        raise ReviewError(f"the review result is not valid JSON: {error}") from error
    return parse_review(data)


# ---------------------------------------------------------------------------- publish


def find_guide_comment(host, number: int):
    """Our earlier guide comment on this change, if there is one.

    Anyone can paste the marker, so only a comment written by this token's own
    account counts. When the token cannot say who it is, only bot accounts count.
    """
    me = host.current_user()
    for comment in host.list_comments(number):
        if not comment.body.startswith(guide.MARKER):
            continue
        if (me and comment.author == me) or (not me and comment.author_is_bot):
            return comment
    return None


def sync_labels(host, config: Config, change: Change, outcome: Outcome, dry_run: bool):
    """Set the tier label and remove the other tiers' labels. Returns what it did."""
    sep = host.label_separator
    wanted = config.label(outcome.tier, sep)
    stale = [
        config.label(tier, sep)
        for tier in TIERS
        if tier != outcome.tier and config.label(tier, sep) in change.labels
    ]
    add = [] if wanted in change.labels else [wanted]
    if not add and not stale:
        return []
    if not dry_run:
        try:
            host.set_labels(change.number, add, stale)
        except ApiError as error:
            if error.status in (401, 403):
                return ["labels skipped: this token may not change labels"]
            raise
    return [f"add label {name}" for name in add] + [f"remove label {name}" for name in stale]


def sync_comment(host, number: int, existing, body: str, dry_run: bool) -> list[str]:
    if existing is None:
        if not dry_run:
            host.create_comment(number, body)
        return ["post guide comment"]
    if existing.body.strip() == body.strip():
        return []
    if not dry_run:
        host.update_comment(number, existing.id, body)
    return ["update guide comment"]


def publish(
    host,
    config: Config,
    change: Change,
    review: Review | None,
    *,
    dry_run: bool = False,
    wait_seconds: int = 0,
    extra_ignored_checks: frozenset[str] = frozenset(),
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict, str]:
    """Decide, write the guide and, in auto mode, merge or hold.

    Returns the result (counts and actions only, safe to log) and the guide text.
    """
    if config.mode == "auto" and not host.supports_auto:
        raise NotSupported(f"auto mode is not implemented for {host.name} in this version")
    if not change.is_open and not dry_run:
        raise NotSupported(
            f"this {host.change_word} is closed or merged, so nothing was written. "
            "Add --dry-run to see what the guide would have said."
        )

    decision = classify_change(config, change)
    outcome = decide(config, change, decision, review)

    if outcome.action == "merge":
        ignore = config.ignore_checks | extra_ignored_checks
        remaining = wait_seconds
        while True:
            checks = host.checks(change.head_sha, ignore)
            outcome = decide(config, change, decision, review, checks)
            if checks.state != "pending" or remaining <= 0:
                break
            sleep(POLL_SECONDS)
            remaining -= POLL_SECONDS

    writes: list[str] = []
    existing = find_guide_comment(host, change.number) if config.post_comment else None
    was_held = guide.previous_action(existing.body) == "hold" if existing else False

    def write_guide(current: Outcome) -> str:
        body = guide.render_guide(config, host, change, decision, current, review, config.notify)
        if config.post_comment:
            writes.extend(sync_comment(host, change.number, existing, body, dry_run))
        return body

    body = write_guide(outcome)
    if config.post_labels:
        writes.extend(sync_labels(host, config, change, outcome, dry_run))

    if outcome.action == "merge":
        writes.append(f"merge ({config.merge_method})")
        if not dry_run:
            try:
                host.merge(change.number, change.head_sha, config.merge_method)
            except ApiError as error:
                # Refused: moved on since the review, conflicts, or a host rule.
                writes[-1] = "merge refused by the host"
                outcome = Outcome(
                    outcome.tier,
                    outcome.reviewers,
                    outcome.raised_by_review,
                    outcome.review_state,
                    "hold",
                    (f"the host refused the merge (HTTP {error.status})",),
                )
                if config.post_comment:
                    existing = find_guide_comment(host, change.number)
                body = write_guide(outcome)

    if outcome.action == "hold" and config.notify and not was_held:
        missing = [login for login in config.notify if login not in change.assignees]
        if missing:
            writes.append(f"assign {len(missing)} owner(s)")
            if not dry_run:
                host.assign(change.number, missing)

    result = {
        "change": change.number,
        "mode": config.mode,
        "dry_run": dry_run,
        **decision.summary(),
        "tier": outcome.tier,
        "reviewers": outcome.reviewers,
        "raised_by_review": outcome.raised_by_review,
        "review": outcome.review_state,
        "action": outcome.action,
        "reasons": list(outcome.reasons),
        "writes": writes,
    }
    return result, body


# --------------------------------------------------------------------------- commands


def cmd_brief(args, env, transport=None) -> int:
    host = make_host(args, env, transport)
    change = host.get_change(args.change)
    config = load_config(args, host, change)
    decision = classify_change(config, change)
    print(json.dumps(guide.brief(config, host.name, args.repo, change, decision), indent=2))
    return 0


def cmd_publish(args, env, transport=None, sleep=time.sleep) -> int:
    review = load_review(args.review)
    host = make_host(args, env, transport)
    change = host.get_change(args.change)
    config = load_config(args, host, change)
    result, body = publish(
        host,
        config,
        change,
        review,
        dry_run=args.dry_run,
        wait_seconds=args.wait,
        extra_ignored_checks=frozenset(args.ignore_check or []),
        sleep=sleep,
    )
    print(json.dumps(result, indent=2))
    if args.show_guide:
        print("\n" + body)
    return 0


def cmd_explain(args, env, transport=None) -> int:
    try:
        text = Path(args.config_file).read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"cannot read {args.config_file}: {error}") from error
    config = parse_config(text, config_path=args.config)
    print(f"mode: {config.mode}   default tier: {config.default_tier}")
    for path in args.paths:
        match = classify_path(config, path)
        if match.pattern is None:
            why = "no rule matched"
        elif match.source == "protected":
            why = f"always high: {match.pattern}"
        else:
            why = f"rule {match.pattern}"
        print(f"{match.tier:<7}{path}  ({why})")
    return 0


def cmd_replay(args, env, transport=None) -> int:
    from . import replay

    return replay.run(args, env, transport)


# ------------------------------------------------------------------------------- main


def _host_args(parser) -> None:
    parser.add_argument("--host", required=True, choices=["github", "gitlab"])
    parser.add_argument(
        "--repo", required=True, help="owner/repo on GitHub; group/project or id on GitLab"
    )
    parser.add_argument("--base-url", help="API address (GitLab: the server's address)")
    parser.add_argument("--token-env", help="environment variable holding the token")
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG, help=f"tier map path in the repo ({DEFAULT_CONFIG})"
    )
    parser.add_argument("--config-file", help="read the tier map from this local file instead")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-tiers",
        description="Risk tiers and review guides for pull requests and merge requests.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    brief = commands.add_parser("brief", help="print the tier and files to look at, as JSON")
    _host_args(brief)
    brief.add_argument("--change", required=True, type=int, help="pull or merge request number")
    brief.set_defaults(run=cmd_brief)

    pub = commands.add_parser("publish", help="post the review guide; merge or hold in auto mode")
    _host_args(pub)
    pub.add_argument("--change", required=True, type=int, help="pull or merge request number")
    pub.add_argument("--review", help="review result JSON from the agent ('-' for stdin)")
    pub.add_argument("--dry-run", action="store_true", help="decide, but change nothing")
    pub.add_argument("--show-guide", action="store_true", help="also print the guide text")
    pub.add_argument(
        "--wait",
        type=int,
        default=0,
        metavar="SECONDS",
        help="auto mode: wait up to this long for other checks to finish",
    )
    pub.add_argument(
        "--ignore-check",
        action="append",
        metavar="NAME",
        help="auto mode: a check not to wait for, such as the job running this tool",
    )
    pub.set_defaults(run=cmd_publish)

    explain = commands.add_parser("explain", help="show which rule decides given paths")
    explain.add_argument("--config-file", required=True, help="local tier map to read")
    explain.add_argument(
        "--config", default=DEFAULT_CONFIG, help="the path the map will have in the repo"
    )
    explain.add_argument("paths", nargs="+")
    explain.set_defaults(run=cmd_explain)

    from . import replay

    replay.add_parser(commands)
    return parser


def main(argv: list[str] | None = None, env: dict | None = None, transport=None) -> int:
    args = build_parser().parse_args(argv)
    env = dict(os.environ) if env is None else env
    try:
        return args.run(args, env, transport)
    except (ConfigError, ReviewError, NotSupported) as error:
        print(f"review-tiers: {error}", file=sys.stderr)
        return 2
    except ApiError as error:
        print(f"review-tiers: {error}", file=sys.stderr)
        return 1
