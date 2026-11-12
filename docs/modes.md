# Modes

Set `mode` in the tier map. The tier decision and the review guide are the same in both.

## recommend

For repositories where a person must approve. `publish` posts the guide and the tier label and does nothing else. It never approves, merges, assigns or blocks, whatever the tier and whatever the review says. The host's own approval rules stay in charge.

The guide says how many reviewers the tier calls for (`[reviewers]` in the tier map) and lists the high-risk files first, each with its rule's `area` and `check`.

## auto

For repositories where you want to be interrupted only when it matters. GitHub only in this version. Each `publish` ends in one of three actions:

| Action | When | What happens |
| --- | --- | --- |
| `merge` | The tier calls for no reviewers, the author is on `authors.trusted`, a review of the current head commit has no important findings, and every other check is green | Merged with `auto.merge_method`, pinned to the reviewed commit |
| `hold` | The tier calls for a reviewer, the author is not trusted, an agent opened it with nobody assigned, the review has an important finding, a check failed, or the host refused the merge | `auto.notify` are assigned and mentioned, once |
| `wait` | A draft, no review of the current commit yet, or checks still running | Nothing. Run `publish` again later, or pass `--wait SECONDS`. |

### Deciding how much reaches you

`[reviewers]` is the dial. With the defaults (low 0, medium 1, high 2), only low-risk changes merge on their own, and everything unlisted is medium. To be told only about the sensitive paths:

```toml
[reviewers]
low = 0
medium = 0
high = 1
```

Medium-risk changes then merge too once the agent's review is clean, and high-risk ones come to you. Run `replay` first to see what share of your history lands in each tier.

### What is enforced, and what is a convention

- **Enforced by this tool:** it will not merge without a clean review of the exact head commit, and the merge call names that commit, so a push after the review makes the host refuse.
- **Not enforced by this tool:** anything else that can merge. If an authoring agent or a person merges directly, nothing here stops it. Give agents the rule in their instructions: merge only through `publish`, and never work around a hold.
- **To make a hold a lock,** protect the branch on the host and require a check that only passes when `publish` says so. That is not built here.
- **The review is only as independent as you make it.** Run the reviewing agent in a context that did not write the change. The deterministic tier is the floor either way: a high-risk path is held no matter what a review says.

### Running from GitHub Actions

- Tell `publish` not to wait for the job it runs in: `--ignore-check NAME` or `auto.ignore_checks`.
- A merge made with the workflow's own token does not trigger other workflows, so a deploy on push will not run for it. Run `publish` with a different token, or from outside Actions, if merges must trigger deploys.
- On the `pull_request` event the workflow file comes from the pull request. The tier map does not.
