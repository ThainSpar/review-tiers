"""Risk tiers: the tier map, path matching, the tier decision and what to do about it.

Pure logic. Nothing in this module reads the network, the environment or the disk, so
the command line, the history replay and the tests all share one decision.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass

TIERS = ("low", "medium", "high")
RANK = {tier: rank for rank, tier in enumerate(TIERS)}

# recommend: describe the risk and say who should look. Never approves or merges.
# auto: additionally merge what is low risk and reviewed clean, and hold the rest.
MODES = ("recommend", "auto")

MERGE_METHODS = ("merge", "squash", "rebase")

# High tier whatever the tier map says, so a change can never lower the bar that judges
# it. The tier map's own path is added when the map is loaded.
BUILTIN_PROTECTED = (
    ".github/workflows/**",
    ".github/actions/**",
    ".gitlab-ci.yml",
    ".gitlab/**",
    "**/CODEOWNERS",
    "**/REVIEW.md",
)
PROTECTED_AREA = "Review and CI configuration"
PROTECTED_CHECK = (
    "This changes how changes are built, checked or reviewed. Confirm that is intended."
)

DEFAULT_REVIEWERS = {"low": 0, "medium": 1, "high": 2}


class ConfigError(ValueError):
    """The tier map is missing, malformed or asks for something unsupported."""


class ReviewError(ValueError):
    """The review result handed in by the reviewing agent is malformed."""


# --------------------------------------------------------------------------- patterns


def compile_glob(pattern: str) -> re.Pattern[str]:
    """Compile a tier map pattern into a regex matched against a repo-relative path.

    Patterns are anchored at the repository root and use forward slashes:

    - ``*`` matches within one path segment, ``?`` matches one character of a segment.
    - ``**/`` matches zero or more directories, so ``**/x.py`` also matches ``x.py``.
    - ``dir/**`` (or ``dir/``) matches everything under ``dir``.
    - Everything else is literal. There are no character classes and no negation.
    """
    if not isinstance(pattern, str) or not pattern.strip():
        raise ConfigError(f"empty or non-string pattern: {pattern!r}")
    if pattern.startswith("/"):
        raise ConfigError(
            f"pattern {pattern!r}: patterns are relative to the repository root, "
            "remove the leading '/'"
        )
    if "\\" in pattern:
        raise ConfigError(f"pattern {pattern!r}: use forward slashes")
    if pattern.endswith("/"):
        pattern += "**"

    out: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            if pattern.startswith("**", i):
                at_segment_start = i == 0 or pattern[i - 1] == "/"
                if at_segment_start and pattern.startswith("**/", i):
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        i += 1
    # DOTALL: a path may legally contain a newline, and it must not slip past "**".
    return re.compile("".join(out) + r"\Z", re.DOTALL)


@dataclass(frozen=True)
class Rule:
    pattern: str
    tier: str
    regex: re.Pattern[str]
    area: str | None = None  # short name for this part of the code
    check: str | None = None  # what a reviewer should check here
    source: str = "rules"  # "protected" or "rules"

    def matches(self, path: str) -> bool:
        return self.regex.match(path) is not None


# ----------------------------------------------------------------------------- config


@dataclass(frozen=True)
class Config:
    mode: str
    default_tier: str
    rules: tuple[Rule, ...]  # in precedence order: protected, high, medium, low
    reviewers: dict[str, int]  # human reviewers each tier calls for
    max_low_changed_lines: int | None
    max_low_changed_files: int | None
    max_review_rounds: int
    trusted_authors: frozenset[str]
    agent_accounts: frozenset[str]
    post_comment: bool
    post_labels: bool
    label_names: dict[str, str]  # explicit names; missing keys use the host default
    force_human_label: str | None
    merge_method: str
    notify: tuple[str, ...]
    ignore_checks: frozenset[str]
    config_path: str | None

    def is_agent(self, login: str | None, is_bot: bool = False) -> bool:
        return is_bot or (login or "").lower() in self.agent_accounts

    def is_trusted(self, login: str | None) -> bool:
        return (login or "").lower() in self.trusted_authors

    def label(self, key: str, separator: str) -> str:
        """Label name for a tier or for "disagree". ``separator`` is ":" or "::"."""
        return self.label_names.get(key) or f"risk{separator}{key}"


_TOP_KEYS = {
    "version",
    "mode",
    "default_tier",
    "rules",
    "reviewers",
    "limits",
    "authors",
    "labels",
    "post",
    "auto",
}


def _reject_unknown(table: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(
            f"unknown key(s) in {where}: {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(allowed))}"
        )


def _table(data: dict, key: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table")
    return value


def _tier(value: object, where: str) -> str:
    if value not in TIERS:
        raise ConfigError(f"{where}: {value!r} is not a tier; use one of {', '.join(TIERS)}")
    return str(value)


def _strings(value: object, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where} must be a list of strings")
    return value


def _positive_int(value: object, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{where} must be a positive whole number")
    return value


def _flag(table: dict, key: str, default: bool, where: str) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{key} must be true or false")
    return value


def _text(table: dict, key: str, where: str) -> str | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{where}.{key} must be a string")
    return " ".join(value.split()) or None


def _own_path(config_path: str) -> str:
    path = config_path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def build_config(data: dict, config_path: str | None = None) -> Config:
    """Validate a parsed tier map. ``config_path`` is its repo-relative path."""
    if not isinstance(data, dict):
        raise ConfigError("the tier map must be a TOML table")
    _reject_unknown(data, _TOP_KEYS, "the tier map")

    version = data.get("version", 1)
    if version != 1:
        raise ConfigError(f"unsupported version {version!r}; this tool reads version 1")

    mode = data.get("mode", "recommend")
    if mode not in MODES:
        raise ConfigError(f"mode {mode!r} is not supported; use one of {', '.join(MODES)}")

    default_tier = _tier(data.get("default_tier", "medium"), "default_tier")

    protected = list(BUILTIN_PROTECTED)
    if config_path:
        protected.append(_own_path(config_path))
    rules = [
        Rule(
            pattern=pattern,
            tier="high",
            regex=compile_glob(pattern),
            area=PROTECTED_AREA,
            check=PROTECTED_CHECK,
            source="protected",
        )
        for pattern in protected
    ]

    raw_rules = data.get("rules", [])
    if not isinstance(raw_rules, list) or not all(isinstance(r, dict) for r in raw_rules):
        raise ConfigError("rules must be written as [[rules]] tables")
    listed: list[Rule] = []
    for index, raw in enumerate(raw_rules, start=1):
        where = f"rules #{index}"
        _reject_unknown(raw, {"tier", "paths", "area", "check"}, where)
        if "tier" not in raw:
            raise ConfigError(f"{where} needs a tier")
        tier = _tier(raw["tier"], f"{where}.tier")
        paths = _strings(raw.get("paths"), f"{where}.paths")
        if not paths:
            raise ConfigError(f"{where}.paths must list at least one pattern")
        area, check = _text(raw, "area", where), _text(raw, "check", where)
        for pattern in paths:
            listed.append(Rule(pattern, tier, compile_glob(pattern), area=area, check=check))
    # The stricter tier wins; within a tier, the rule written first wins.
    listed.sort(key=lambda rule: -RANK[rule.tier])
    rules.extend(listed)

    reviewers_table = _table(data, "reviewers")
    _reject_unknown(reviewers_table, set(TIERS), "[reviewers]")
    reviewers = dict(DEFAULT_REVIEWERS)
    for tier, count in reviewers_table.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ConfigError(f"reviewers.{tier} must be 0 or a positive whole number")
        reviewers[tier] = count
    if not reviewers["low"] <= reviewers["medium"] <= reviewers["high"]:
        raise ConfigError("reviewers must not decrease from low to medium to high")

    limits = _table(data, "limits")
    _reject_unknown(
        limits,
        {"max_low_changed_lines", "max_low_changed_files", "max_review_rounds"},
        "[limits]",
    )

    authors = _table(data, "authors")
    _reject_unknown(authors, {"trusted", "agents"}, "[authors]")

    labels = _table(data, "labels")
    _reject_unknown(labels, set(TIERS) | {"force_human", "disagree"}, "[labels]")
    label_names: dict[str, str] = {}
    for key in (*TIERS, "disagree"):
        name = _text(labels, key, "labels")
        if name:
            label_names[key] = name
    tier_names = [label_names[tier] for tier in TIERS if tier in label_names]
    if len(set(tier_names)) != len(tier_names):
        raise ConfigError("labels.low, labels.medium and labels.high must differ")
    force_human = labels.get("force_human", "needs-human")
    if not isinstance(force_human, str):
        raise ConfigError("labels.force_human must be a string")

    post = _table(data, "post")
    _reject_unknown(post, {"comment", "labels"}, "[post]")

    auto = _table(data, "auto")
    _reject_unknown(auto, {"merge_method", "notify", "ignore_checks"}, "[auto]")
    merge_method = auto.get("merge_method", "squash")
    if merge_method not in MERGE_METHODS:
        raise ConfigError(f"auto.merge_method must be one of {', '.join(MERGE_METHODS)}")

    return Config(
        mode=mode,
        default_tier=default_tier,
        rules=tuple(rules),
        reviewers=reviewers,
        max_low_changed_lines=_positive_int(
            limits.get("max_low_changed_lines"), "limits.max_low_changed_lines"
        ),
        max_low_changed_files=_positive_int(
            limits.get("max_low_changed_files"), "limits.max_low_changed_files"
        ),
        max_review_rounds=_positive_int(
            limits.get("max_review_rounds", 3), "limits.max_review_rounds"
        )
        or 3,
        trusted_authors=frozenset(
            name.lower() for name in _strings(authors.get("trusted", []), "authors.trusted")
        ),
        agent_accounts=frozenset(
            name.lower() for name in _strings(authors.get("agents", []), "authors.agents")
        ),
        post_comment=_flag(post, "comment", True, "post"),
        post_labels=_flag(post, "labels", True, "post"),
        label_names=label_names,
        force_human_label=force_human.strip() or None,
        merge_method=merge_method,
        notify=tuple(_strings(auto.get("notify", []), "auto.notify")),
        ignore_checks=frozenset(_strings(auto.get("ignore_checks", []), "auto.ignore_checks")),
        config_path=_own_path(config_path) if config_path else None,
    )


def parse_config(text: str, config_path: str | None = None) -> Config:
    """Parse and validate the text of a tier map."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"the tier map is not valid TOML: {error}") from error
    return build_config(data, config_path=config_path)


# --------------------------------------------------------------------------- decision


@dataclass(frozen=True)
class ChangedFile:
    path: str
    previous_path: str | None = None  # set when the file was renamed
    additions: int = 0
    deletions: int = 0
    size_known: bool = True  # False when the host would not show the diff


@dataclass(frozen=True)
class Change:
    """A pull request or merge request, reduced to what the decision needs."""

    number: int
    author: str
    author_is_bot: bool = False
    draft: bool = False
    labels: frozenset[str] = frozenset()
    assignees: tuple[str, ...] = ()
    files: tuple[ChangedFile, ...] = ()
    files_truncated: bool = False
    head_sha: str | None = None
    target_branch: str | None = None
    is_open: bool = True  # False once it is closed or merged


@dataclass(frozen=True)
class Match:
    path: str
    tier: str
    pattern: str | None  # None when no rule matched and the default tier applied
    source: str  # "protected", "rules" or "default"
    area: str | None = None
    check: str | None = None
    additions: int = 0
    deletions: int = 0
    size_known: bool = True

    @property
    def changed_lines(self) -> int:
        return self.additions + self.deletions


@dataclass(frozen=True)
class Decision:
    tier: str  # the tier after escalations
    path_tier: str  # the tier from paths alone
    reviewers: int  # human reviewers this tier calls for
    escalations: tuple[str, ...]
    matches: tuple[Match, ...]  # one per changed file
    changed_files: int
    changed_lines: int
    size_known: bool
    author_trusted: bool
    author_is_agent: bool
    owners: tuple[str, ...]  # people assigned to an agent-opened change
    blockers: tuple[str, ...]  # why it cannot go through with no person, if any

    @property
    def no_human_needed(self) -> bool:
        """True when tier, author and state alone would let it through."""
        return not self.blockers

    def by_tier(self) -> dict[str, list[Match]]:
        grouped: dict[str, list[Match]] = {tier: [] for tier in TIERS}
        for match in self.matches:
            grouped[match.tier].append(match)
        for matches in grouped.values():
            matches.sort(key=lambda m: (-m.changed_lines, m.path))
        return grouped

    def unmatched(self) -> list[str]:
        return [match.path for match in self.matches if match.pattern is None]

    def summary(self) -> dict:
        """Counts only: no paths, logins or branch names."""
        grouped = self.by_tier()
        return {
            "tier": self.tier,
            "path_tier": self.path_tier,
            "reviewers": self.reviewers,
            "escalations": list(self.escalations),
            "files_by_tier": {tier: len(grouped[tier]) for tier in TIERS},
            "changed_files": self.changed_files,
            "changed_lines": self.changed_lines,
            "size_known": self.size_known,
            "author_trusted": self.author_trusted,
            "author_is_agent": self.author_is_agent,
        }


def classify_path(config: Config, path: str) -> Match:
    """The tier of one path: the first rule that matches, in precedence order."""
    for rule in config.rules:
        if rule.matches(path):
            return Match(
                path=path,
                tier=rule.tier,
                pattern=rule.pattern,
                source=rule.source,
                area=rule.area,
                check=rule.check,
            )
    return Match(path=path, tier=config.default_tier, pattern=None, source="default")


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def classify_change(config: Config, change: Change) -> Decision:
    """Decide a change's tier from its files, size and labels.

    The tier is the highest tier among its changed files (a rename counts under both
    its old and new path). A low-tier change is raised to medium when it is over the
    size limits, when its size cannot be measured, or when the force-human label is
    set. A change whose file list is incomplete is high.
    """
    matches: list[Match] = []
    for changed in change.files:
        candidates = [classify_path(config, changed.path)]
        if changed.previous_path and changed.previous_path != changed.path:
            candidates.append(classify_path(config, changed.previous_path))
        best = max(candidates, key=lambda match: RANK[match.tier])
        matches.append(
            Match(
                path=best.path,
                tier=best.tier,
                pattern=best.pattern,
                source=best.source,
                area=best.area,
                check=best.check,
                additions=changed.additions,
                deletions=changed.deletions,
                size_known=changed.size_known,
            )
        )

    escalations: list[str] = []
    if matches:
        path_tier = max((match.tier for match in matches), key=RANK.__getitem__)
    else:
        path_tier = config.default_tier
        escalations.append("no changed files were listed, so the default tier applies")
    tier = path_tier

    changed_files = len(change.files)
    changed_lines = sum(f.additions + f.deletions for f in change.files)
    size_known = all(f.size_known for f in change.files)

    if change.files_truncated:
        tier = "high"
        escalations.append(
            "the host did not list every changed file, so the change cannot be classified by path"
        )

    if tier == "low":
        reasons: list[str] = []
        limit = config.max_low_changed_lines
        if limit and not size_known:
            reasons.append(
                "the size of at least one file could not be measured, so the "
                "low-tier size limit cannot be checked"
            )
        elif limit and changed_lines > limit:
            reasons.append(f"{changed_lines} changed lines is over the low-tier limit of {limit}")
        limit = config.max_low_changed_files
        if limit and changed_files > limit:
            reasons.append(f"{changed_files} changed files is over the low-tier limit of {limit}")
        if reasons:
            tier = "medium"
            escalations.extend(reasons)

    if (
        config.force_human_label
        and config.force_human_label in change.labels
        and RANK[tier] < RANK["medium"]
    ):
        tier = "medium"
        escalations.append(f"the '{config.force_human_label}' label asks for a person")

    author_is_agent = config.is_agent(change.author, change.author_is_bot)
    author_trusted = config.is_trusted(change.author)
    owners = tuple(
        login for login in change.assignees if not config.is_agent(login) and login != change.author
    )
    reviewers = config.reviewers[tier]

    blockers: list[str] = []
    if reviewers > 0:
        blockers.append(f"{tier} risk calls for {plural(reviewers, 'reviewer')}")
    if not author_trusted:
        blockers.append("the author is not on the trusted list")
    if author_is_agent and not owners:
        blockers.append("an agent opened it and no person is assigned as its owner")

    return Decision(
        tier=tier,
        path_tier=path_tier,
        reviewers=reviewers,
        escalations=tuple(escalations),
        matches=tuple(matches),
        changed_files=changed_files,
        changed_lines=changed_lines,
        size_known=size_known,
        author_trusted=author_trusted,
        author_is_agent=author_is_agent,
        owners=owners,
        blockers=tuple(blockers),
    )


# ----------------------------------------------------------------------- agent review

SEVERITIES = ("important", "nit")


@dataclass(frozen=True)
class Finding:
    severity: str
    title: str
    path: str | None = None
    line: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class Focus:
    path: str
    why: str
    line: int | None = None


@dataclass(frozen=True)
class Review:
    """What a reviewing agent hands back. See docs/contract.md."""

    reviewed_sha: str | None = None
    tier: str | None = None  # the agent may raise the tier, never lower it
    tier_reason: str = ""
    summary: str = ""
    findings: tuple[Finding, ...] = ()
    focus: tuple[Focus, ...] = ()

    @property
    def important(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == "important")


def _opt_str(table: dict, key: str, where: str) -> str:
    value = table.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ReviewError(f"{where}.{key} must be a string")
    return value.strip()


def _opt_line(table: dict, where: str) -> int | None:
    value = table.get("line")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ReviewError(f"{where}.line must be a positive whole number")
    return value


def parse_review(data: object) -> Review:
    """Validate a review result. Raises ReviewError on anything unexpected."""
    if not isinstance(data, dict):
        raise ReviewError("the review result must be a JSON object")
    allowed = {"reviewed_sha", "tier", "tier_reason", "summary", "findings", "focus"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ReviewError(f"unknown key(s) in the review result: {', '.join(unknown)}")

    tier = data.get("tier")
    if tier is not None and tier not in TIERS:
        raise ReviewError(f"tier {tier!r} is not one of {', '.join(TIERS)}")

    findings: list[Finding] = []
    raw_findings = data.get("findings")
    if raw_findings is None:
        raw_findings = []
    if not isinstance(raw_findings, list):
        raise ReviewError("findings must be a list")
    for index, raw in enumerate(raw_findings, start=1):
        where = f"findings #{index}"
        if not isinstance(raw, dict):
            raise ReviewError(f"{where} must be an object")
        severity = raw.get("severity")
        if severity not in SEVERITIES:
            raise ReviewError(f"{where}.severity must be one of {', '.join(SEVERITIES)}")
        title = _opt_str(raw, "title", where)
        if not title:
            raise ReviewError(f"{where} needs a title")
        findings.append(
            Finding(
                severity=severity,
                title=title,
                path=_opt_str(raw, "path", where) or None,
                line=_opt_line(raw, where),
                detail=_opt_str(raw, "detail", where),
            )
        )

    focus: list[Focus] = []
    raw_focus = data.get("focus")
    if raw_focus is None:
        raw_focus = []
    if not isinstance(raw_focus, list):
        raise ReviewError("focus must be a list")
    for index, raw in enumerate(raw_focus, start=1):
        where = f"focus #{index}"
        if not isinstance(raw, dict):
            raise ReviewError(f"{where} must be an object")
        path, why = _opt_str(raw, "path", where), _opt_str(raw, "why", where)
        if not path or not why:
            raise ReviewError(f"{where} needs a path and a why")
        focus.append(Focus(path=path, why=why, line=_opt_line(raw, where)))

    return Review(
        reviewed_sha=_opt_str(data, "reviewed_sha", "review") or None,
        tier=tier,
        tier_reason=_opt_str(data, "tier_reason", "review"),
        summary=_opt_str(data, "summary", "review"),
        findings=tuple(findings),
        focus=tuple(focus),
    )


# ---------------------------------------------------------------------------- outcome


@dataclass(frozen=True)
class Checks:
    """The state of the other automated checks on the head commit."""

    state: str  # "green", "pending" or "failed"
    names: tuple[str, ...] = ()  # the pending or failed ones


@dataclass(frozen=True)
class Outcome:
    tier: str  # final tier, after the reviewing agent's raise
    reviewers: int
    raised_by_review: bool
    review_state: str  # "none", "stale", "clean" or "findings"
    action: str  # "recommend", "merge", "wait" or "hold"
    reasons: tuple[str, ...]  # why it is waiting or held


def review_state(change: Change, review: Review | None) -> str:
    if review is None:
        return "none"
    if not review.reviewed_sha or not change.head_sha:
        return "stale"
    if review.reviewed_sha != change.head_sha:
        return "stale"
    return "findings" if review.important else "clean"


def decide(
    config: Config,
    change: Change,
    decision: Decision,
    review: Review | None = None,
    checks: Checks | None = None,
) -> Outcome:
    """Combine the tier decision with the agent's review into what happens next.

    In recommend mode the action is always "recommend". In auto mode:

    - "hold": a person is needed (the risk is above low, the author is not trusted,
      the review found something important, or a check failed).
    - "wait": nothing is wrong yet but it is not ready (a draft, no review of the
      current commit, or checks still running).
    - "merge": low risk, trusted author, reviewed clean on this commit, checks green.
    """
    tier = decision.tier
    raised = False
    if review and review.tier and RANK[review.tier] > RANK[tier]:
        tier, raised = review.tier, True
    reviewers = config.reviewers[tier]
    state = review_state(change, review)

    if config.mode != "auto":
        return Outcome(tier, reviewers, raised, state, "recommend", ())

    hold: list[str] = []
    wait: list[str] = []
    if reviewers > 0:
        hold.append(f"{tier} risk calls for {plural(reviewers, 'reviewer')}")
    hold.extend(b for b in decision.blockers if "risk calls for" not in b)
    if state == "findings":
        count = review.important if review else 0
        hold.append(f"the review has {plural(count, 'important finding')}")
    elif state == "none":
        wait.append("no review of this change yet")
    elif state == "stale":
        wait.append("the review is of an older commit")
    if checks is not None:
        if checks.state == "failed":
            hold.append(f"checks failed: {', '.join(checks.names)}")
        elif checks.state == "pending":
            wait.append(f"checks still running: {', '.join(checks.names)}")

    if change.draft:
        return Outcome(tier, reviewers, raised, state, "wait", ("it is still a draft",))
    if hold:
        return Outcome(tier, reviewers, raised, state, "hold", tuple(hold))
    if wait:
        return Outcome(tier, reviewers, raised, state, "wait", tuple(wait))
    return Outcome(tier, reviewers, raised, state, "merge", ())
