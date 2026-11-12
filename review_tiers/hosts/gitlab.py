"""GitLab: merge requests through the REST API (v4).

Written against the documented API and tested against a stand-in, not a live server.
Run ``publish --dry-run`` on a real merge request before trusting it.
"""

from __future__ import annotations

import re

from ..http import Api, Transport, quote, urllib_transport
from ..tiers import Change, ChangedFile, Checks
from . import Comment, NotSupported

# Accounts GitLab creates for project and group access tokens.
_TOKEN_BOT = re.compile(r"^(project|group)_\d+_bot")

_NO_AUTO = "auto mode is not implemented for GitLab in this version"


def count_diff_lines(diff: str) -> tuple[int, int]:
    """Count added and removed lines in one file's diff text.

    Only lines inside hunks count, so file headers (present with ``unidiff``) are not
    mistaken for changes.
    """
    additions = deletions = 0
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("@@"):
            in_hunk = True
        elif not in_hunk:
            continue
        elif line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return additions, deletions


def _is_bot(user: dict) -> bool:
    if user.get("bot") is True:
        return True
    return bool(_TOKEN_BOT.match(user.get("username") or ""))


def change_from_api(raw: dict, diffs: list[dict]) -> Change:
    """Build the decision's view of a merge request from REST API objects.

    The diffs endpoint gives no line counts, so they are counted from the diff text.
    When GitLab withholds a file's diff (too large, or collapsed with no text), that
    file's size is marked unknown instead of being counted as zero.
    """
    files = []
    for item in diffs:
        text = item.get("diff") or ""
        additions, deletions = count_diff_lines(text)
        withheld = bool(item.get("too_large")) or (bool(item.get("collapsed")) and not text)
        old, new = item.get("old_path"), item.get("new_path")
        files.append(
            ChangedFile(
                path=new or old or "",
                previous_path=old if item.get("renamed_file") and old != new else None,
                additions=additions,
                deletions=deletions,
                size_known=not withheld,
            )
        )

    # changes_count is a string such as "12", or "1000+" past GitLab's display limit.
    reported = str(raw.get("changes_count") or "").strip()
    truncated = reported.endswith("+") or (reported.isdigit() and int(reported) > len(files))

    author = raw.get("author") or {}
    labels = raw.get("labels") or []
    return Change(
        number=raw["iid"],
        author=author.get("username") or "",
        author_is_bot=_is_bot(author),
        draft=bool(raw.get("draft") or raw.get("work_in_progress")),
        labels=frozenset(label["name"] if isinstance(label, dict) else label for label in labels),
        assignees=tuple(person["username"] for person in raw.get("assignees") or []),
        files=tuple(files),
        files_truncated=truncated,
        head_sha=raw.get("sha"),
        target_branch=raw.get("target_branch"),
        is_open=raw.get("state", "opened") == "opened",
    )


class GitLab:
    name = "gitlab"
    label_separator = "::"  # scoped labels on tiers that have them; plain otherwise
    change_word = "merge request"
    supports_auto = False

    def __init__(
        self,
        project: str,
        token: str,
        base_url: str,
        transport: Transport = urllib_transport,
    ):
        self.repo = project
        self.api = Api(
            base_url.rstrip("/") + "/api/v4",
            {"PRIVATE-TOKEN": token, "User-Agent": "review-tiers"},
            transport,
        )
        # A numeric id or a URL-encoded path such as group%2Fsubgroup%2Fproject.
        self._p = f"projects/{quote(project)}"

    # -- reading

    def get_change(self, number: int) -> Change:
        raw = self.api.get(f"{self._p}/merge_requests/{number}")
        diffs = self.api.get_all(f"{self._p}/merge_requests/{number}/diffs", until_empty=True)
        return change_from_api(raw, diffs)

    def read_file(self, path: str, ref: str) -> str | None:
        """The text of ``path`` at ``ref``, or None when it does not exist there."""
        return self.api.get(
            f"{self._p}/repository/files/{quote(path)}/raw?ref={quote(ref)}",
            raw=True,
            tolerate=(404,),
        )

    def current_user(self) -> str | None:
        data = self.api.get("user", tolerate=(401, 403, 404))
        return (data or {}).get("username")

    def list_comments(self, number: int) -> list[Comment]:
        notes = self.api.get_all(
            f"{self._p}/merge_requests/{number}/notes?sort=asc&order_by=created_at",
            until_empty=True,
        )
        return [
            Comment(
                id=note["id"],
                body=note.get("body") or "",
                author=(note.get("author") or {}).get("username") or "",
                author_is_bot=_is_bot(note.get("author") or {}),
            )
            for note in notes
            if not note.get("system")
        ]

    def checks(self, sha: str, ignore: frozenset[str] = frozenset()) -> Checks:
        raise NotSupported(_NO_AUTO)

    # -- writing

    def create_comment(self, number: int, body: str) -> None:
        self.api.request("POST", f"{self._p}/merge_requests/{number}/notes", {"body": body})

    def update_comment(self, number: int, comment_id: int, body: str) -> None:
        self.api.request(
            "PUT",
            f"{self._p}/merge_requests/{number}/notes/{comment_id}",
            {"body": body},
        )

    def set_labels(self, number: int, add: list[str], remove: list[str]) -> None:
        body = {}
        if add:
            body["add_labels"] = ",".join(add)
        if remove:
            body["remove_labels"] = ",".join(remove)
        if body:
            self.api.request("PUT", f"{self._p}/merge_requests/{number}", body)

    def assign(self, number: int, logins: list[str]) -> None:
        raise NotSupported(_NO_AUTO)

    def merge(self, number: int, sha: str, method: str) -> None:
        raise NotSupported(_NO_AUTO)
