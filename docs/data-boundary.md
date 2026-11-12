# Data boundary

What the tool reads, writes and sends, so it can be checked before use.

| Command | Reads | Writes | Sends elsewhere |
| --- | --- | --- | --- |
| `explain` | A local tier map | Nothing | Nothing. No network. |
| `brief` | One change, its file list and the tier map on its target branch | Nothing | Nothing beyond the host it reads from |
| `publish --dry-run` | The same, plus the change's comments, and in auto mode its checks | Nothing | Nothing |
| `publish` | The same | One comment and a label on that change. In auto mode also a merge, or an assignment. | Nothing |
| `replay` | Merged pull requests, their file lists and reviews | Its report, and the JSON file you name | Nothing |

- The only network destination is the host address you configure, over HTTPS. A plain-HTTP address is refused. There is no telemetry and no update check.
- Only the Python standard library is imported.
- The token is read from an environment variable and sent only to that host.
- The tool never checks out, builds or runs code from a change.

## What the outputs contain

- `publish` prints counts and actions only: no paths, logins or branch names.
- `brief` names every changed path. It is meant for an agent that can already read the change.
- The guide comment names changed paths, on a change that already shows them.
- `replay` prints counts, shares and times by default. `--tuning` adds a section that names paths and pull request numbers, and is marked internal.

Whatever reviewing agent you connect has its own data handling; that is outside this tool.
