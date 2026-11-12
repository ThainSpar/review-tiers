# review-tiers

Sort pull requests and merge requests into low, medium and high risk, and tell the reviewer where to look first.

A tier map in the repository says which paths are sensitive and what to check there. `review-tiers` reads a change's files, decides its tier, and posts one review guide on the change. A reviewing agent can add findings and raise the tier. What happens next depends on the mode:

| | `recommend` | `auto` |
| --- | --- | --- |
| Low risk, reviewed clean | Guide says a light review is enough | Merged without a person |
| Anything else | Guide says how many reviewers, and what to read first | Held, and the owner is assigned and mentioned |
| Approves or merges | Never | Only the first row |

Use `recommend` where a person must approve every change. Use `auto` on repositories where you want to be interrupted only when it matters.

## How it works

```
tier map (.review-tiers.toml, read from the target branch)
        │
changed files ──► tier decision ──► brief (JSON) ──► reviewing agent
                        │                                   │
                        └──────────► publish ◄── review result (JSON)
                                        │
                     review guide comment, tier label, and in auto mode: merge or hold
```

- **The tier decision is deterministic.** Each file takes the strictest rule that matches it; the change takes the highest tier among its files. A low-risk change that is too large, or whose size the host will not show, is raised to medium. CI configuration, `CODEOWNERS`, `REVIEW.md` and the tier map itself are always high.
- **The agent can raise a tier and never lower one.** Nothing in a diff can talk the risk down.
- **The tier map comes from the target branch,** never from the change, so a change cannot lower its own tier.
- **No code from the change is ever run.**

## Requirements

Python 3.11 or newer. No other packages: it uses only the standard library and talks to the host over HTTPS.

## Quick start

```bash
git clone <this repository> review-tiers && cd review-tiers
cp templates/review-tiers.toml /path/to/your/repo/.review-tiers.toml   # then edit the rules

# See what your rules do, offline
python3 -m review_tiers explain --config-file /path/to/your/repo/.review-tiers.toml \
    src/app.py db/migrations/0001.py docs/guide.md
```

Then against a real change, without writing anything:

```bash
# GitHub
export GITHUB_TOKEN=...
python3 -m review_tiers publish --host github --repo OWNER/REPO --change 123 \
    --dry-run --show-guide

# GitLab
export GITLAB_URL=https://gitlab.example.com GITLAB_TOKEN=...
python3 -m review_tiers publish --host gitlab --repo GROUP/PROJECT --change 45 \
    --dry-run --show-guide
```

Until the tier map is on the target branch, add `--config-file path/to/draft.toml` to try a local draft. Drop `--dry-run` to post the guide.

## With a reviewing agent

```bash
python3 -m review_tiers brief   --host ... --repo ... --change N  > brief.json
#   ... your agent reads brief.json and the diff, and writes review.json ...
python3 -m review_tiers publish --host ... --repo ... --change N --review review.json
```

[prompts/triage.md](prompts/triage.md) is a prompt for that agent, and [docs/contract.md](docs/contract.md) defines both JSON documents. Any agent that can read a diff and write JSON will do. `templates/` has two ready-made wirings for Claude Code: a slash command and a GitHub Actions workflow.

## Commands

| Command | What it does | Writes |
| --- | --- | --- |
| `explain` | Shows which rule decides the tier of given paths | Nothing. Offline. |
| `brief` | Prints the tier and the files to look at, as JSON | Nothing |
| `publish` | Posts or updates the review guide and tier label; in auto mode, merges or holds | With `--dry-run`, nothing |
| `replay` | Replays the tier map over merged history and prints a baseline | Nothing |

## What works where

| | GitHub | GitLab |
| --- | --- | --- |
| `brief`, `publish` in recommend mode | Yes, run against live pull requests | Yes, tested against a stand-in only |
| `publish` in auto mode | Yes, the merge itself tested against a stand-in only | Not implemented |
| `replay` | Yes, run against live history | Not implemented |

The GitLab client was written from the API documentation and has not been run against a live server. Start with `--dry-run`; see [docs/gitlab.md](docs/gitlab.md).

## More

- [docs/modes.md](docs/modes.md): the two modes, what auto mode needs, and what is and is not enforced
- [docs/contract.md](docs/contract.md): the brief, the review result and exit codes
- [docs/gitlab.md](docs/gitlab.md): token, permissions and a first run
- [docs/data-boundary.md](docs/data-boundary.md): what is read, written and sent

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

They use a stand-in for the network.

## License

MIT. See [LICENSE](LICENSE).
