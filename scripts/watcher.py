#!/usr/bin/env python3
"""Poll public GitHub repositories for merged PRs and notify configured channels."""

from __future__ import annotations

import json
import os
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "repositories.yml"
API = "https://api.github.com"
OUTBOX_LIMIT = 1000
PROCESSED_IDS_LIMIT = 500

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
DISCORD_NOTIFY = os.getenv("DISCORD_NOTIFY", "0") == "1"
EMAIL_NOTIFY = os.getenv("EMAIL_NOTIFY", "0") == "1"
_GITHUB_NOTIFY = os.getenv("_GITHUB_NOTIFY", "0") == "1"
GH_READ_TOKEN = os.getenv("GH_READ_TOKEN", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
INITIALIZE_ONLY = os.getenv("INITIALIZE_ONLY", "false").lower() == "true"

read_session = requests.Session()
read_session.headers.update(
    {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-merge-watcher/1.0",
    }
)
if GH_READ_TOKEN:
    read_session.headers["Authorization"] = f"Bearer {GH_READ_TOKEN}"

write_session = requests.Session()
write_session.headers.update(
    {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-merge-watcher/1.0",
    }
)
if GITHUB_TOKEN:
    write_session.headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"


def utc_now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Missing configuration file: {CONFIG_PATH}")
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(data.get("repositories", []), list):
        raise ValueError("'repositories' must be a YAML list")
    data.setdefault("repositories", [])
    data.setdefault("outbox", [])
    data.setdefault("processed_merge_ids", [])
    return data


def save_config(data: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110),
        encoding="utf-8",
    )


def api_get(url: str, params: dict[str, Any] | None = None) -> Any:
    for attempt in range(5):
        response = read_session.get(url, params=params, timeout=30)
        if response.status_code in (403, 429):
            remaining = response.headers.get("X-RateLimit-Remaining")
            reset = response.headers.get("X-RateLimit-Reset")
            if remaining == "0" and reset:
                wait = max(1, int(reset) - int(time.time()) + 2)
                if wait <= 90 and attempt < 4:
                    print(f"Rate limited; waiting {wait}s")
                    time.sleep(wait)
                    continue
        if response.status_code >= 500 and attempt < 4:
            time.sleep(2**attempt)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"GitHub API request failed after retries: {url}")


def fetch_closed_prs(
    owner: str, repo: str, branch: str, watermark: datetime | None
) -> list[dict[str, Any]]:
    """Fetch closed PRs newest-updated first.

    A merged PR's updated_at is never earlier than its merged_at. Once a full
    page reaches updated_at < the stored merged_at watermark, older pages
    cannot contain a merge newer than that watermark.
    """
    url = f"{API}/repos/{owner}/{repo}/pulls"
    page = 1
    found: list[dict[str, Any]] = []

    while True:
        items = api_get(
            url,
            {
                "state": "closed",
                "base": branch,
                "sort": "updated",
                "direction": "desc",
                "per_page": 100,
                "page": page,
            },
        )
        if not items:
            break

        found.extend(
            pr
            for pr in items
            if pr.get("merged_at") and pr.get("base", {}).get("ref") == branch
        )

        # API ordering is by updated_at, not merged_at. Since merged_at <=
        # updated_at, crossing below the watermark is a safe stopping point.
        if watermark and len(items) == 100:
            oldest_updated = min(
                (
                    parse_dt(pr.get("updated_at"))
                    for pr in items
                    if pr.get("updated_at")
                ),
                default=None,
            )
            if oldest_updated and oldest_updated < watermark:
                break

        if len(items) < 100:
            break
        page += 1

    # De-duplicate and sort chronologically for deterministic notifications.
    unique = {pr["id"]: pr for pr in found}
    return sorted(
        unique.values(),
        key=lambda pr: (pr.get("merged_at") or "", pr.get("number", 0)),
    )


def merge_event(
    owner: str, repo: str, branch: str, pr: dict[str, Any]
) -> dict[str, Any]:
    sha = pr.get("merge_commit_sha") or ""
    identity = f"{owner}/{repo}#{pr['number']}@{sha or pr['merged_at']}"
    return {
        "id": identity,
        "owner": owner,
        "repo": repo,
        "branch": branch,
        "pr_number": pr["number"],
        "pr_title": pr.get("title") or "(untitled)",
        "pr_url": pr.get("html_url") or "",
        "commit_sha": sha,
        "commit_url": f"https://github.com/{owner}/{repo}/commit/{sha}" if sha else "",
        "author": (pr.get("user") or {}).get("login", "unknown"),
        "merged_at": pr.get("merged_at"),
        "discovered_at": utc_now(),
        "delivery": {
            "discord": {"status": "pending"},
            "email": {"status": "pending"},
            "github": {"status": "pending"},
        },
    }


def branch_key(owner: str, repo: str, branch: str) -> str:
    return f"{owner}/{repo}:{branch}"


def discover(config: dict[str, Any]) -> None:
    outbox = config["outbox"]
    processed = set(config["processed_merge_ids"])
    outbox_ids = {item["id"] for item in outbox}

    for repository in config["repositories"]:
        owner = repository["owner"]
        repo = repository["repo"]
        for branch_cfg in repository.get("branches", []):
            branch = branch_cfg["name"]
            key = branch_key(owner, repo, branch)
            watermark = parse_dt(branch_cfg.get("latest_merged_at"))
            print(f"Checking {key}; watermark={branch_cfg.get('latest_merged_at')}")

            prs = fetch_closed_prs(owner, repo, branch, watermark)
            if not prs:
                continue

            # First run: establish a baseline instead of flooding notifications
            # with historical merges, unless notify_existing is explicitly true.
            if not branch_cfg.get("latest_merged_at"):
                newest = prs[-1]
                if branch_cfg.get("notify_existing", False) and not INITIALIZE_ONLY:
                    candidates = prs
                else:
                    candidates = []
                    print(f"Initialized baseline for {key} at PR #{newest['number']}")
                branch_cfg["latest_merged_at"] = newest["merged_at"]
                branch_cfg["latest_merged_commit"] = newest.get("merge_commit_sha")
                branch_cfg["latest_merged_pr"] = newest["number"]
                # Remember every PR sharing the baseline timestamp so that
                # equal-timestamp historical merges are not mistaken for new ones.
                for pr in prs:
                    if pr.get("merged_at") == newest.get("merged_at"):
                        processed.add(
                            f"{owner}/{repo}#{pr['number']}@{pr.get('merge_commit_sha') or pr['merged_at']}"
                        )
            else:
                candidates = []
                if not INITIALIZE_ONLY:
                    for pr in prs:
                        merged_at = parse_dt(pr.get("merged_at"))
                        if merged_at and (
                            merged_at > watermark or merged_at == watermark
                        ):
                            candidates.append(pr)

            for pr in candidates:
                event = merge_event(owner, repo, branch, pr)
                if event["id"] not in processed and event["id"] not in outbox_ids:
                    outbox.append(event)
                    outbox_ids.add(event["id"])
                    print(f"Discovered {event['id']}")

            # Advance branch cursor to the newest merge discovered from the API.
            newest = max(prs, key=lambda p: p.get("merged_at") or "")
            newest_at = newest.get("merged_at")
            current_at = branch_cfg.get("latest_merged_at")
            if newest_at and (not current_at or newest_at > current_at):
                branch_cfg["latest_merged_at"] = newest_at
                branch_cfg["latest_merged_commit"] = newest.get("merge_commit_sha")
                branch_cfg["latest_merged_pr"] = newest["number"]

    config["processed_merge_ids"] = list(processed)


def notify_discord(event: dict[str, Any]) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL secret is not configured")
    sha = event["commit_sha"]
    description = (
        f"**[{event['owner']}/{event['repo']}]({event['pr_url']})**\n"
        f"**Branch:** `{event['branch']}`\n"
        f"**PR:** [#{event['pr_number']} — {event['pr_title']}]({event['pr_url']})\n"
        f"**Author:** @{event['author']}\n"
        f"**Merged:** {event['merged_at']}\n"
        f"**Commit:** [`{sha[:12]}`]({event['commit_url']})"
        if sha
        else f"**[{event['owner']}/{event['repo']}]({event['pr_url']})**\n"
        f"**Branch:** `{event['branch']}`\n"
        f"**PR:** [#{event['pr_number']} — {event['pr_title']}]({event['pr_url']})\n"
        f"**Merged:** {event['merged_at']}"
    )
    payload = {
        "username": "GitHub Merge Watcher",
        "embeds": [
            {
                "title": "Pull request merged",
                "description": description[:4000],
                "url": event["pr_url"],
                "footer": {"text": event["id"][:200]},
            }
        ],
        "allowed_mentions": {"parse": []},
    }
    response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
    response.raise_for_status()


def notify_email(event: dict[str, Any]) -> None:
    required = ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TO"]
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        raise RuntimeError("Missing email settings: " + ", ".join(missing))

    recipients = [x.strip() for x in os.environ["SMTP_TO"].split(",") if x.strip()]
    if not recipients:
        raise RuntimeError("SMTP_TO has no valid recipients")

    message = EmailMessage()
    message["Subject"] = (
        f"[Merge] {event['owner']}/{event['repo']} "
        f"{event['branch']} — PR #{event['pr_number']}"
    )
    message["From"] = os.environ["SMTP_FROM"]
    message["To"] = ", ".join(recipients)

    lines = [
        f"Repository: {event['owner']}/{event['repo']}",
        f"Target branch: {event['branch']}",
        f"Pull request: #{event['pr_number']} — {event['pr_title']}",
        f"Author: {event['author']}",
        f"Merged at: {event['merged_at']}",
        f"PR URL: {event['pr_url']}",
        f"Merge commit: {event['commit_sha']}",
        f"Commit URL: {event['commit_url']}",
        f"Event ID: {event['id']}",
    ]
    message.set_content("\n".join(lines))

    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(message)


def github_issue_exists(repo_full_name: str, marker: str) -> dict[str, Any] | None:
    # Search API makes the create-then-crash retry window less likely to
    # produce duplicate Issues. Search may be eventually consistent.
    query = f'repo:{repo_full_name} is:issue in:body "{marker}"'
    response = write_session.get(
        f"{API}/search/issues",
        params={"q": query, "per_page": 10},
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    for issue in result.get("items", []):
        if marker in (issue.get("body") or ""):
            return issue
    return None


def notify_github_issue(event: dict[str, Any]) -> int:
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN is unavailable")
    repo_full_name = os.getenv("GITHUB_REPOSITORY", "").strip()
    if not repo_full_name or "/" not in repo_full_name:
        raise RuntimeError("GITHUB_REPOSITORY is not set by GitHub Actions")

    marker = f"merge-watch-id:{event['id']}"
    existing = github_issue_exists(repo_full_name, marker)
    if existing:
        return int(existing["number"])

    username = os.getenv("NOTIFY_USERNAME", "").strip().lstrip("@")
    mention = f"@{username} " if username else ""
    body = (
        f"{mention}A pull request has been merged into a watched branch.\n\n"
        f"- **Repository:** [{event['owner']}/{event['repo']}](https://github.com/{event['owner']}/{event['repo']})\n"
        f"- **Branch:** `{event['branch']}`\n"
        f"- **Pull request:** [#{event['pr_number']} — {event['pr_title']}]({event['pr_url']})\n"
        f"- **Merged at:** {event['merged_at']}\n"
        f"- **Merge commit:** [`{event['commit_sha'][:12]}`]({event['commit_url']})\n\n"
        f"<!-- {marker} -->"
    )
    response = write_session.post(
        f"{API}/repos/{repo_full_name}/issues",
        json={
            "title": f"[Merge] {event['owner']}/{event['repo']} {event['branch']} PR #{event['pr_number']}",
            "body": body,
            "labels": ["merge-watcher"],
        },
        timeout=30,
    )
    # A missing label is a common first-run issue; retry without labels.
    if response.status_code == 422 and "label" in response.text.lower():
        response = write_session.post(
            f"{API}/repos/{repo_full_name}/issues",
            json={
                "title": f"[Merge] {event['owner']}/{event['repo']} {event['branch']} PR #{event['pr_number']}",
                "body": body,
            },
            timeout=30,
        )
    response.raise_for_status()
    return int(response.json()["number"])


def deliver_channel(channel: str, event: dict[str, Any]) -> None:
    if channel == "discord":
        notify_discord(event)
    elif channel == "email":
        notify_email(event)
    elif channel == "github":
        number = notify_github_issue(event)
        event["delivery"][channel]["issue_number"] = number
    else:
        raise ValueError(f"Unknown notification channel: {channel}")


def process_outbox(config: dict[str, Any]) -> bool:
    channels = ()
    if DISCORD_NOTIFY:
        channels = (*channels, "discord")
    if EMAIL_NOTIFY:
        channels = (*channels, "email")
    if _GITHUB_NOTIFY:
        channels = (*channels, "github")
    all_ok = True
    processed = set(config["processed_merge_ids"])

    for event in config["outbox"]:
        for channel in channels:
            state = event["delivery"].setdefault(channel, {"status": "pending"})
            if state.get("status") == "sent":
                continue
            if DRY_RUN:
                print(f"[DRY RUN] Would send {event['id']} to {channel}")
                continue
            try:
                deliver_channel(channel, event)
                state["status"] = "sent"
                state["sent_at"] = utc_now()
                state.pop("last_error", None)
                print(f"Delivered {event['id']} to {channel}")
            except Exception as exc:  # Keep pending; retry on next scheduled run.
                state["status"] = "pending"
                state["last_error"] = str(exc)[:500]
                state["last_attempt_at"] = utc_now()
                all_ok = False
                print(
                    f"ERROR delivering {event['id']} to {channel}: {exc}",
                    file=sys.stderr,
                )

        if all(
            event["delivery"].get(ch, {}).get("status") == "sent" for ch in channels
        ):
            processed.add(event["id"])

    config["processed_merge_ids"] = list(processed)[-PROCESSED_IDS_LIMIT:]
    # Retain pending events and a bounded history of completed events.
    pending = [
        event
        for event in config["outbox"]
        if not all(
            event["delivery"].get(ch, {}).get("status") == "sent" for ch in channels
        )
    ]
    completed = [
        event
        for event in config["outbox"]
        if all(event["delivery"].get(ch, {}).get("status") == "sent" for ch in channels)
    ][-100:]
    config["outbox"] = completed + pending
    return all_ok


def write_summary(config: dict[str, Any]) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    pending = sum(
        1
        for event in config["outbox"]
        if not all(
            event["delivery"].get(ch, {}).get("status") == "sent"
            for ch in ("discord", "email", "github")
        )
    )
    lines = [
        "## GitHub Merge Watcher",
        "",
        f"- Checked at: `{utc_now()}`",
        f"- Watched repositories: **{len(config['repositories'])}**",
        f"- Pending outbox events: **{pending}**",
        f"- Dry run: **{DRY_RUN}**",
    ]
    Path(summary_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    config = load_config()
    if DRY_RUN:
        print("DRY_RUN enabled: no notifications and no state changes will be saved.")
        before = json.dumps(config, sort_keys=True)
        discover(config)
        print(f"Discovered outbox events: {len(config['outbox'])}")
        print(json.dumps(config["outbox"], indent=2, ensure_ascii=False))
        return 0

    discover(config)

    if INITIALIZE_ONLY:
        # Clear any events discovered during explicit initialization. Branch
        # cursors are retained, so historical merges won't notify later.
        # discover() suppresses all new events in this mode. Existing outbox
        # entries are preserved so an explicit initialization cannot discard
        # notifications that were already waiting for retry.
        save_config(config)
        print("Initialization complete; baseline state saved.")
        write_summary(config)
        return 0

    process_outbox(config)
    save_config(config)
    write_summary(config)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"FATAL: {error}", file=sys.stderr)
        raise SystemExit(1)
