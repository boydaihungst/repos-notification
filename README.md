# GitHub Merge Watcher

A standalone GitHub Actions project that watches **public repositories you do not own** and notifies you when pull requests are merged into selected branches.

Supported notification channels:

- Discord webhook
- Email via SMTP
- GitHub Issues in this watcher repository (GitHub notification)

Polling runs hourly (at minute 17 UTC) and can also be started manually.

## Features

- Multiple repositories and multiple target branches.
- Detects merged pull requests using the GitHub REST API.
- Stores the latest merged commit SHA, merge time, and PR number per branch.
- Durable YAML outbox with per-channel delivery status.
- Retries failed channels without resending channels already marked successful.
- First-run baseline avoids sending historical notifications by default.
- Manual dry-run and initialization modes.
- No workflow or installation is required in the repositories being watched.

## Repository layout

```text
.
├── .github/workflows/merge-watcher.yml
├── config/repositories.yml
├── scripts/watcher.py
├── requirements.txt
└── README.md
```

## 1. Create the watcher repository

Create a repository on GitHub, for example `your-user/github-merge-watcher`.

You can make the watcher repository private. The repositories being watched can remain public.

Upload these project files to the watcher repository.

## 2. Configure repositories and branches

Edit `config/repositories.yml`:

```yaml
repositories:
  - owner: torvalds
    repo: linux
    branches:
      - name: master
        latest_merged_commit: null
        latest_merged_at: null
        latest_merged_pr: null
        notify_existing: false

  - owner: microsoft
    repo: vscode
    branches:
      - name: main
        latest_merged_commit: null
        latest_merged_at: null
        latest_merged_pr: null
        notify_existing: false
```

Fields:

| Field                  | Meaning                                                                 |
| ---------------------- | ----------------------------------------------------------------------- |
| `owner`                | Repository owner or organization                                        |
| `repo`                 | Repository name                                                         |
| `branches[].name`      | Base branch to monitor                                                  |
| `latest_merged_commit` | Last observed merge commit SHA; maintained by the script                |
| `latest_merged_at`     | Last observed merge timestamp; maintained by the script                 |
| `latest_merged_pr`     | Last observed merged PR number; maintained by the script                |
| `notify_existing`      | If true on first run, enqueue historical merged PRs returned by the API |

For a new branch, set the three `latest_merged_*` values to `null`. On the first normal run, the watcher establishes a baseline without notifying historical merges. Later merges are notified.

The script also stores `outbox` and `processed_merge_ids` at the top level of this same YAML file. Do not manually delete pending outbox entries unless you intentionally want to discard notifications.

## 3. Configure GitHub Actions permissions

In the watcher repository, open:

**Settings → Actions → General → Workflow permissions**

Select **Read and write permissions**.

The workflow also declares:

- `contents: write` to commit updated YAML state.
- `issues: write` to create GitHub notification Issues.

If your organization restricts Actions permissions, its policy must allow these permissions.

## 4. Configure Secrets and Variables

Open:

**Settings → Secrets and variables → Actions**

Add the following repository secrets.

### Required for the selected three-channel setup

| Secret                | Example                                | Purpose                                |
| --------------------- | -------------------------------------- | -------------------------------------- |
| `DISCORD_WEBHOOK_URL` | `https://discord.com/api/webhooks/...` | Discord incoming webhook               |
| `DISCORD_NOTIFY`      | `1` (default)                          | Disable/Enable discord webhook         |
| `EMAIL_NOTIFY`        | `1` (default)                          | Disable/Enable email smtp notification |
| `SMTP_HOST`           | `smtp.gmail.com`                       | SMTP server                            |
| `SMTP_PORT`           | `587`                                  | STARTTLS port                          |
| `SMTP_USERNAME`       | `you@gmail.com`                        | SMTP login                             |
| `SMTP_PASSWORD`       | Gmail App Password                     | SMTP password                          |
| `SMTP_FROM`           | `you@gmail.com`                        | Sender address                         |
| `SMTP_TO`             | `you@gmail.com,other@example.com`      | Comma-separated recipients             |

For Gmail, enable 2-Step Verification and create an **App Password**. Do not use your normal Google account password. If your email provider requires implicit TLS on port 465, adapt `notify_email()` to use `smtplib.SMTP_SSL`.

### Optional GitHub read token

| Secret          | Purpose                                                       |
| --------------- | ------------------------------------------------------------- |
| `GH_READ_TOKEN` | Optional token to increase authenticated read API rate limits |

The watched repositories are public, so no token is strictly required to read them. Without `GH_READ_TOKEN`, the GitHub API unauthenticated rate limit applies (normally 60 requests/hour per IP). A fine-grained PAT can be used for public read access; grant only the minimum repository metadata / pull request read permissions supported by GitHub for the selected repositories. Never use a token with broad write permissions just to read public repositories.

The workflow's built-in `GITHUB_TOKEN` is used only to create Issues and commit state in the watcher repository.

### Optional GitHub mention variable

Add an Actions **repository variable**:

| Variable          | Example                |
| ----------------- | ---------------------- |
| `NOTIFY_USERNAME` | `your-github-username` |

The created Issue will mention this account. Leave it unset if you do not want a mention.

### GitHub notification behavior

The GitHub channel creates one Issue in the watcher repository for each merged PR. Repository watchers/subscribers can receive notifications according to their GitHub notification settings. This is not a direct call to GitHub's notification inbox API; GitHub does not provide a general API for creating arbitrary inbox notifications.

The workflow has `issues: write`. The script attempts to apply a `merge-watcher` label; if that label does not exist and GitHub rejects it, it retries without the label.

## 5. First run

1. Add your repository configuration.
2. Configure all three notification channels.
3. Open **Actions → GitHub Merge Watcher**.
4. Select **Run workflow**.

For a normal first run, leave both inputs false. Empty branch cursors are initialized to the latest merged PR found, without sending historical notifications.

To initialize explicitly, choose `initialize=true`. This saves the discovered baseline and discards pending events for those newly initialized branch cursors.

To test discovery without sending messages or changing the persisted state, choose `dry_run=true`. Dry-run prints the events it would enqueue. It does not commit state.

## 6. How the outbox works

Each discovered merge gets a stable event ID:

```text
owner/repo#PR_NUMBER@MERGE_COMMIT_SHA
```

Each event has independent delivery state:

```yaml
delivery:
  discord:
    status: sent
    sent_at: "..."
  email:
    status: pending
    last_error: "..."
  github:
    status: sent
    issue_number: 42
```

If Discord succeeds but SMTP fails, the next run retries email and GitHub only if those channels remain pending. Successfully delivered channels are skipped.

The workflow commits `config/repositories.yml` even if the watcher step fails, so successfully completed channel states and pending retry states can be persisted.

### Delivery guarantees

This is **at-least-once**, not mathematically exactly-once delivery. A process can be interrupted after a provider accepts a message but before the success state is committed. Discord and SMTP do not provide a universal idempotency key. The GitHub channel searches for its marker before creating an Issue to reduce duplicate Issues, but GitHub search can be eventually consistent.

## 7. Schedule

The workflow runs at minute 17 of every hour:

```yaml
schedule:
  - cron: "17 * * * *"
```

GitHub Actions schedules use UTC. GitHub may delay scheduled workflows during periods of high load, and scheduled workflows can be disabled for inactive repositories. Manual `workflow_dispatch` remains available.

## 8. API behavior and limits

The watcher lists closed PRs for each configured base branch, filters entries with `merged_at`, and uses `merge_commit_sha` as the merge commit identifier.

The API is sorted by `updated_at`, not `merged_at`. The script paginates and uses the stored merge timestamp as a conservative stopping watermark: a PR's `updated_at` cannot be earlier than its `merged_at`. A very active repository with many closed PRs can require multiple API pages.

For a large watch list, configure `GH_READ_TOKEN` and monitor API rate limits.

## 9. Important operational notes

- The workflow file and state file must be on the default branch for scheduled runs.
- Do not run multiple copies of this watcher against the same state repository. Workflow concurrency prevents overlapping runs within this repository.
- If you rename a branch, update its `name` in the YAML. The old cursor is not automatically migrated.
- If a repository is renamed, update `owner` and `repo`.
- If you manually reset a branch cursor to `null`, the next run treats it as a new baseline unless `notify_existing: true`.
- Protect the watcher repository and its Actions secrets. Anyone who can change the workflow may be able to access secrets during a run.
- GitHub Actions schedule has a minimum practical interval of one hour for this configuration.

## Troubleshooting

### No GitHub Issues are created

Check that Actions workflow permissions are set to **Read and write permissions**, and that the workflow has `issues: write`. Also check organization policy.

### Email delivery fails

Check SMTP host, port, credentials, sender authorization, and whether your provider requires an App Password. The error is saved under the event's email delivery state.

### Discord delivery fails

Check the webhook URL and whether the webhook still exists. Do not publish the webhook URL; anyone possessing it can post to that channel.

### No historical PRs are notified

This is intentional for first-run baseline initialization. Set `notify_existing: true` for that branch if you explicitly want historical merges enqueued.

### API rate limit

Set `GH_READ_TOKEN` to a least-privilege token and reduce the number of watched repositories or branches if necessary.
