"""End-to-end tests for brief and publish, and for the guide text, through the
command line against a stand-in for the network."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from review_tiers import cli, guide
from review_tiers import tiers as rt

from .fakes import GITLAB, FakeTransport

REPO = "acme/widgets"
R = f"repos/{REPO}"
P = "projects/acme%2Fwidgets"
SHA = "abc1234def"

TIER_MAP = """
mode = "{mode}"

[[rules]]
tier = "high"
area = "Schema migrations"
check = "Can it be rolled back?"
paths = ["**/migrations/**"]

[[rules]]
tier = "low"
area = "Documentation"
paths = ["docs/**"]

[limits]
max_low_changed_lines = 50

[authors]
trusted = ["alice"]

[auto]
notify = ["alice"]
ignore_checks = ["Review"]
"""


# ----------------------------------------------------------------------- fake hosts


def github_routes(files, mode="auto", comments=(), runs=(), me="alice", **pr_extra):
    pr = {
        "number": 7,
        "user": {"login": "alice", "type": "User"},
        "draft": False,
        "labels": [],
        "assignees": [],
        "head": {"sha": SHA},
        "base": {"ref": "main"},
    }
    pr.update(pr_extra)
    return {
        f"{R}/pulls/7": pr,
        f"{R}/pulls/7/files": [
            {"filename": name, "additions": adds, "deletions": 0} for name, adds in files
        ],
        f"{R}/contents/.review-tiers.toml": TIER_MAP.format(mode=mode),
        f"{R}/issues/7/comments": list(comments),
        f"{R}/commits/{SHA}/check-runs": {"check_runs": list(runs)},
        f"{R}/commits/{SHA}/status": {"statuses": []},
        "user": {"login": me} if me else ("error", 403),
    }


def gitlab_routes(files, mode="recommend", notes=(), **mr_extra):
    mr = {
        "iid": 12,
        "author": {"username": "alice"},
        "draft": False,
        "labels": [],
        "assignees": [],
        "sha": SHA,
        "target_branch": "main",
        "changes_count": str(len(files)),
    }
    mr.update(mr_extra)
    return {
        f"{P}/merge_requests/12": mr,
        f"{P}/merge_requests/12/diffs": [
            {
                "old_path": name,
                "new_path": name,
                "diff": "@@ -0,0 +1 @@\n" + "+x\n" * adds,
                "renamed_file": False,
                "collapsed": False,
                "too_large": False,
            }
            for name, adds in files
        ],
        f"{P}/repository/files/.review-tiers.toml/raw": TIER_MAP.format(mode=mode),
        f"{P}/merge_requests/12/notes": list(notes),
        "user": {"username": "group_5_bot_ab12"},
    }


def run(argv, routes, review=None, stdin=None):
    """Run the command line. Returns (exit code, stdout, stderr, fake)."""
    fake = FakeTransport(routes)
    out, err = io.StringIO(), io.StringIO()
    with tempfile.TemporaryDirectory() as scratch:
        if review is not None:
            path = Path(scratch) / "review.json"
            path.write_text(json.dumps(review), encoding="utf-8")
            argv = [*argv, "--review", str(path)]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(
                argv,
                env={"GITHUB_TOKEN": "t", "GITLAB_TOKEN": "t", "GITLAB_URL": GITLAB},
                transport=fake,
            )
    return code, out.getvalue(), err.getvalue(), fake


def _split(code, out):
    result, _, body = out.partition("\n\n")
    return (json.loads(result) if code == 0 else None), body.rstrip("\n") + "\n"


def github_publish(routes, review=None, extra=()):
    argv = ["publish", "--host", "github", "--repo", REPO, "--change", "7", "--show-guide", *extra]
    code, out, err, fake = run(argv, routes, review)
    result, body = _split(code, out)
    return code, result, body, err, fake


def gitlab_publish(routes, review=None, extra=()):
    argv = ["publish", "--host", "gitlab", "--repo", REPO, "--change", "12", "--show-guide", *extra]
    code, out, err, fake = run(argv, routes, review)
    result, body = _split(code, out)
    return code, result, body, err, fake


CLEAN = {"reviewed_sha": SHA, "summary": "Nothing of note."}


# ------------------------------------------------------------------- recommend mode


class RecommendOnGitLabTest(unittest.TestCase):
    FILES = [
        ("app/db/migrations/0042_add_index.py", 12),
        ("src/service.py", 30),
        ("docs/notes.md", 4),
    ]

    def test_dry_run_reads_and_decides_but_writes_nothing(self):
        code, result, body, _, fake = gitlab_publish(gitlab_routes(self.FILES), extra=["--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [])
        self.assertEqual(
            (result["tier"], result["action"], result["reviewers"]), ("high", "recommend", 2)
        )
        self.assertEqual(result["writes"], ["post guide comment", "add label risk::high"])
        self.assertTrue(result["dry_run"])

    def test_the_guide_points_the_reviewer_at_the_risky_file_first(self):
        _, _, body, _, _ = gitlab_publish(gitlab_routes(self.FILES), extra=["--dry-run"])
        self.assertTrue(
            body.startswith("<!-- review-tiers action=recommend -->\n### Review guide: high risk")
        )
        self.assertIn("**Recommendation:** 2 reviewers. Start with the files listed first.", body)
        self.assertIn(
            "- `app/db/migrations/0042_add_index.py` (+12 −0): "
            "Schema migrations. Can it be rolled back?",
            body,
        )
        self.assertIn("- `src/service.py` (+30 −0)\n", body)
        self.assertIn("**Low risk: skim.** 1 file.", body)
        self.assertIn("No agent review was supplied", body)
        self.assertIn("Tiers come from `.review-tiers.toml` on `main`", body)
        self.assertIn("`risk::disagree`", body)
        self.assertLess(body.index("High risk: read first"), body.index("Medium risk"))

    def test_a_real_run_posts_one_note_and_sets_the_scoped_label(self):
        routes = gitlab_routes(self.FILES, labels=["risk::low", "bug"])
        code, result, body, _, fake = gitlab_publish(routes)
        self.assertEqual(code, 0)
        self.assertEqual(
            fake.writes(),
            [
                ("POST", f"{P}/merge_requests/12/notes", {"body": body}),
                (
                    "PUT",
                    f"{P}/merge_requests/12",
                    {"add_labels": "risk::high", "remove_labels": "risk::low"},
                ),
            ],
        )
        self.assertFalse(result["dry_run"])

    def test_a_token_that_may_not_label_still_posts_the_guide(self):
        routes = gitlab_routes(self.FILES)
        routes[("PUT", f"{P}/merge_requests/12")] = ("error", 403)
        code, result, _, _, fake = gitlab_publish(routes)
        self.assertEqual(code, 0)
        self.assertEqual(
            result["writes"],
            ["post guide comment", "labels skipped: this token may not change labels"],
        )
        self.assertEqual(fake.writes()[0][0], "POST")

    def test_the_existing_note_is_updated_in_place_or_left_alone(self):
        _, _, body, _, _ = gitlab_publish(gitlab_routes(self.FILES), extra=["--dry-run"])
        mine = {
            "id": 55,
            "body": "<!-- review-tiers action=recommend -->\nold",
            "author": {"username": "group_5_bot_ab12"},
        }
        code, result, _, _, fake = gitlab_publish(
            gitlab_routes(self.FILES, notes=[mine], labels=["risk::high"])
        )
        self.assertEqual(
            fake.writes(), [("PUT", f"{P}/merge_requests/12/notes/55", {"body": body})]
        )
        current = dict(mine, body=body)
        _, result, _, _, fake = gitlab_publish(
            gitlab_routes(self.FILES, notes=[current], labels=["risk::high"])
        )
        self.assertEqual((fake.writes(), result["writes"]), ([], []))

    def test_a_pasted_marker_from_someone_else_is_not_ours(self):
        pasted = {
            "id": 9,
            "body": "<!-- review-tiers action=hold -->\nfake",
            "author": {"username": "mallory"},
        }
        _, _, _, _, fake = gitlab_publish(gitlab_routes(self.FILES, notes=[pasted]))
        self.assertEqual(fake.writes()[0][0], "POST")

    def test_low_risk_gets_a_light_recommendation_and_nothing_is_approved(self):
        code, result, body, _, fake = gitlab_publish(
            gitlab_routes([("docs/a.md", 3)]), review=CLEAN
        )
        self.assertEqual(
            (result["tier"], result["action"], result["review"]), ("low", "recommend", "clean")
        )
        self.assertIn("a light review is enough", body)
        self.assertIn("the agent review found nothing important", body)
        paths = [path for _, path, _ in fake.writes()]
        self.assertFalse([p for p in paths if "approve" in p or p.endswith("/merge")])

    def test_the_agent_can_raise_the_tier_and_its_findings_are_shown(self):
        review = {
            "reviewed_sha": SHA,
            "tier": "high",
            "tier_reason": "docs page embeds a live credential",
            "summary": "One real problem.",
            "findings": [
                {"severity": "nit", "title": "Typo"},
                {
                    "severity": "important",
                    "path": "docs/a.md",
                    "line": 14,
                    "title": "Credential in example",
                    "detail": "Rotate it.",
                },
            ],
            "focus": [{"path": "docs/a.md", "line": 14, "why": "the example block"}],
        }
        _, result, body, _, _ = gitlab_publish(
            gitlab_routes([("docs/a.md", 3)]), review, ["--dry-run"]
        )
        self.assertEqual(
            (result["tier"], result["path_tier"], result["raised_by_review"]), ("high", "low", True)
        )
        self.assertEqual(result["writes"][-1], "add label risk::high")
        self.assertIn("### Review guide: high risk", body)
        self.assertIn("- Important: `docs/a.md:14` Credential in example. Rotate it.", body)
        self.assertLess(body.index("- Important:"), body.index("- Nit: Typo"))
        self.assertIn("- `docs/a.md:14`: the example block", body)
        self.assertIn(
            "- Raised to high by the agent review: docs page embeds a live credential", body
        )

    def test_a_review_of_an_older_commit_is_not_shown_as_current(self):
        stale = {
            "reviewed_sha": "0ld",
            "findings": [{"severity": "important", "title": "Old news"}],
        }
        _, result, body, _, _ = gitlab_publish(
            gitlab_routes([("docs/a.md", 3)]), stale, ["--dry-run"]
        )
        self.assertEqual(result["review"], "stale")
        self.assertIn("is of an older commit, so it is not shown", body)
        self.assertNotIn("Old news", body)

    def test_withheld_diffs_are_called_out_and_block_low(self):
        routes = gitlab_routes([("docs/a.md", 0)])
        routes[f"{P}/merge_requests/12/diffs"][0].update(diff="", too_large=True)
        _, result, body, _, _ = gitlab_publish(routes, extra=["--dry-run"])
        self.assertEqual(
            (result["path_tier"], result["tier"], result["size_known"]), ("low", "medium", False)
        )
        self.assertIn("**Recommendation:** 1 reviewer.\n", body)
        self.assertIn("**Changed files (low risk by path)**", body)
        self.assertIn("- `docs/a.md` (size not shown by the host): Documentation.", body)
        self.assertIn("could not be measured", body)

    def test_the_result_names_no_paths_or_people(self):
        code, out, _, _ = run(
            ["publish", "--host", "gitlab", "--repo", REPO, "--change", "12", "--dry-run"],
            gitlab_routes(self.FILES),
        )
        for secret in ("migrations", "service.py", "alice", "main", REPO):
            self.assertNotIn(secret, out)

    def test_closed_or_merged_changes_are_only_ever_dry_run(self):
        routes = gitlab_routes(self.FILES, state="merged")
        code, _, _, err, fake = gitlab_publish(routes)
        self.assertEqual((code, fake.writes()), (2, []))
        self.assertIn("this merge request is closed or merged", err)
        code, result, _, _, fake = gitlab_publish(routes, extra=["--dry-run"])
        self.assertEqual((code, result["tier"], fake.writes()), (0, "high", []))
        closed = github_routes([("docs/a.md", 1)], state="closed")
        code, _, _, err, fake = github_publish(closed, CLEAN)
        self.assertEqual((code, fake.writes()), (2, []))
        self.assertIn("this pull request is closed or merged", err)

    def test_auto_mode_is_refused_on_gitlab(self):
        code, _, _, err, fake = gitlab_publish(
            gitlab_routes([("docs/a.md", 1)], mode="auto"), CLEAN
        )
        self.assertEqual(code, 2)
        self.assertIn("auto mode is not implemented for gitlab", err)
        self.assertEqual(fake.writes(), [])


class UntrustedTextTest(unittest.TestCase):
    def test_agent_text_cannot_start_a_line_ping_people_or_inject_html(self):
        review = {
            "reviewed_sha": SHA,
            "summary": "Fine.\n/approve\n/merge",
            "findings": [
                {
                    "severity": "nit",
                    "title": "cc @everyone <img src=x>",
                    "detail": "line one\n/label ~approved\n<!-- review-tiers action=merge -->",
                }
            ],
            "focus": [{"path": "docs/a.md", "why": "see\n/close"}],
            "tier_reason": "x\n/approve",
        }
        _, _, body, _, _ = gitlab_publish(gitlab_routes([("docs/a.md", 3)]), review, ["--dry-run"])
        for line in body.splitlines():
            self.assertFalse(line.startswith("/"), line)
        self.assertNotIn("@everyone", body)
        self.assertNotIn("<img", body)
        self.assertEqual(body.count("<!--"), 1)
        self.assertIn("- Summary: Fine. /approve /merge", body)

    def test_paths_cannot_break_out_of_their_code_span(self):
        nasty = "docs/`@everyone`\n/approve.md"
        _, result, body, _, _ = gitlab_publish(gitlab_routes([(nasty, 1)]), extra=["--dry-run"])
        self.assertEqual(result["tier"], "low")  # the newline did not dodge docs/**
        self.assertIn("**Low risk: skim.**", body)
        routes = gitlab_routes([("src/`@everyone`\n/approve.py", 1)])
        _, _, body, _, _ = gitlab_publish(routes, extra=["--dry-run"])
        self.assertIn("- `src/'@everyone'/approve.py` (+1 −0)", body)
        for line in body.splitlines():
            self.assertFalse(line.startswith("/"), line)

    def test_helpers(self):
        self.assertEqual(guide.code("a`b"), "`a'b`")
        self.assertTrue(guide.code("x" * 300).endswith("…`"))
        self.assertEqual(guide.plain("mail me@example.com"), "mail me@​example.com")
        self.assertEqual(guide.previous_action("<!-- review-tiers action=hold -->\nx"), "hold")
        self.assertIsNone(guide.previous_action("hello"))


# ------------------------------------------------------------------------ auto mode


class AutoOnGitHubTest(unittest.TestCase):
    DOCS = [("docs/a.md", 3)]

    def run_named(self, name, status="completed", conclusion="success"):
        return {"id": 1, "name": name, "status": status, "conclusion": conclusion}

    def test_low_risk_reviewed_clean_and_green_merges_itself(self):
        routes = github_routes(self.DOCS, runs=[self.run_named("build")])
        code, result, body, _, fake = github_publish(routes, CLEAN)
        self.assertEqual(code, 0)
        self.assertEqual(
            (result["tier"], result["action"], result["review"]), ("low", "merge", "clean")
        )
        self.assertEqual(
            result["writes"], ["post guide comment", "add label risk:low", "merge (squash)"]
        )
        self.assertIn("**Clear.** Low risk, reviewed clean at `abc1234`, checks green.", body)
        self.assertEqual(
            fake.writes()[-1],
            ("PUT", f"{R}/pulls/7/merge", {"sha": SHA, "merge_method": "squash"}),
        )
        self.assertNotIn(f"{R}/issues/7/assignees", [path for _, path, _ in fake.writes()])

    def test_medium_can_be_let_through_by_asking_for_no_reviewers(self):
        routes = github_routes([("src/app.py", 10)])
        routes[f"{R}/contents/.review-tiers.toml"] += (
            "\n[reviewers]\nlow = 0\nmedium = 0\nhigh = 1\n"
        )
        _, result, body, _, fake = github_publish(routes, CLEAN)
        self.assertEqual((result["tier"], result["action"]), ("medium", "merge"))
        self.assertIn("**Clear.** Medium risk, reviewed clean", body)
        high = github_routes([("db/migrations/1.py", 10)])
        high[f"{R}/contents/.review-tiers.toml"] = routes[f"{R}/contents/.review-tiers.toml"]
        _, result, _, _, _ = github_publish(high, CLEAN)
        self.assertEqual(
            (result["action"], result["reasons"]), ("hold", ["high risk calls for 1 reviewer"])
        )

    def test_dry_run_never_merges(self):
        _, result, _, _, fake = github_publish(github_routes(self.DOCS), CLEAN, ["--dry-run"])
        self.assertEqual(result["action"], "merge")
        self.assertEqual(fake.writes(), [])

    def test_anything_above_low_is_held_and_the_owner_is_told_once(self):
        routes = github_routes([("src/app.py", 10)])
        code, result, body, _, fake = github_publish(routes, CLEAN)
        self.assertEqual(
            (result["action"], result["reasons"]), ("hold", ["medium risk calls for 1 reviewer"])
        )
        self.assertIn("**Needs a person: @alice.** Medium risk calls for 1 reviewer.", body)
        self.assertEqual(
            [(m, p) for m, p, _ in fake.writes()],
            [
                ("POST", f"{R}/issues/7/comments"),
                ("POST", f"{R}/issues/7/labels"),
                ("POST", f"{R}/issues/7/assignees"),
            ],
        )
        self.assertNotIn(f"{R}/pulls/7/merge", [p for _, p, _ in fake.writes()])

        # Second run on the same state: already held, so nobody is assigned again.
        held = {"id": 5, "body": body, "user": {"login": "alice", "type": "User"}}
        routes = github_routes(
            [("src/app.py", 10)], comments=[held], labels=[{"name": "risk:medium"}]
        )
        _, result, _, _, fake = github_publish(routes, CLEAN)
        self.assertEqual((fake.writes(), result["writes"]), ([], []))

    def test_important_findings_hold_even_low_risk_changes(self):
        review = {
            "reviewed_sha": SHA,
            "findings": [{"severity": "important", "title": "Broken link to prod runbook"}],
        }
        _, result, body, _, fake = github_publish(github_routes(self.DOCS), review)
        self.assertEqual((result["action"], result["review"]), ("hold", "findings"))
        self.assertIn("The review has 1 important finding.", body)
        self.assertNotIn(f"{R}/pulls/7/merge", [p for _, p, _ in fake.writes()])

    def test_without_a_review_it_waits_quietly(self):
        _, result, body, _, fake = github_publish(github_routes(self.DOCS))
        self.assertEqual(
            (result["action"], result["reasons"]), ("wait", ["no review of this change yet"])
        )
        self.assertIn("**Waiting.** No review of this change yet. Nothing to do yet.", body)
        paths = [p for _, p, _ in fake.writes()]
        self.assertNotIn(f"{R}/issues/7/assignees", paths)
        self.assertNotIn(f"{R}/pulls/7/merge", paths)
        # No review means the checks were never even asked about.
        self.assertNotIn(f"{R}/commits/{SHA}/check-runs", [p for _, p, _ in fake.calls])

    def test_a_review_of_an_older_commit_does_not_merge(self):
        _, result, _, _, fake = github_publish(github_routes(self.DOCS), {"reviewed_sha": "0ld"})
        self.assertEqual((result["action"], result["review"]), ("wait", "stale"))
        self.assertNotIn(f"{R}/pulls/7/merge", [p for _, p, _ in fake.writes()])

    def test_pending_checks_wait_and_failed_checks_hold(self):
        pending = github_routes(self.DOCS, runs=[self.run_named("build", "in_progress", None)])
        _, result, _, _, fake = github_publish(pending, CLEAN)
        self.assertEqual(
            (result["action"], result["reasons"]), ("wait", ["checks still running: build"])
        )
        self.assertNotIn(f"{R}/pulls/7/merge", [p for _, p, _ in fake.writes()])

        failed = github_routes(self.DOCS, runs=[self.run_named("build", conclusion="failure")])
        _, result, body, _, fake = github_publish(failed, CLEAN)
        self.assertEqual((result["action"], result["reasons"]), ("hold", ["checks failed: build"]))
        self.assertIn(f"{R}/issues/7/assignees", [p for _, p, _ in fake.writes()])

    def test_ignored_checks_do_not_block(self):
        own = github_routes(self.DOCS, runs=[self.run_named("Review", "in_progress", None)])
        self.assertEqual(github_publish(own, CLEAN)[1]["action"], "merge")
        other = github_routes(self.DOCS, runs=[self.run_named("Triage", "in_progress", None)])
        self.assertEqual(github_publish(other, CLEAN)[1]["action"], "wait")
        self.assertEqual(
            github_publish(other, CLEAN, ["--ignore-check", "Triage"])[1]["action"], "merge"
        )

    def test_wait_polls_until_checks_finish(self):
        states = iter(["in_progress", "in_progress", "completed"])
        routes = github_routes(self.DOCS)
        routes[f"{R}/commits/{SHA}/check-runs"] = lambda *_: {
            "check_runs": [
                {"id": 1, "name": "build", "status": next(states), "conclusion": "success"}
            ]
        }
        fake = FakeTransport(routes)
        naps = []
        host = cli.GitHub(REPO, "t", transport=fake)
        change = host.get_change(7)
        config = rt.parse_config(TIER_MAP.format(mode="auto"), ".review-tiers.toml")
        result, _ = cli.publish(
            host, config, change, rt.parse_review(CLEAN), wait_seconds=60, sleep=naps.append
        )
        self.assertEqual((result["action"], naps), ("merge", [15, 15]))

        forever = github_routes(self.DOCS, runs=[self.run_named("build", "queued", None)])
        naps = []
        host = cli.GitHub(REPO, "t", transport=FakeTransport(forever))
        result, _ = cli.publish(
            host,
            config,
            host.get_change(7),
            rt.parse_review(CLEAN),
            wait_seconds=30,
            sleep=naps.append,
        )
        self.assertEqual((result["action"], naps), ("wait", [15, 15]))

    def test_a_refused_merge_becomes_a_hold(self):
        routes = github_routes(self.DOCS)
        routes[("PUT", f"{R}/pulls/7/merge")] = ("error", 409)
        code, result, body, _, fake = github_publish(routes, CLEAN)
        self.assertEqual(code, 0)
        self.assertEqual(
            (result["action"], result["reasons"]),
            ("hold", ["the host refused the merge (HTTP 409)"]),
        )
        self.assertIn("merge refused by the host", result["writes"])
        self.assertIn("**Needs a person: @alice.** The host refused the merge (HTTP 409).", body)
        self.assertIn(f"{R}/issues/7/assignees", [p for _, p, _ in fake.writes()])

    def test_drafts_wait_whatever_else_is_true(self):
        routes = github_routes([("src/app.py", 10)], draft=True)
        _, result, _, _, fake = github_publish(routes, CLEAN)
        self.assertEqual((result["action"], result["reasons"]), ("wait", ["it is still a draft"]))
        self.assertNotIn(f"{R}/issues/7/assignees", [p for _, p, _ in fake.writes()])

    def test_untrusted_authors_never_get_the_no_person_path(self):
        routes = github_routes(self.DOCS, user={"login": "mallory", "type": "User"})
        _, result, _, _, _ = github_publish(routes, CLEAN)
        self.assertEqual(
            (result["action"], result["reasons"]),
            ("hold", ["the author is not on the trusted list"]),
        )

    def test_recommend_mode_on_github_never_merges_or_assigns(self):
        routes = github_routes(self.DOCS, mode="recommend")
        _, result, body, _, fake = github_publish(routes, CLEAN)
        self.assertEqual(result["action"], "recommend")
        self.assertEqual(
            [(m, p) for m, p, _ in fake.writes()],
            [("POST", f"{R}/issues/7/comments"), ("POST", f"{R}/issues/7/labels")],
        )
        self.assertIn("`risk:disagree`", body)

    def test_when_the_token_cannot_say_who_it_is_only_bot_comments_are_ours(self):
        body = "<!-- review-tiers action=wait -->\nold"
        person = {"id": 1, "body": body, "user": {"login": "mallory", "type": "User"}}
        bot = {"id": 2, "body": body, "user": {"login": "github-actions[bot]", "type": "Bot"}}
        routes = github_routes(self.DOCS, comments=[person, bot], me=None)
        _, _, _, _, fake = github_publish(routes)
        self.assertEqual(fake.writes()[0][:2], ("PATCH", f"{R}/issues/comments/2"))


# ---------------------------------------------------------------- brief and errors


class BriefTest(unittest.TestCase):
    def test_brief_is_read_only_and_ordered_riskiest_first(self):
        files = [("docs/a.md", 2), ("app/migrations/1.py", 5), ("src/x.py", 9)]
        code, out, _, fake = run(
            ["brief", "--host", "gitlab", "--repo", REPO, "--change", "12"], gitlab_routes(files)
        )
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [])
        data = json.loads(out)
        self.assertEqual(
            (data["tier"], data["mode"], data["head_sha"], data["change"]),
            ("high", "recommend", SHA, 12),
        )
        self.assertEqual(
            [f["path"] for f in data["files"]], ["app/migrations/1.py", "src/x.py", "docs/a.md"]
        )
        first = data["files"][0]
        self.assertEqual(
            (first["area"], first["check"], first["additions"]),
            ("Schema migrations", "Can it be rolled back?", 5),
        )
        self.assertEqual(data["review_result"]["reviewed_sha"], SHA)
        # The template it hands the agent is itself a valid review result.
        template = {k: v for k, v in data["review_result"].items() if k != "note"}
        self.assertEqual(rt.parse_review(template).reviewed_sha, SHA)


class ErrorsTest(unittest.TestCase):
    def test_a_missing_tier_map_is_explained(self):
        routes = gitlab_routes([("docs/a.md", 1)])
        del routes[f"{P}/repository/files/.review-tiers.toml/raw"]
        code, _, _, err, fake = gitlab_publish(routes)
        self.assertEqual(code, 2)
        self.assertIn("no tier map at .review-tiers.toml on branch main", err)
        self.assertEqual(fake.writes(), [])

    def test_a_local_draft_map_can_be_tried_with_config_file(self):
        routes = gitlab_routes([("docs/a.md", 1)])
        del routes[f"{P}/repository/files/.review-tiers.toml/raw"]
        with tempfile.TemporaryDirectory() as scratch:
            draft = Path(scratch) / "draft.toml"
            draft.write_text('[[rules]]\ntier = "high"\npaths = ["docs/**"]\n', encoding="utf-8")
            code, result, _, _, _ = gitlab_publish(
                routes, extra=["--dry-run", "--config-file", str(draft)]
            )
        self.assertEqual((code, result["tier"]), (0, "high"))

    def test_bad_review_bad_map_and_api_failures_have_distinct_exit_codes(self):
        routes = gitlab_routes([("docs/a.md", 1)])
        code, _, _, err, fake = gitlab_publish(routes, {"verdict": "ship it"})
        self.assertEqual((code, fake.calls), (2, []))
        self.assertIn("unknown key", err)

        routes[f"{P}/repository/files/.review-tiers.toml/raw"] = 'mode = "enforce"\n'
        self.assertEqual(gitlab_publish(routes)[0], 2)

        code, _, _, err, _ = gitlab_publish({})
        self.assertEqual(code, 1)
        self.assertIn("HTTP 404", err)

    def test_missing_token_or_address(self):
        fake = FakeTransport({})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(
                ["brief", "--host", "gitlab", "--repo", REPO, "--change", "1"],
                env={},
                transport=fake,
            )
        self.assertEqual(code, 2)
        self.assertIn("GITLAB_TOKEN", err.getvalue())
        with contextlib.redirect_stderr(err):
            code = cli.main(
                ["brief", "--host", "gitlab", "--repo", REPO, "--change", "1"],
                env={"GITLAB_TOKEN": "t"},
                transport=fake,
            )
        self.assertEqual(code, 2)
        self.assertIn("GITLAB_URL", err.getvalue())

    def test_explain_runs_offline(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as scratch:
            draft = Path(scratch) / "map.toml"
            draft.write_text(TIER_MAP.format(mode="recommend"), encoding="utf-8")
            with contextlib.redirect_stdout(out):
                code = cli.main(
                    [
                        "explain",
                        "--config-file",
                        str(draft),
                        "a/migrations/1.py",
                        "src/x.py",
                        ".gitlab-ci.yml",
                        ".review-tiers.toml",
                    ],
                    env={},
                )
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("high   a/migrations/1.py  (rule **/migrations/**)", text)
        self.assertIn("medium src/x.py  (no rule matched)", text)
        self.assertIn("high   .gitlab-ci.yml  (always high: .gitlab-ci.yml)", text)
        self.assertIn("high   .review-tiers.toml  (always high: .review-tiers.toml)", text)


if __name__ == "__main__":
    unittest.main()
