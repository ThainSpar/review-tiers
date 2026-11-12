---
description: Review a pull request with review-tiers. Classifies it, reviews it in a fresh context, publishes the review guide and, in auto mode, merges or holds.
argument-hint: <pull request number>
disable-model-invocation: true
---

Review pull request $ARGUMENTS of this repository with review-tiers.

Setup this command assumes: Python 3.11+, the `gh` CLI signed in, and review-tiers installed (`pip install git+<address of the review-tiers repository>`) or its checkout on `PYTHONPATH`. Use `GH_TOKEN="$(gh auth token)"` for the commands below, and find `<owner/repo>` with `gh repo view --json nameWithOwner`.

1. **Brief.** Run `python3 -m review_tiers brief --host github --repo <owner/repo> --change $ARGUMENTS` and save the JSON to a temporary file outside the repository.

2. **Review in a fresh context.** Start a subagent that has not seen how this change was written. Give it the triage prompt from the review-tiers repository (`prompts/triage.md`), the brief, and the pull request number. It reads the diff itself and returns one JSON object. Save that to a second temporary file. Do not edit its findings or its tier.

3. **Publish.** Run `python3 -m review_tiers publish --host github --repo <owner/repo> --change $ARGUMENTS --review <review file> --wait 600`.

4. **Report the result's `action`:**
   - `merge`: it merged. Carry on with whatever follows a merge here.
   - `hold`: a person is needed and has been assigned. Stop. Do not merge it yourself by any other route.
   - `wait`: not ready (a draft, or checks still running after the wait). Say which, and stop.
   - `recommend`: the repository is in recommend mode; the guide is posted and nothing else happens.

If a command exits with an error, report the message and stop. Never work around a hold.
