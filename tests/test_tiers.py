"""Tests for the tier map, the path matcher, the decision and the outcome."""

import unittest
from pathlib import Path

from review_tiers import tiers as rt

BASE = """
default_tier = "medium"

[[rules]]
tier = "high"
area = "Infrastructure"
check = "Is the change safe to roll back?"
paths = ["infra/**"]

[[rules]]
tier = "high"
area = "Schema migrations"
paths = ["**/migrations/**", "src/auth/**"]

[[rules]]
tier = "medium"
paths = ["CLAUDE.md", "**/CLAUDE.md"]

[[rules]]
tier = "low"
area = "Documentation"
paths = ["docs/**", "**/*.md"]

[limits]
max_low_changed_lines = 100
max_low_changed_files = 5

[authors]
trusted = ["Alice"]
agents = ["helper-bot"]
"""


def config(extra="", mode=None):
    text = (f'mode = "{mode}"\n' if mode else "") + BASE + extra
    return rt.parse_config(text, config_path=".review-tiers.toml")


def change(paths, author="alice", **kwargs):
    files = tuple(
        item if isinstance(item, rt.ChangedFile) else rt.ChangedFile(item, additions=1)
        for item in paths
    )
    kwargs.setdefault("head_sha", "abc1234")
    return rt.Change(number=1, author=author, files=files, **kwargs)


class GlobTest(unittest.TestCase):
    def assert_glob(self, pattern, yes=(), no=()):
        regex = rt.compile_glob(pattern)
        for path in yes:
            self.assertTrue(regex.match(path), f"{pattern!r} should match {path!r}")
        for path in no:
            self.assertFalse(regex.match(path), f"{pattern!r} should not match {path!r}")

    def test_star_stays_inside_one_segment(self):
        self.assert_glob("*.md", yes=["README.md"], no=["docs/README.md", "README.mdx"])
        self.assert_glob("src/*.py", yes=["src/a.py"], no=["src/pkg/a.py", "a.py"])

    def test_question_mark_is_one_character(self):
        self.assert_glob("v?.txt", yes=["v1.txt"], no=["v12.txt", "v/.txt"])

    def test_leading_double_star_matches_zero_or_more_directories(self):
        self.assert_glob(
            "**/*.md", yes=["README.md", "docs/a.md", "a/b/c.md"], no=["README.markdown"]
        )
        self.assert_glob(
            "**/migrations/**",
            yes=["migrations/001.py", "app/db/migrations/versions/1.py"],
            no=["app/migrations.py", "app/migrations_old/1.py"],
        )

    def test_trailing_double_star_and_trailing_slash_mean_everything_below(self):
        for pattern in ("infra/**", "infra/"):
            self.assert_glob(
                pattern,
                yes=["infra/a.tf", "infra/x/y/z.tf"],
                no=["infra", "infrastructure/a.tf", "src/infra/a.tf"],
            )

    def test_double_star_in_the_middle(self):
        self.assert_glob(
            "src/**/test_*.py",
            yes=["src/test_a.py", "src/x/test_a.py", "src/x/y/test_a.py"],
            no=["test_a.py", "src/x/a.py"],
        )

    def test_patterns_are_anchored_and_literal(self):
        self.assert_glob("CODEOWNERS", yes=["CODEOWNERS"], no=["docs/CODEOWNERS"])
        self.assert_glob("a+b(1).txt", yes=["a+b(1).txt"], no=["aab1.txt"])
        self.assert_glob("docs/[draft]/**", yes=["docs/[draft]/a.md"], no=["docs/d/a.md"])

    def test_a_newline_in_a_path_does_not_slip_past_a_rule(self):
        self.assert_glob("infra/**", yes=["infra/a\nb.tf", "infra/x/\n/y"])
        self.assert_glob("**/*.md", yes=["docs/a\nb.md"])
        self.assert_glob("src/*.py", yes=["src/a\nb.py"], no=["src/a\n/b.py"])

    def test_bad_patterns_are_rejected(self):
        for bad in ("", "  ", "/abs/path", "win\\path"):
            with self.assertRaises(rt.ConfigError):
                rt.compile_glob(bad)


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = rt.parse_config("")
        self.assertEqual((cfg.mode, cfg.default_tier), ("recommend", "medium"))
        self.assertEqual(cfg.reviewers, {"low": 0, "medium": 1, "high": 2})
        self.assertEqual(cfg.trusted_authors, frozenset())
        self.assertEqual(cfg.merge_method, "squash")
        self.assertTrue(cfg.post_comment and cfg.post_labels)
        self.assertEqual(cfg.force_human_label, "needs-human")

    def test_label_names_follow_the_host_unless_set(self):
        cfg = rt.parse_config("")
        self.assertEqual(cfg.label("high", ":"), "risk:high")
        self.assertEqual(cfg.label("high", "::"), "risk::high")
        self.assertEqual(cfg.label("disagree", "::"), "risk::disagree")
        named = rt.parse_config('[labels]\nhigh = "tier-3"\nforce_human = ""\n')
        self.assertEqual(named.label("high", "::"), "tier-3")
        self.assertEqual(named.label("low", "::"), "risk::low")
        self.assertIsNone(named.force_human_label)

    def test_unknown_keys_and_bad_values_are_refused(self):
        cases = [
            'mode = "enforce"',
            "version = 2",
            'default_tier = "none"',
            "[tiers.high]\npaths = []",
            '[[rules]]\npaths = ["a"]',
            '[[rules]]\ntier = "critical"\npaths = ["a"]',
            '[[rules]]\ntier = "high"\npaths = "a"',
            '[[rules]]\ntier = "high"\npaths = []',
            '[[rules]]\ntier = "high"\npaths = ["a"]\nhint = "x"',
            "[limits]\nmax_low_changed_lines = 0",
            "[limits]\nmax_lines = 5",
            "[reviewers]\nlow = -1",
            "[reviewers]\nlow = 2\nmedium = 1",
            '[post]\ncomment = "yes"',
            '[labels]\nlow = "x"\nmedium = "x"',
            '[auto]\nmerge_method = "fast"',
            '[auto]\nnotify = "me"',
            "mode = ",
        ]
        for text in cases:
            with self.assertRaises(rt.ConfigError, msg=text):
                rt.parse_config(text)

    def test_logins_compare_without_case(self):
        cfg = config()
        self.assertTrue(cfg.is_trusted("ALICE"))
        self.assertTrue(cfg.is_agent("Helper-Bot"))
        self.assertTrue(cfg.is_agent("anything", is_bot=True))
        self.assertFalse(cfg.is_agent("alice"))

    def test_the_shipped_template_is_valid_and_strict_by_default(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "templates" / "review-tiers.toml").read_text(encoding="utf-8")
        cfg = rt.parse_config(text, config_path=".review-tiers.toml")
        self.assertEqual((cfg.mode, cfg.default_tier), ("recommend", "medium"))
        self.assertEqual(cfg.trusted_authors, frozenset())
        self.assertEqual(rt.classify_path(cfg, "docs/guide.md").tier, "low")
        self.assertEqual(rt.classify_path(cfg, "src/tests/test_app.py").tier, "medium")
        self.assertEqual(rt.classify_path(cfg, "AGENTS.md").tier, "medium")


class ClassifyPathTest(unittest.TestCase):
    def test_stricter_rule_wins_and_carries_its_hints(self):
        cfg = config()
        match = rt.classify_path(cfg, "infra/README.md")
        self.assertEqual((match.tier, match.area), ("high", "Infrastructure"))
        self.assertEqual(match.check, "Is the change safe to roll back?")
        self.assertEqual(rt.classify_path(cfg, "pkg/CLAUDE.md").tier, "medium")
        docs = rt.classify_path(cfg, "docs/guide.md")
        self.assertEqual((docs.tier, docs.area, docs.check), ("low", "Documentation", None))

    def test_first_written_rule_wins_within_a_tier(self):
        cfg = rt.parse_config(
            '[[rules]]\ntier = "high"\narea = "First"\npaths = ["a/**"]\n'
            '[[rules]]\ntier = "low"\npaths = ["a/**"]\n'
            '[[rules]]\ntier = "high"\narea = "Second"\npaths = ["a/b/**"]\n'
        )
        self.assertEqual(rt.classify_path(cfg, "a/b/c").area, "First")

    def test_unmatched_paths_get_the_default_tier(self):
        match = rt.classify_path(config(), "src/app.py")
        self.assertEqual((match.tier, match.pattern, match.source), ("medium", None, "default"))

    def test_the_gate_and_its_inputs_are_always_high(self):
        # Even a map that calls everything low cannot lower these.
        cfg = rt.parse_config(
            'default_tier = "low"\n[[rules]]\ntier = "low"\npaths = ["**"]\n',
            config_path="./.review-tiers.toml",
        )
        for path in (
            ".github/workflows/ci.yml",
            ".github/actions/setup/action.yml",
            ".gitlab-ci.yml",
            ".gitlab/CODEOWNERS",
            ".gitlab/ci/build.yml",
            ".review-tiers.toml",
            "CODEOWNERS",
            "docs/CODEOWNERS",
            "REVIEW.md",
            "services/billing/REVIEW.md",
        ):
            match = rt.classify_path(cfg, path)
            self.assertEqual((match.tier, match.source), ("high", "protected"), path)
            self.assertEqual(match.area, rt.PROTECTED_AREA)
        self.assertEqual(rt.classify_path(cfg, "src/app.py").tier, "low")


class ClassifyChangeTest(unittest.TestCase):
    def test_tier_is_the_highest_among_the_files(self):
        cfg = config()
        self.assertEqual(rt.classify_change(cfg, change(["docs/a.md"])).tier, "low")
        decision = rt.classify_change(cfg, change(["docs/a.md", "src/app.py", "infra/main.tf"]))
        self.assertEqual((decision.tier, decision.reviewers), ("high", 2))
        grouped = decision.by_tier()
        self.assertEqual([len(grouped[t]) for t in rt.TIERS], [1, 1, 1])
        self.assertEqual(decision.unmatched(), ["src/app.py"])

    def test_files_in_a_tier_are_ordered_largest_first(self):
        files = [
            rt.ChangedFile("infra/small.tf", additions=1),
            rt.ChangedFile("infra/big.tf", additions=40, deletions=2),
        ]
        grouped = rt.classify_change(config(), change(files)).by_tier()
        self.assertEqual([m.path for m in grouped["high"]], ["infra/big.tf", "infra/small.tf"])
        self.assertEqual(grouped["high"][0].changed_lines, 42)

    def test_a_rename_counts_under_its_old_path_too(self):
        moved = rt.ChangedFile("docs/old-auth.md", previous_path="src/auth/session.py")
        decision = rt.classify_change(config(), change([moved]))
        self.assertEqual(decision.tier, "high")
        self.assertEqual(decision.matches[0].path, "src/auth/session.py")

    def test_low_tier_changes_over_the_size_limits_need_a_person(self):
        cfg = config()
        big = rt.ChangedFile("docs/a.md", additions=90, deletions=11)
        decision = rt.classify_change(cfg, change([big]))
        self.assertEqual((decision.path_tier, decision.tier), ("low", "medium"))
        self.assertIn("101 changed lines", decision.escalations[0])
        many = [f"docs/{n}.md" for n in range(6)]
        self.assertIn("6 changed files", rt.classify_change(cfg, change(many)).escalations[0])
        at_limit = rt.ChangedFile("docs/a.md", additions=100)
        self.assertEqual(rt.classify_change(cfg, change([at_limit])).tier, "low")

    def test_unknown_size_is_not_treated_as_small(self):
        hidden = rt.ChangedFile("docs/a.md", size_known=False)
        decision = rt.classify_change(config(), change([hidden]))
        self.assertEqual(decision.tier, "medium")
        self.assertFalse(decision.size_known)
        self.assertIn("could not be measured", decision.escalations[0])
        # With no size limit configured there is nothing to check.
        no_limit = rt.parse_config('[[rules]]\ntier = "low"\npaths = ["docs/**"]\n')
        self.assertEqual(rt.classify_change(no_limit, change([hidden])).tier, "low")

    def test_size_never_changes_medium_or_high(self):
        huge = rt.ChangedFile("src/app.py", additions=5000)
        self.assertEqual(rt.classify_change(config(), change([huge])).tier, "medium")

    def test_incomplete_file_list_is_high(self):
        decision = rt.classify_change(config(), change(["docs/a.md"], files_truncated=True))
        self.assertEqual(decision.tier, "high")
        self.assertIn("did not list every changed file", decision.escalations[0])

    def test_no_files_uses_the_default_tier(self):
        decision = rt.classify_change(config(), change([]))
        self.assertEqual(decision.tier, "medium")
        self.assertTrue(decision.escalations)

    def test_force_human_label_lifts_low_to_medium_only(self):
        cfg = config()
        labelled = frozenset({"needs-human"})
        self.assertEqual(
            rt.classify_change(cfg, change(["docs/a.md"], labels=labelled)).tier, "medium"
        )
        self.assertEqual(
            rt.classify_change(cfg, change(["infra/a.tf"], labels=labelled)).tier, "high"
        )

    def test_blockers(self):
        cfg = config()
        self.assertTrue(rt.classify_change(cfg, change(["docs/a.md"])).no_human_needed)
        stranger = rt.classify_change(cfg, change(["docs/a.md"], author="mallory"))
        self.assertEqual(stranger.blockers, ("the author is not on the trusted list",))
        high = rt.classify_change(cfg, change(["infra/a.tf"]))
        self.assertEqual(high.blockers, ("high risk calls for 2 reviewers",))
        medium = rt.classify_change(cfg, change(["src/app.py"]))
        self.assertEqual(medium.blockers, ("medium risk calls for 1 reviewer",))

    def test_agent_opened_changes_need_a_person_assigned(self):
        cfg = rt.parse_config(BASE.replace('trusted = ["Alice"]', 'trusted = ["helper-bot"]'))
        orphan = rt.classify_change(cfg, change(["docs/a.md"], author="helper-bot"))
        self.assertTrue(orphan.author_is_agent)
        self.assertIn("no person is assigned", orphan.blockers[0])
        owned = rt.classify_change(
            cfg, change(["docs/a.md"], author="helper-bot", assignees=("bob", "helper-bot"))
        )
        self.assertEqual(owned.owners, ("bob",))
        self.assertTrue(owned.no_human_needed)
        by_flag = rt.classify_change(cfg, change(["docs/a.md"], author="x", author_is_bot=True))
        self.assertTrue(by_flag.author_is_agent)

    def test_summary_has_counts_but_no_paths_or_names(self):
        summary = rt.classify_change(
            config(), change(["docs/a.md", "src/secret_name.py"])
        ).summary()
        self.assertEqual(summary["files_by_tier"], {"low": 1, "medium": 1, "high": 0})
        self.assertNotIn("secret_name", str(summary))
        self.assertNotIn("alice", str(summary))


class ReviewTest(unittest.TestCase):
    def test_a_full_review_result(self):
        review = rt.parse_review(
            {
                "reviewed_sha": "abc1234",
                "tier": "high",
                "tier_reason": "touches token handling",
                "summary": "Looks fine.",
                "findings": [
                    {"severity": "nit", "title": "typo"},
                    {
                        "severity": "important",
                        "title": "leak",
                        "path": "a.py",
                        "line": 3,
                        "detail": "d",
                    },
                ],
                "focus": [{"path": "a.py", "line": 9, "why": "new query"}],
            }
        )
        self.assertEqual((review.tier, review.important), ("high", 1))
        self.assertEqual(review.findings[1].line, 3)
        self.assertEqual(review.focus[0].why, "new query")

    def test_an_empty_object_is_a_review_with_nothing_in_it(self):
        review = rt.parse_review({})
        self.assertEqual((review.tier, review.important, review.reviewed_sha), (None, 0, None))

    def test_malformed_results_are_refused(self):
        cases = [
            [],
            "text",
            {"verdict": "clean"},
            {"tier": "critical"},
            {"findings": {}},
            {"findings": ["x"]},
            {"findings": [{"severity": "blocker", "title": "x"}]},
            {"findings": [{"severity": "nit"}]},
            {"findings": [{"severity": "nit", "title": "x", "line": 0}]},
            {"findings": [{"severity": "nit", "title": "x", "line": True}]},
            {"focus": [{"path": "a.py"}]},
            {"summary": 5},
        ]
        for data in cases:
            with self.assertRaises(rt.ReviewError, msg=str(data)):
                rt.parse_review(data)


class DecideTest(unittest.TestCase):
    def outcome(self, paths, review=None, checks=None, mode="auto", **change_kwargs):
        cfg = config(mode=mode)
        the_change = change(paths, **change_kwargs)
        decision = rt.classify_change(cfg, the_change)
        return rt.decide(cfg, the_change, decision, review, checks)

    CLEAN = rt.Review(reviewed_sha="abc1234")
    GREEN = rt.Checks("green")

    def test_recommend_mode_only_ever_recommends(self):
        for paths in (["docs/a.md"], ["infra/a.tf"]):
            outcome = self.outcome(paths, self.CLEAN, self.GREEN, mode="recommend")
            self.assertEqual(outcome.action, "recommend")
        self.assertEqual(self.outcome(["infra/a.tf"], mode="recommend").reviewers, 2)

    def test_auto_merges_only_low_risk_reviewed_clean_and_green(self):
        outcome = self.outcome(["docs/a.md"], self.CLEAN, self.GREEN)
        self.assertEqual((outcome.action, outcome.reasons), ("merge", ()))
        self.assertEqual(outcome.review_state, "clean")

    def test_auto_holds_anything_above_low(self):
        outcome = self.outcome(["src/app.py"], self.CLEAN, self.GREEN)
        self.assertEqual(outcome.action, "hold")
        self.assertEqual(outcome.reasons, ("medium risk calls for 1 reviewer",))

    def test_auto_holds_on_important_findings_and_untrusted_authors(self):
        found = rt.Review(
            reviewed_sha="abc1234", findings=(rt.Finding("important", "x"), rt.Finding("nit", "y"))
        )
        outcome = self.outcome(["docs/a.md"], found, self.GREEN)
        self.assertEqual((outcome.action, outcome.review_state), ("hold", "findings"))
        self.assertEqual(outcome.reasons, ("the review has 1 important finding",))
        nits_only = rt.Review(reviewed_sha="abc1234", findings=(rt.Finding("nit", "y"),))
        self.assertEqual(self.outcome(["docs/a.md"], nits_only, self.GREEN).action, "merge")
        stranger = self.outcome(["docs/a.md"], self.CLEAN, self.GREEN, author="mallory")
        self.assertEqual(stranger.reasons, ("the author is not on the trusted list",))

    def test_auto_waits_without_a_review_of_the_current_commit(self):
        none = self.outcome(["docs/a.md"], None, self.GREEN)
        self.assertEqual((none.action, none.review_state), ("wait", "none"))
        old = rt.Review(reviewed_sha="0ld0ld0")
        stale = self.outcome(["docs/a.md"], old, self.GREEN)
        self.assertEqual((stale.action, stale.review_state), ("wait", "stale"))
        self.assertEqual(stale.reasons, ("the review is of an older commit",))
        unpinned = self.outcome(["docs/a.md"], rt.Review(), self.GREEN)
        self.assertEqual(unpinned.review_state, "stale")

    def test_auto_waits_on_pending_checks_and_holds_on_failed_ones(self):
        pending = self.outcome(["docs/a.md"], self.CLEAN, rt.Checks("pending", ("build",)))
        self.assertEqual(
            (pending.action, pending.reasons), ("wait", ("checks still running: build",))
        )
        failed = self.outcome(["docs/a.md"], self.CLEAN, rt.Checks("failed", ("build", "lint")))
        self.assertEqual((failed.action, failed.reasons), ("hold", ("checks failed: build, lint",)))

    def test_a_draft_only_ever_waits(self):
        outcome = self.outcome(["infra/a.tf"], self.CLEAN, self.GREEN, draft=True)
        self.assertEqual((outcome.action, outcome.reasons), ("wait", ("it is still a draft",)))

    def test_hold_reasons_win_over_wait_reasons(self):
        outcome = self.outcome(["src/app.py"], None, rt.Checks("pending", ("build",)))
        self.assertEqual(outcome.action, "hold")

    def test_the_review_can_raise_the_tier_but_never_lower_it(self):
        raise_it = rt.Review(reviewed_sha="abc1234", tier="high", tier_reason="r")
        outcome = self.outcome(["docs/a.md"], raise_it, self.GREEN)
        self.assertEqual(
            (outcome.tier, outcome.raised_by_review, outcome.action), ("high", True, "hold")
        )
        self.assertEqual(outcome.reasons, ("high risk calls for 2 reviewers",))
        lower_it = rt.Review(reviewed_sha="abc1234", tier="low")
        outcome = self.outcome(["infra/a.tf"], lower_it, self.GREEN)
        self.assertEqual((outcome.tier, outcome.raised_by_review), ("high", False))
        stale_raise = rt.Review(reviewed_sha="0ld0ld0", tier="high")
        self.assertEqual(self.outcome(["docs/a.md"], stale_raise, mode="recommend").tier, "high")


if __name__ == "__main__":
    unittest.main()
