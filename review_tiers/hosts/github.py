"""GitHub: pull requests through the REST API."""

from __future__ import annotations

from ..http import Api, Transport, quote, urllib_transport
from ..tiers import Change, ChangedFile, Checks
from . import Comment

# The pull request files endpoint lists at most this many files.
FILE_LIMIT = 3000

_PASSING = ("success", "neutral", "skipped")


def change_from_api(raw: dict, files: list[dict]) -> Change:
    """Build the decision's view of a pull request from REST API objects."""
    user = raw.get("user") or {}
    changed = tuple(
        ChangedFile(
            path=item["filename"],
            previous_path=item.get("previous_filename"),
            additions=item.get("additions", 0) or 0,
            deletions=item.get("deletions", 0) or 0,
        )
        for item in files
    )
    reported = raw.get("changed_files")
    truncated = len(changed) >= FILE_LIMIT or (
        isinstance(reported, int) and reported > len(changed)
    )
    return Change(
        number=raw["number"],
        author=user.get("login") or "",
        author_is_bot=(user.get("type") or "").lower() == "bot",
        draft=bool(raw.get("draft")),
        labels=frozenset(label["name"] for label in raw.get("labels") or []),
        assignees=tuple(person["login"] for person in raw.get("assignees") or []),
        files=changed,
        files_truncated=truncated,
        head_sha=(raw.get("head") or {}).get("sha"),
        target_branch=(raw.get("base") or {}).get("ref"),
        is_open=raw.get("state", "open") == "open",
    )


class GitHub:
    name = "github"
    label_separator = ":"
    change_word = "pull request"
    supports_auto = True

    def __init__(
        self,
        repo: str,
        token: str,
        base_url: str = "https://api.github.com",
        transport: Transport = urllib_transport,
    ):
        self.repo = repo
        self.api = Api(
            base_url,
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "review-tiers",
            },
            transport,
        )
        self._r = f"repos/{repo}"

    # -- reading

    def get_change(self, number: int) -> Change:
        raw = self.api.get(f"{self._r}/pulls/{number}")
        files = self.api.get_all(f"{self._r}/pulls/{number}/files", limit=FILE_LIMIT)
        return change_from_api(raw, files)

    def read_file(self, path: str, ref: str) -> str | None:
        """The text of ``path`` at ``ref``, or None when it does not exist there."""
        quoted = "/".join(quote(part) for part in path.split("/"))
        return self.api.get(
            f"{self._r}/contents/{quoted}?ref={quote(ref)}",
            raw=True,
            accept="application/vnd.github.raw+json",
            tolerate=(404,),
        )

    def current_user(self) -> str | None:
        """The login this token acts as, or None when the token cannot say."""
        data = self.api.get("user", tolerate=(401, 403, 404))
        return (data or {}).get("login")

    def list_comments(self, number: int) -> list[Comment]:
        return [
            Comment(
                id=item["id"],
                body=item.get("body") or "",
                author=(item.get("user") or {}).get("login") or "",
                author_is_bot=((item.get("user") or {}).get("type") or "").lower() == "bot",
            )
            for item in self.api.get_all(f"{self._r}/issues/{number}/comments")
        ]

    def checks(self, sha: str, ignore: frozenset[str] = frozenset()) -> Checks:
        """The state of every check run and commit status on ``sha``.

        Names in ``ignore`` are left out, so the job running this tool does not wait
        for itself. When a check ran more than once, only its latest run counts.
        """
        latest: dict[str, dict] = {}
        runs = self.api.get_all(f"{self._r}/commits/{sha}/check-runs", key="check_runs")
        for run in runs:
            name = run.get("name") or ""
            if name not in latest or (run.get("id") or 0) > (latest[name].get("id") or 0):
                latest[name] = run
        pending: list[str] = []
        failed: list[str] = []
        for name, run in latest.items():
            if name in ignore:
                continue
            if run.get("status") != "completed":
                pending.append(name)
            elif run.get("conclusion") not in _PASSING:
                failed.append(name)

        combined = self.api.get(f"{self._r}/commits/{sha}/status") or {}
        for status in combined.get("statuses") or []:
            name = status.get("context") or ""
            if name in ignore:
                continue
            if status.get("state") == "pending":
                pending.append(name)
            elif status.get("state") in ("failure", "error"):
                failed.append(name)

        if failed:
            return Checks("failed", tuple(sorted(failed)))
        if pending:
            return Checks("pending", tuple(sorted(pending)))
        return Checks("green")

    # -- writing

    def create_comment(self, number: int, body: str) -> None:
        self.api.request("POST", f"{self._r}/issues/{number}/comments", {"body": body})

    def update_comment(self, number: int, comment_id: int, body: str) -> None:
        self.api.request("PATCH", f"{self._r}/issues/comments/{comment_id}", {"body": body})

    def set_labels(self, number: int, add: list[str], remove: list[str]) -> None:
        if add:
            self.api.request("POST", f"{self._r}/issues/{number}/labels", {"labels": add})
        for name in remove:
            self.api.request(
                "DELETE",
                f"{self._r}/issues/{number}/labels/{quote(name)}",
                tolerate=(404,),
            )

    def assign(self, number: int, logins: list[str]) -> None:
        self.api.request("POST", f"{self._r}/issues/{number}/assignees", {"assignees": logins})

    def merge(self, number: int, sha: str, method: str) -> None:
        """Merge, but only if the head is still ``sha``: the commit that was reviewed."""
        self.api.request(
            "PUT",
            f"{self._r}/pulls/{number}/merge",
            {"sha": sha, "merge_method": method},
        )
