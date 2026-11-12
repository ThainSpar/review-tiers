"""Tests for the history replay: review facts, reverts, the numbers and the report."""

import contextlib
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from review_tiers import cli
from review_tiers import replay as retro
from review_tiers import tiers as rt
from review_tiers.http import Api

from .fakes import GITHUB, FakeTransport

REPO = "acme/widgets"
NOW = datetime(2026, 11, 1, tzinfo=UTC)
TIER_MAP = """
[[rules]]
tier = "high"
paths = ["infra/**"]
[[rules]]
tier = "low"
paths = ["docs/**"]
[limits]
max_low_changed_lines = 50
[authors]
trusted = ["alice"]
agents = ["helper-bot"]
"""


def cfg(text=TIER_MAP):
    return rt.parse_config(text, config_path=".review-tiers.toml")


def stamp(days_ago=0.0, hours=0.0):
    moment = NOW - timedelta(days=days_ago) + timedelta(hours=hours)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def pull(number, days_ago=5, open_hours=10, login="alice", title=None, body="", sha=None):
    return {
        "number": number,
        "title": title or f"Change {number}",
        "body": body,
        "user": {"login": login, "type": "User"},
        "labels": [],
        "assignees": [],
        "head": {"sha": sha or f"sha{number}"},
        "created_at": stamp(days_ago, -open_hours),
        "merged_at": stamp(days_ago),
        "updated_at": stamp(days_ago),
    }


def review(
    login,
    state,
    hours_after_open=1.0,
    commit="sha1",
    kind="User",
    days_ago=5,
    open_hours=10,
):
    return {
        "user": {"login": login, "type": kind},
        "state": state,
        "commit_id": commit,
        "submitted_at": stamp(days_ago, -open_hours + hours_after_open),
    }


def changed(name, additions=1, deletions=0):
    return {"filename": name, "additions": additions, "deletions": deletions}


class ReviewFactsTest(unittest.TestCase):
    def facts(self, reviews, author="alice", head="sha1"):
        return retro.summarize_reviews(reviews, author, cfg(), head)

    def test_no_reviews(self):
        facts = self.facts([])
        self.assertEqual((facts.human_reviewers, facts.human_approvals), (0, 0))
        self.assertIsNone(facts.stale_approval)
        self.assertIsNone(facts.first_human_review_at)

    def test_bots_listed_agents_and_the_author_are_not_human_reviews(self):
        facts = self.facts(
            [
                review("claude[bot]", "COMMENTED", kind="Bot"),
                review("Helper-Bot", "APPROVED"),
                review("alice", "COMMENTED"),
                review("someone", "PENDING"),
            ]
        )
        self.assertEqual(facts.human_reviewers, 0)
        self.assertEqual(facts.human_approvals, 0)
        self.assertEqual(facts.agent_reviews, 2)
        self.assertFalse(facts.human_feedback)

    def test_feedback_and_approval_states(self):
        facts = self.facts(
            [
                review("bob", "COMMENTED", 2),
                review("carol", "CHANGES_REQUESTED", 3),
                review("carol", "APPROVED", 6),
                review("dave", "APPROVED", 4),
                review("dave", "CHANGES_REQUESTED", 7),
            ]
        )
        self.assertEqual(facts.human_reviewers, 3)
        self.assertTrue(facts.human_feedback and facts.human_changes_requested)
        # carol ended on an approval, dave on a change request, bob only commented
        self.assertEqual(facts.human_approvals, 1)
        self.assertEqual(facts.first_human_review_at, retro.parse_time(stamp(5, -8)))

    def test_a_later_comment_does_not_undo_an_approval(self):
        facts = self.facts([review("bob", "APPROVED", 2), review("bob", "COMMENTED", 3)])
        self.assertEqual(facts.human_approvals, 1)

    def test_approval_is_stale_unless_some_approval_is_on_the_merged_commit(self):
        old = review("bob", "APPROVED", 2, commit="older")
        self.assertTrue(self.facts([old], head="sha1").stale_approval)
        fresh = review("carol", "APPROVED", 3, commit="sha1")
        self.assertFalse(self.facts([old, fresh], head="sha1").stale_approval)

    def test_review_rounds_count_distinct_reviewed_commits(self):
        rounds = [
            review("claude[bot]", "COMMENTED", 1, commit="c1", kind="Bot"),
            review("claude[bot]", "COMMENTED", 2, commit="c2", kind="Bot"),
            review("bob", "APPROVED", 3, commit="c2"),
            review("claude[bot]", "COMMENTED", 4, commit="c3", kind="Bot"),
        ]
        self.assertEqual(self.facts(rounds).review_rounds, 3)


class RevertsTest(unittest.TestCase):
    def test_body_reference_marks_both_sides(self):
        pulls = [
            pull(12, 9),
            pull(15, 3, body="Reverts acme/widgets#12\n\nBroke login."),
        ]
        self.assertEqual(retro.find_reverts(pulls), ({12}, {15}))

    def test_short_reference_and_target_outside_the_window(self):
        pulls = [pull(15, 3, body="reverts #4")]
        self.assertEqual(retro.find_reverts(pulls), (set(), {15}))

    def test_title_match_finds_the_earlier_pull_request(self):
        pulls = [
            pull(20, 9, title="Add export button (#20)"),
            pull(21, 8, title="Unrelated"),
            pull(22, 2, title='Revert "Add export button"'),
        ]
        self.assertEqual(retro.find_reverts(pulls), ({20}, {22}))

    def test_revert_title_without_a_match_is_still_a_revert(self):
        self.assertEqual(retro.find_reverts([pull(30, 2, title='Revert "Gone"')]), (set(), {30}))

    def test_ordinary_pull_requests_are_not_reverts(self):
        pulls = [pull(1, 3, title="Revert button styling to blue", body="See #2 for context")]
        self.assertEqual(retro.find_reverts(pulls), (set(), set()))

    def test_a_pull_request_cannot_revert_itself(self):
        self.assertEqual(retro.find_reverts([pull(7, 3, body="Reverts #7")]), (set(), set()))


def record(number, files, reviews=(), **pull_kwargs):
    return retro.build_record(pull(number, **pull_kwargs), list(files), list(reviews), cfg())


class AggregateTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            record(1, [changed("docs/a.md")], open_hours=2),
            record(
                2,
                [changed("docs/b.md")],
                [review("bob", "CHANGES_REQUESTED", 1, open_hours=4)],
                open_hours=4,
            ),
            record(3, [changed("docs/c.md", 80)], open_hours=6),  # low by path, raised by size
            record(
                4,
                [changed("src/app.py")],
                [review("bob", "APPROVED", 1, commit="sha4", open_hours=20)],
                open_hours=20,
            ),
            record(
                5,
                [changed("src/x.py")],
                [review("bob", "APPROVED", 3, commit="older", open_hours=30)],
                open_hours=30,
            ),
            record(6, [changed("infra/main.tf")], login="mallory", open_hours=40),
            record(7, [changed("docs/d.md")], login="mallory", open_hours=8),
        ]
        self.records[0].reverted = True
        self.summary = retro.aggregate(self.records, cfg())

    def test_tier_counts_and_shares(self):
        tiers = self.summary["tiers"]
        self.assertEqual([tiers[t]["count"] for t in rt.TIERS], [3, 3, 1])
        self.assertEqual(tiers["low"]["share"], round(3 / 7, 4))
        self.assertEqual(tiers["low"]["median_hours_to_merge"], 4.0)
        self.assertEqual(tiers["medium"]["median_changed_lines"], 1.0)
        self.assertEqual(tiers["high"]["no_human_review"], {"count": 1, "share": 1.0})

    def test_no_person_numbers(self):
        zero = self.summary["no_person"]
        self.assertEqual(zero["low_tier"]["count"], 3)
        self.assertEqual(zero["eligible"]["count"], 2)  # alice's two low-tier changes
        self.assertEqual(zero["raised_from_low_by_size_or_label"], 1)
        self.assertFalse(zero["trusted_list_empty"])

    def test_low_tier_checks_count_what_a_no_person_merge_would_have_skipped(self):
        low = self.summary["low_tier_checks"]
        self.assertEqual(low["count"], 3)
        self.assertEqual(low["human_requested_changes"]["count"], 1)
        self.assertEqual(low["human_left_feedback"]["count"], 1)
        self.assertEqual(low["reverted"]["count"], 1)

    def test_review_today(self):
        today = self.summary["today"]
        self.assertEqual(today["no_human_review"]["count"], 4)
        self.assertEqual(today["stale_approval"], {"count": 1, "share": 0.5, "of_approved": 2})
        self.assertEqual(today["reverted"]["count"], 1)
        self.assertEqual(today["median_hours_to_merge"], 8.0)
        self.assertEqual(today["median_hours_to_first_human_review"], 1.0)

    def test_empty_trusted_list_is_flagged(self):
        summary = retro.aggregate(
            self.records, cfg(TIER_MAP.replace('trusted = ["alice"]', "trusted = []"))
        )
        self.assertTrue(summary["no_person"]["trusted_list_empty"])

    def test_no_records(self):
        summary = retro.aggregate([], cfg())
        self.assertEqual(summary["merged"], 0)
        self.assertIsNone(summary["tiers"]["low"]["share"])
        report = retro.render_report(summary, "this repository", 90, "main", cfg())
        self.assertIn("nothing to report", report)


class NumbersTest(unittest.TestCase):
    def test_percentile_interpolates(self):
        self.assertIsNone(retro.percentile([], 0.9))
        self.assertEqual(retro.percentile([5.0], 0.9), 5.0)
        self.assertEqual(retro.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11], 0.9), 10.0)
        self.assertEqual(retro.percentile([0.0, 10.0], 0.9), 9.0)

    def test_formatting(self):
        self.assertEqual(retro.pct(None), "n/a")
        self.assertEqual(retro.pct(0), "0%")
        self.assertEqual(retro.pct(1.0), "100%")
        self.assertEqual(retro.pct(0.3333), "33.3%")
        self.assertEqual(retro.num(None), "n/a")
        self.assertEqual(retro.num(4.0), "4")
        self.assertEqual(retro.num(12.25), "12.25")


class BreakerTest(unittest.TestCase):
    def summary(self, count, reverted):
        return {"low_tier_checks": {"count": count, "reverted": {"count": reverted}}}

    def test_small_samples_are_not_judged(self):
        tripped, message = retro.breaker(self.summary(5, 5), 0.05, 20)
        self.assertFalse(tripped)
        self.assertIn("not judged", message)

    def test_trips_only_above_the_limit(self):
        self.assertEqual(retro.breaker(self.summary(40, 2), 0.05, 20)[0], False)
        tripped, message = retro.breaker(self.summary(40, 3), 0.05, 20)
        self.assertTrue(tripped)
        self.assertIn("TRIPPED", message)
        self.assertIn("7.5%", message)


class ReportTest(unittest.TestCase):
    SECRET_PATH = "src/payments/quarterly_forecast_engine.py"

    def records(self):
        records = [
            record(
                41,
                [changed(self.SECRET_PATH), changed("docs/a.md")],
                [review("reviewer-rita", "CHANGES_REQUESTED")],
                login="author-otto",
                title="Rework the forecast engine",
            ),
            record(
                42,
                [changed("docs/b.md")],
                [review("reviewer-rita", "CHANGES_REQUESTED")],
            ),
        ]
        records[1].reverted = True
        return records

    def test_default_report_and_json_name_nothing(self):
        records = self.records()
        summary = retro.aggregate(records, cfg())
        report = retro.render_report(summary, "this repository", 30, "main", cfg())
        blob = report + json.dumps(summary)
        for secret in (
            self.SECRET_PATH,
            "payments",
            "author-otto",
            "reviewer-rita",
            "forecast",
            "#41",
            "#42",
            REPO,
        ):
            self.assertNotIn(secret, blob)
        self.assertIn("# Review baseline: last 30 days", report)
        self.assertIn("| low | 1 | 50.0% |", report)
        self.assertIn("a person requested changes on 1", report)
        self.assertIn("1 was later reverted", report)

    def test_tuning_section_names_paths_and_numbers_and_says_so(self):
        text = retro.render_tuning(self.records())
        self.assertIn("internal: names paths and pull requests", text)
        self.assertIn("`src/payments/`: 1", text)
        self.assertIn("`docs/**`: 2", text)
        self.assertIn("A person requested changes: #42", text)
        self.assertIn("Later reverted: #42", text)


class FetchTest(unittest.TestCase):
    def test_list_merged_keeps_the_window_and_stops_paging_when_older(self):
        since = NOW - timedelta(days=30)
        recent = [pull(n, days_ago=n % 20) for n in range(1, 101)]  # a full first page
        closed_unmerged = dict(pull(500, 1), merged_at=None)
        old = [pull(900 + n, days_ago=60 + n) for n in range(5)]
        page_one = sorted(
            recent[:99] + [closed_unmerged], key=lambda p: p["updated_at"], reverse=True
        )
        fake = FakeTransport({f"repos/{REPO}/pulls": page_one + old})
        found = retro.list_merged(Api(GITHUB, {}, fake), REPO, "main", since, None)
        self.assertEqual(len(found), 99)
        self.assertTrue(all(item["merged_at"] >= stamp(30) for item in found))
        self.assertEqual(len(fake.calls), 2)  # second page was short and all old
        self.assertEqual(
            [p["merged_at"] for p in found],
            sorted((p["merged_at"] for p in found), reverse=True),
        )
        capped = retro.list_merged(Api(GITHUB, {}, fake), REPO, "main", since, 10)
        self.assertEqual(len(capped), 10)

    def run_main(self, fake, args=()):
        with tempfile.TemporaryDirectory() as root:
            json_path = Path(root) / "out.json"
            argv = ["replay", "--host", "github", "--repo", REPO, "--days", "30"]
            parsed = cli.build_parser().parse_args([*argv, "--json", str(json_path), *args])
            parsed.now = NOW
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = retro.run(parsed, {"GITHUB_TOKEN": "t"}, fake)
                except Exception as error:  # the command line maps these to exit codes
                    code, _ = type(error).__name__, print(error, file=err)
            saved = json.loads(json_path.read_text()) if json_path.exists() else None
        return code, out.getvalue(), err.getvalue(), saved

    def fake(self):
        pulls = [
            pull(3, 2, body="Reverts #1"),
            pull(2, 4),
            pull(1, 6),
        ]
        return FakeTransport(
            {
                f"repos/{REPO}/contents/.review-tiers.toml": TIER_MAP,
                f"repos/{REPO}": {"default_branch": "trunk"},
                f"repos/{REPO}/pulls": pulls,
                f"repos/{REPO}/pulls/1/files": [changed("docs/a.md")],
                f"repos/{REPO}/pulls/2/files": [changed("src/app.py")],
                f"repos/{REPO}/pulls/3/files": [changed("docs/a.md")],
                f"repos/{REPO}/pulls/1/reviews": [],
                f"repos/{REPO}/pulls/2/reviews": [review("bob", "APPROVED", commit="sha2")],
                f"repos/{REPO}/pulls/3/reviews": [],
            }
        )

    def test_main_end_to_end_reads_only(self):
        fake = self.fake()
        code, out, err, saved = self.run_main(fake, ["--tuning"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [])
        self.assertIn("3 pull requests merged into `trunk` in this repository", out)
        self.assertIn("| low | 2 | 66.7% |", out)
        self.assertIn("Later reverted: #1", out)
        self.assertIn("3 merged pull requests to replay", err)
        self.assertEqual(saved["merged"], 3)
        self.assertEqual(saved["today"]["reverted"]["count"], 1)
        self.assertEqual(saved["today"]["revert_pull_requests"], 1)
        self.assertNotIn(REPO, out)

    def test_breaker_exit_code(self):
        code, out, _, saved = self.run_main(
            self.fake(),
            ["--breaker-max-revert-rate", "0.1", "--breaker-min-sample", "2"],
        )
        self.assertEqual(code, 3)
        self.assertIn("TRIPPED", out)
        self.assertTrue(saved["breaker"]["tripped"])

    def test_name_repo_flag(self):
        _, out, _, _ = self.run_main(self.fake(), ["--name-repo"])
        self.assertIn(f"in `{REPO}`", out)

    def test_api_failure_and_missing_map(self):
        code, _, err, _ = self.run_main(FakeTransport({}))
        self.assertEqual(code, "ApiError")
        self.assertIn("HTTP 404", err)
        fake = self.fake()
        del fake.routes[f"repos/{REPO}/contents/.review-tiers.toml"]
        code, _, err, _ = self.run_main(fake)
        self.assertEqual(code, "ConfigError")
        self.assertIn("no tier map at .review-tiers.toml on branch trunk", err)

    def test_through_the_command_line(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(
                ["replay", "--host", "github", "--repo", REPO, "--days", "36500"],
                env={"GITHUB_TOKEN": "t"},
                transport=self.fake(),
            )
        self.assertEqual(code, 0)
        self.assertIn("# Review baseline: last 36500 days", out.getvalue())
        with contextlib.redirect_stderr(err):
            code = cli.main(
                ["replay", "--host", "github", "--repo", REPO],
                env={"GITHUB_TOKEN": "t"},
                transport=FakeTransport({}),
            )
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
