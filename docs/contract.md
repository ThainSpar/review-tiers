# Contract

Two JSON documents connect `review-tiers` to a reviewing agent. Both carry `"schema": 1` or are validated strictly, so a mismatch fails loudly.

## The brief: `review-tiers brief`

Printed to standard output. It names paths, so treat it like the change itself.

| Key | Meaning |
| --- | --- |
| `host`, `repo`, `change` | Where the change lives and its number |
| `head_sha` | The commit this brief describes. The review must echo it. |
| `target_branch` | The branch the change targets, and where the tier map was read from |
| `mode` | `recommend` or `auto` |
| `tier` | The tier after size and label escalations |
| `path_tier` | The tier from paths alone |
| `reviewers` | Human reviewers this tier calls for |
| `escalations` | Why `tier` differs from `path_tier`, in words |
| `changed_files`, `changed_lines` | Totals |
| `files_truncated` | True when the host did not list every file. The tier is then high. |
| `files` | Every changed file, riskiest first |
| `review_result` | An empty review result to fill in |

Each entry in `files`:

| Key | Meaning |
| --- | --- |
| `path` | The path that decided the tier (the old path, for a rename out of a sensitive area) |
| `tier` | This file's tier |
| `area`, `check` | From the matching rule: the part of the system, and what to check. May be null. |
| `rule` | The pattern that matched, or null when the default tier applied |
| `additions`, `deletions` | Line counts |
| `size_known` | False when the host withheld the diff; the counts are then not meaningful |

## The review result: `review-tiers publish --review FILE`

A JSON object. `--review -` reads it from standard input. Unknown keys are refused.

| Key | Required | Meaning |
| --- | --- | --- |
| `reviewed_sha` | For the review to count | The `head_sha` that was reviewed. If it is missing or no longer the head, the review is treated as out of date: it is not shown, and auto mode will not merge on it. |
| `tier` | No | `low`, `medium`, `high` or null. Raises the tier when higher than the path tier; ignored when lower. |
| `tier_reason` | With `tier` | Shown in the guide when the tier was raised |
| `summary` | No | A few sentences |
| `findings` | No | List of `{severity, title, path?, line?, detail?}`. `severity` is `important` or `nit`. |
| `focus` | No | List of `{path, why, line?}`: where a person should look |

Any `important` finding makes the review "findings"; otherwise it is "clean". Text from the review is flattened to single lines, escaped, and has mentions broken before it is posted.

## The result: what `publish` prints

Counts and actions only: no paths, logins or branch names, so it is safe in logs.

| Key | Meaning |
| --- | --- |
| `tier`, `path_tier`, `raised_by_review` | The final tier and how it got there |
| `reviewers` | Human reviewers the final tier calls for |
| `review` | `none`, `stale`, `clean` or `findings` |
| `action` | `recommend`, `merge`, `wait` or `hold` |
| `reasons` | Why it is waiting or held |
| `writes` | What was changed on the host, or would be with `--dry-run` |

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Done. Read `action` for what happened; a hold is still exit 0. |
| 1 | The host's API failed or could not be reached |
| 2 | The tier map or the review result is invalid, a token or address is missing, the mode is not supported on this host, or the change is closed or merged and `--dry-run` was not given |
| 3 | `replay` only: the circuit breaker tripped |
