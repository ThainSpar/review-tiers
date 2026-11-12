"""Tests for the HTTP client and the two hosts, against a stand-in for the network."""

import unittest

from review_tiers.hosts import NotSupported
from review_tiers.hosts import github as gh
from review_tiers.hosts import gitlab as gl
from review_tiers.http import Api, ApiError

from .fakes import GITHUB, GITLAB, FakeTransport

REPO = "acme/widgets"
P = "projects/acme%2Fwidgets"


def github(routes=None, **kwargs):
    fake = FakeTransport(routes, **kwargs)
    return gh.GitHub(REPO, "t0ken", transport=fake), fake


def gitlab(routes=None, **kwargs):
    fake = FakeTransport(routes, **kwargs)
    return gl.GitLab(REPO, "t0ken", GITLAB, transport=fake), fake


class ApiTest(unittest.TestCase):
    def test_a_token_is_never_sent_over_plain_http(self):
        with self.assertRaisesRegex(ApiError, "non-HTTPS"):
            Api("http://gitlab.internal", {"PRIVATE-TOKEN": "x"})

    def test_get_all_follows_pages_until_a_short_one(self):
        fake = FakeTransport({"things": [{"n": n} for n in range(250)]})
        api = Api(GITHUB, {}, fake)
        self.assertEqual(len(api.get_all("things")), 250)
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(len(api.get_all("things", limit=100)), 100)

    def test_until_empty_survives_a_host_that_serves_smaller_pages(self):
        things = [{"n": n} for n in range(45)]
        capped = Api(GITHUB, {}, FakeTransport({"things": things}, page_cap=20))
        self.assertEqual(len(capped.get_all("things")), 20)  # the trap
        self.assertEqual(len(capped.get_all("things", until_empty=True)), 45)

    def test_errors_carry_the_status_and_can_be_tolerated(self):
        api = Api(GITHUB, {}, FakeTransport({("PUT", "x"): ("error", 403)}))
        with self.assertRaises(ApiError) as caught:
            api.request("PUT", "x", {})
        self.assertEqual(caught.exception.status, 403)
        self.assertIn("failed with 403", str(caught.exception))
        self.assertIsNone(api.request("PUT", "x", {}, tolerate=(403,)))

    def test_reads_are_retried_but_writes_are_not(self):
        attempts = []

        def flaky(method, url, headers, body):
            attempts.append(method)
            if len(attempts) == 1:
                raise ApiError("no response (timed out)")
            if len(attempts) == 2:
                return 503, "{}"
            return 200, '{"ok": true}'

        api = Api(GITHUB, {}, flaky)
        naps = []
        api.sleep = naps.append
        self.assertEqual(api.get("x"), {"ok": True})
        self.assertEqual((attempts, naps), (["GET"] * 3, [1, 3]))

        def down(method, url, headers, body):
            attempts.append(method)
            raise ApiError("no response")

        attempts.clear()
        api = Api(GITHUB, {}, down)
        api.sleep = lambda _: None
        with self.assertRaises(ApiError):
            api.get("x")
        self.assertEqual(len(attempts), 3)
        attempts.clear()
        with self.assertRaises(ApiError):
            api.request("POST", "x", {})
        self.assertEqual(attempts, ["POST"])

        busy = Api(GITHUB, {}, lambda *a: (503, "{}"))
        busy.sleep = lambda _: None
        with self.assertRaises(ApiError) as caught:
            busy.get("x")
        self.assertEqual(caught.exception.status, 503)

    def test_raw_replies_are_returned_as_text(self):
        api = Api(GITHUB, {}, FakeTransport({"file": 'mode = "auto"\n'}))
        self.assertEqual(api.get("file", raw=True), 'mode = "auto"\n')


class GitHubTest(unittest.TestCase):
    def pr(self, **extra):
        raw = {
            "number": 7,
            "user": {"login": "alice", "type": "User"},
            "draft": False,
            "labels": [{"name": "bug"}],
            "assignees": [{"login": "bob"}],
            "head": {"sha": "abc1234"},
            "base": {"ref": "main"},
        }
        raw.update(extra)
        return raw

    def test_a_pull_request_maps_onto_the_change_model(self):
        files = [
            {
                "filename": "docs/new.md",
                "previous_filename": "infra/old.md",
                "additions": 3,
                "deletions": 2,
            }
        ]
        host, fake = github(
            {f"repos/{REPO}/pulls/7": self.pr(), f"repos/{REPO}/pulls/7/files": files}
        )
        change = host.get_change(7)
        self.assertEqual(
            (change.author, change.author_is_bot, change.draft), ("alice", False, False)
        )
        self.assertEqual((change.head_sha, change.target_branch), ("abc1234", "main"))
        self.assertEqual(change.labels, frozenset({"bug"}))
        self.assertEqual(change.assignees, ("bob",))
        self.assertEqual(change.files[0].previous_path, "infra/old.md")
        self.assertEqual(fake.headers[0]["Authorization"], "Bearer t0ken")

    def test_bots_and_truncated_file_lists(self):
        bot = gh.change_from_api(self.pr(user={"login": "x[bot]", "type": "Bot"}), [])
        self.assertTrue(bot.author_is_bot)
        short = gh.change_from_api(self.pr(changed_files=5000), [{"filename": "a"}])
        self.assertTrue(short.files_truncated)
        full = gh.change_from_api(self.pr(changed_files=1), [{"filename": "a"}])
        self.assertFalse(full.files_truncated)

    def test_read_file_asks_for_the_target_branch_and_handles_absence(self):
        host, fake = github({f"repos/{REPO}/contents/.review-tiers.toml": "x = 1\n"})
        self.assertEqual(host.read_file(".review-tiers.toml", "main"), "x = 1\n")
        self.assertEqual(fake.headers[0]["Accept"], "application/vnd.github.raw+json")
        self.assertIsNone(host.read_file("missing.toml", "main"))

    def checks(self, runs=(), statuses=(), ignore=frozenset()):
        host, _ = github(
            {
                f"repos/{REPO}/commits/abc/check-runs": {"check_runs": list(runs)},
                f"repos/{REPO}/commits/abc/status": {"statuses": list(statuses)},
            }
        )
        return host.checks("abc", ignore)

    def run_(self, name, status="completed", conclusion="success", run_id=1):
        return {"id": run_id, "name": name, "status": status, "conclusion": conclusion}

    def test_checks_green_pending_failed(self):
        self.assertEqual(self.checks().state, "green")
        ok = [
            self.run_("build"),
            self.run_("docs", conclusion="skipped"),
            self.run_("n", conclusion="neutral"),
        ]
        self.assertEqual(self.checks(ok).state, "green")
        pending = self.checks(
            [self.run_("build"), self.run_("e2e", status="in_progress", conclusion=None)]
        )
        self.assertEqual((pending.state, pending.names), ("pending", ("e2e",)))
        failed = self.checks(
            [self.run_("build", conclusion="failure"), self.run_("e2e", status="queued")],
            [{"context": "deploy/preview", "state": "error"}],
        )
        self.assertEqual((failed.state, failed.names), ("failed", ("build", "deploy/preview")))
        for conclusion in ("cancelled", "timed_out", "action_required"):
            self.assertEqual(self.checks([self.run_("b", conclusion=conclusion)]).state, "failed")
        self.assertEqual(
            self.checks(statuses=[{"context": "ci", "state": "pending"}]).state, "pending"
        )

    def test_only_the_latest_run_of_a_check_counts_and_ignored_ones_never_do(self):
        rerun = [self.run_("build", conclusion="failure", run_id=1), self.run_("build", run_id=2)]
        self.assertEqual(self.checks(rerun).state, "green")
        own = [self.run_("Review", status="in_progress", conclusion=None)]
        self.assertEqual(self.checks(own, ignore=frozenset({"Review"})).state, "green")

    def test_merge_pins_the_reviewed_commit(self):
        host, fake = github()
        host.merge(7, "abc1234", "squash")
        self.assertEqual(
            fake.writes(),
            [("PUT", f"repos/{REPO}/pulls/7/merge", {"sha": "abc1234", "merge_method": "squash"})],
        )

    def test_labels_and_assignees(self):
        host, fake = github()
        host.set_labels(7, ["risk:high"], ["risk:low"])
        host.assign(7, ["alice"])
        self.assertEqual(
            fake.writes(),
            [
                ("POST", f"repos/{REPO}/issues/7/labels", {"labels": ["risk:high"]}),
                ("DELETE", f"repos/{REPO}/issues/7/labels/risk%3Alow", None),
                ("POST", f"repos/{REPO}/issues/7/assignees", {"assignees": ["alice"]}),
            ],
        )

    def test_current_user_is_none_when_the_token_cannot_say(self):
        host, _ = github({"user": ("error", 403)})
        self.assertIsNone(host.current_user())
        host, _ = github({"user": {"login": "alice"}})
        self.assertEqual(host.current_user(), "alice")


class DiffCountTest(unittest.TestCase):
    def test_counts_only_lines_inside_hunks(self):
        diff = (
            "@@ -1,3 +1,4 @@\n context\n-old line\n+new line\n+another\n"
            "\\ No newline at end of file\n"
        )
        self.assertEqual(gl.count_diff_lines(diff), (2, 1))

    def test_file_headers_are_not_counted(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
        self.assertEqual(gl.count_diff_lines(diff), (1, 1))

    def test_removed_lines_that_look_like_headers_still_count(self):
        diff = "@@ -1,2 +1,2 @@\n--- a markdown rule\n+++ b markdown rule\n@@ -9 +9 @@\n-x\n"
        self.assertEqual(gl.count_diff_lines(diff), (1, 2))

    def test_empty(self):
        self.assertEqual(gl.count_diff_lines(""), (0, 0))


class GitLabTest(unittest.TestCase):
    def mr(self, **extra):
        raw = {
            "iid": 12,
            "author": {"username": "alice", "bot": False},
            "draft": False,
            "labels": ["bug"],
            "assignees": [{"username": "bob"}],
            "sha": "abc1234",
            "target_branch": "main",
            "changes_count": "3",
        }
        raw.update(extra)
        return raw

    def diff(self, new, old=None, text="@@ -1 +1,2 @@\n a\n+b\n", **flags):
        entry = {
            "old_path": old or new,
            "new_path": new,
            "diff": text,
            "renamed_file": bool(old),
            "new_file": False,
            "deleted_file": False,
            "collapsed": False,
            "too_large": False,
        }
        entry.update(flags)
        return entry

    def test_a_merge_request_maps_onto_the_change_model(self):
        diffs = [
            self.diff("docs/new.md", old="infra/old.md"),
            self.diff("src/big.py", text="", too_large=True),
            self.diff("src/folded.py", text="", collapsed=True),
        ]
        host, fake = gitlab(
            {f"{P}/merge_requests/12": self.mr(), f"{P}/merge_requests/12/diffs": diffs}
        )
        change = host.get_change(12)
        self.assertEqual((change.number, change.author, change.draft), (12, "alice", False))
        self.assertEqual((change.head_sha, change.target_branch), ("abc1234", "main"))
        self.assertEqual(change.labels, frozenset({"bug"}))
        self.assertEqual(change.assignees, ("bob",))
        renamed, big, folded = change.files
        self.assertEqual(
            (renamed.previous_path, renamed.additions, renamed.size_known),
            ("infra/old.md", 1, True),
        )
        self.assertFalse(big.size_known)
        self.assertFalse(folded.size_known)
        self.assertFalse(change.files_truncated)
        self.assertEqual(fake.headers[0]["PRIVATE-TOKEN"], "t0ken")
        self.assertEqual(fake.calls[0][1], f"{P}/merge_requests/12")

    def test_collapsed_with_text_still_has_a_known_size(self):
        change = gl.change_from_api(self.mr(changes_count="1"), [self.diff("a.py", collapsed=True)])
        self.assertTrue(change.files[0].size_known)

    def test_every_page_of_diffs_is_read_even_when_pages_are_small(self):
        diffs = [self.diff(f"src/f{n}.py") for n in range(45)]
        host, _ = gitlab(
            {
                f"{P}/merge_requests/12": self.mr(changes_count="45"),
                f"{P}/merge_requests/12/diffs": diffs,
            },
            page_cap=20,
        )
        change = host.get_change(12)
        self.assertEqual(len(change.files), 45)
        self.assertFalse(change.files_truncated)

    def test_overflowing_merge_requests_are_marked_truncated(self):
        one = [self.diff("a.py")]
        self.assertTrue(gl.change_from_api(self.mr(changes_count="1000+"), one).files_truncated)
        self.assertTrue(gl.change_from_api(self.mr(changes_count="7"), one).files_truncated)
        self.assertFalse(gl.change_from_api(self.mr(changes_count="1"), one).files_truncated)
        self.assertFalse(gl.change_from_api(self.mr(changes_count=None), one).files_truncated)

    def test_bot_authors_by_flag_or_by_access_token_username(self):
        flagged = gl.change_from_api(self.mr(author={"username": "svc", "bot": True}), [])
        self.assertTrue(flagged.author_is_bot)
        for name in ("group_123_bot_4ffca233d8298ea1", "project_9_bot", "project_9_bot2"):
            named = gl.change_from_api(self.mr(author={"username": name}), [])
            self.assertTrue(named.author_is_bot, name)
        person = gl.change_from_api(self.mr(author={"username": "group_leader"}), [])
        self.assertFalse(person.author_is_bot)

    def test_drafts_and_detailed_labels(self):
        change = gl.change_from_api(
            self.mr(draft=None, work_in_progress=True, labels=[{"name": "risk::low"}]), []
        )
        self.assertTrue(change.draft)
        self.assertEqual(change.labels, frozenset({"risk::low"}))

    def test_read_file_encodes_the_path_and_handles_absence(self):
        host, fake = gitlab({f"{P}/repository/files/config%2Ftiers.toml/raw": "x = 1\n"})
        self.assertEqual(host.read_file("config/tiers.toml", "release/1.0"), "x = 1\n")
        self.assertIsNone(host.read_file("missing.toml", "main"))

    def test_system_notes_are_not_comments(self):
        notes = [
            {"id": 1, "body": "added 1 commit", "system": True, "author": {"username": "alice"}},
            {"id": 2, "body": "hello", "system": False, "author": {"username": "group_5_bot_ab12"}},
        ]
        host, _ = gitlab({f"{P}/merge_requests/12/notes": notes})
        comments = host.list_comments(12)
        self.assertEqual([(c.id, c.author_is_bot) for c in comments], [(2, True)])

    def test_writes(self):
        host, fake = gitlab()
        host.create_comment(12, "body")
        host.update_comment(12, 99, "new")
        host.set_labels(12, ["risk::high"], ["risk::low", "risk::medium"])
        host.set_labels(12, [], [])
        self.assertEqual(
            fake.writes(),
            [
                ("POST", f"{P}/merge_requests/12/notes", {"body": "body"}),
                ("PUT", f"{P}/merge_requests/12/notes/99", {"body": "new"}),
                (
                    "PUT",
                    f"{P}/merge_requests/12",
                    {"add_labels": "risk::high", "remove_labels": "risk::low,risk::medium"},
                ),
            ],
        )

    def test_auto_mode_operations_are_not_implemented(self):
        host, fake = gitlab()
        for call in (
            lambda: host.merge(12, "abc", "squash"),
            lambda: host.assign(12, ["a"]),
            lambda: host.checks("abc"),
        ):
            with self.assertRaises(NotSupported):
                call()
        self.assertEqual(fake.calls, [])

    def test_a_numeric_project_id_works_too(self):
        fake = FakeTransport(
            {"projects/42/merge_requests/12": self.mr(), "projects/42/merge_requests/12/diffs": []}
        )
        host = gl.GitLab("42", "t", GITLAB + "/", transport=fake)
        self.assertEqual(host.get_change(12).number, 12)


if __name__ == "__main__":
    unittest.main()
