# GitLab

Recommend mode only in this version: `brief` and `publish` work; auto mode and `replay` do not.

**This client was written from GitLab's API documentation and tested against a stand-in. It has not been run against a live server.** Do the first run below before relying on it.

## Token

A project or group access token, or a personal one.

- **Scope:** `api`. Posting a comment is a write, so `read_api` is enough only for `brief` and `--dry-run`.
- **Role:** enough to comment on merge requests. Changing labels may need a higher role than commenting does. If the token may not label, `publish` reports "labels skipped" and still posts the guide; set `[post] labels = false` to stop it trying.

```bash
export GITLAB_URL=https://gitlab.example.com   # or CI_SERVER_URL inside GitLab CI
export GITLAB_TOKEN=...                         # or --token-env NAME
```

`--repo` takes the project's full path (`group/subgroup/project`) or its numeric id. The address must be HTTPS. For a private certificate authority, point `SSL_CERT_FILE` at its bundle.

## First run

1. `explain` against your draft tier map, with a few real paths. Offline.
2. `brief --config-file draft.toml` on a real merge request. Check that the number of files matches the merge request's Changes tab and that the line counts look right.
3. `publish --dry-run --show-guide --config-file draft.toml`. Read the guide and the `writes` list.
4. Commit the tier map to the default branch, drop `--config-file`, and run `publish` for real on one merge request.

## Things that differ from GitHub

- **Line counts** are counted from the diff text, because the API gives none. When GitLab withholds a file's diff (too large, or collapsed), that file's size is unknown: the guide says so, and a change that would otherwise be low risk is raised to medium instead of being assumed small.
- **Large merge requests** past GitLab's diff limits are treated as incomplete and are high risk.
- **Labels** default to `risk::low`, `risk::medium`, `risk::high`. Where scoped labels are available GitLab keeps only one; elsewhere they are ordinary labels and `publish` removes the other two itself.
- **Bot accounts** are recognised by the `bot` flag on recent versions, and otherwise by the usernames GitLab gives access tokens.
- **Quick actions:** a comment line starting with `/` is a command on GitLab. All text taken from a review or from file paths is flattened and never starts a line, so a change cannot make the guide approve or relabel anything.
- **The guide comment** is found again by its marker and by being written by the token's own account, so run `publish` with the same token each time.
