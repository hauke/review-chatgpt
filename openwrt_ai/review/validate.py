#!/usr/bin/env python3
"""Validate a review and emit a manual publishing preview. This script cannot publish."""

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from openwrt_ai.common import (
    BOT,
    BOT_ID,
    REVIEWER,
    Policy,
    commit_sha,
    config_argument,
    emit,
    last_review,
    marker,
    positive_int,
    read_json,
    require,
    run_cli,
    snapshot,
)
from openwrt_ai.connector import client_argument, github_client
from openwrt_ai.review.history import read_history

HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)\Z")


def exact_keys(value, required, optional=()):
    require(isinstance(value, dict), "expected an object")
    require(
        set(required) <= value.keys() and not value.keys() - set(required) - set(optional),
        "missing or unknown object fields",
    )


def recent_evidence(value, now):
    stamp = datetime.fromisoformat(value)
    require(
        stamp.tzinfo is not None and 0 <= (now - stamp).total_seconds() <= 300,
        "connector evidence is stale or from the future",
    )


def verify_identity(identity, now):
    exact_keys(identity, ("login", "id", "observed_at"))
    require(
        identity["login"] == BOT and identity["id"] == BOT_ID,
        "GitHub connection must authenticate as openwrt-ai",
    )
    recent_evidence(identity["observed_at"], now)


def diff_locations(patch):
    """Map LEFT/RIGHT line numbers to hunks, rejecting missing/truncated patches."""
    require(isinstance(patch, str) and patch, "inline file has no complete textual patch")
    locations = {}
    hunk, old, new, left, right = -1, 0, 0, 0, 0
    for text in patch.splitlines():
        match = HUNK.fullmatch(text)
        if match:
            require(left == 0 and right == 0, "truncated diff hunk")
            old, new = int(match[1]), int(match[3])
            left = int(match[2]) if match[2] is not None else 1
            right = int(match[4]) if match[4] is not None else 1
            hunk += 1
            continue
        require(hunk >= 0, "invalid diff patch")
        if text == "\\ No newline at end of file":
            continue
        require(bool(text) and text[0] in " +-", "invalid diff hunk line")
        if text[0] in " -":
            require(left > 0, "diff old-side count mismatch")
            locations[("LEFT", old)] = hunk
            old, left = old + 1, left - 1
        if text[0] in " +":
            require(right > 0, "diff new-side count mismatch")
            locations[("RIGHT", new)] = hunk
            new, right = new + 1, right - 1
    require(hunk >= 0 and left == 0 and right == 0, "truncated diff patch")
    return locations


def validate_comment(comment, files, policy):
    exact_keys(comment, ("path", "line", "side", "body", "kind"), ("start_line", "start_side"))
    path = comment["path"]
    require(
        isinstance(path, str)
        and path
        and not path.startswith("/")
        and not any(part in ("", ".", "..") for part in path.split("/"))
        and "\\" not in path
        and not any(ord(c) < 32 for c in path)
        and str(PurePosixPath(path)) == path,
        "invalid inline path",
    )
    require(path in files, "inline path is absent from the current PR diff")
    line = positive_int(comment["line"], "inline line")
    side = comment["side"]
    require(side in ("LEFT", "RIGHT"), "invalid inline side")
    locations = diff_locations(files[path].get("patch"))
    require((side, line) in locations, "inline line is outside the PR diff")
    has_start = "start_line" in comment
    require(has_start == ("start_side" in comment), "incomplete inline range")
    if has_start:
        start = positive_int(comment["start_line"], "inline start line")
        require(comment["start_side"] == side and start < line, "invalid inline range")
        require(line - start <= 100, "inline range is too large")
        require(
            all(
                locations.get((side, position)) == locations[(side, line)]
                for position in range(start, line + 1)
            ),
            "inline range crosses diff hunks",
        )
    body = comment["body"]
    require(
        isinstance(body, str)
        and body.strip()
        and len(body) <= policy.max_comment_chars
        and "\x00" not in body,
        "invalid inline comment size",
    )
    require(comment["kind"] in ("issue", "nit", "ci"), "invalid finding kind")
    require(comment["kind"] != "nit" or body.startswith("nit:"), "nit must have nit: prefix")
    return {key: value for key, value in comment.items() if key != "kind"}


def reply_previews(proposals, evidence, reviews, comments, repository, number, head, now):
    if not proposals:
        return []
    exact_keys(evidence, ("observed_at", "repository", "number", "head_sha", "threads"))
    recent_evidence(evidence["observed_at"], now)
    require(
        (evidence["repository"], evidence["number"], evidence["head_sha"])
        == (repository, number, head),
        "thread evidence targets a different PR or SHA",
    )
    require(isinstance(evidence["threads"], list), "invalid thread evidence")
    own_reviews = {review["id"] for review in reviews if last_review([review])}
    roots = {comment["id"]: comment for comment in comments if not comment.get("in_reply_to_id")}
    replied = {comment.get("in_reply_to_id") for comment in comments}
    result, seen = [], set()
    for proposal in proposals:
        exact_keys(proposal, ("comment_id", "evidence"))
        comment_id = positive_int(proposal["comment_id"], "thread comment ID")
        require(comment_id not in seen, "duplicate reply proposal")
        seen.add(comment_id)
        require(
            isinstance(proposal["evidence"], str)
            and proposal["evidence"].strip()
            and len(proposal["evidence"]) <= 1000,
            "missing evidence that finding is fixed",
        )
        root = roots.get(comment_id)
        require(
            root
            and root.get("user", {}).get("login") == BOT
            and root.get("user", {}).get("id") == BOT_ID
            and root.get("pull_request_review_id") in own_reviews,
            "reply target is not a ChatGPT finding",
        )
        require(comment_id not in replied, "someone has already replied to the finding")
        thread = next(
            (thread for thread in evidence["threads"] if thread.get("comment_id") == comment_id),
            None,
        )
        require(
            thread and thread.get("is_resolved") is False and thread.get("comment_count") == 1,
            "thread must be unresolved with exactly one comment",
        )
        result.append(
            {
                "repo_full_name": repository,
                "pr_number": number,
                "comment_id": comment_id,
                "body": "fixed, thanks",
            }
        )
    return result


def validate(findings, manifest, policy, api, identity, threads=None, now=None):
    now = now or datetime.now(UTC)
    verify_identity(identity, now)
    exact_keys(
        findings,
        (
            "version",
            "reviewer",
            "repository",
            "number",
            "head_sha",
            "base_sha",
            "status",
            "action",
            "comments",
            "commit_checks",
            "fixed_threads",
        ),
    )
    require(
        findings["version"] == 1 and findings["reviewer"] == REVIEWER,
        "unsupported findings version or reviewer",
    )
    require(
        findings["status"] == "complete",
        "incomplete analysis cannot mark a PR reviewed",
    )
    require(findings["action"] == "COMMENT", "only COMMENT reviews are permitted")
    repository = policy.allow(findings["repository"])
    number = positive_int(findings["number"], "PR number")
    head, base = commit_sha(findings["head_sha"]), commit_sha(findings["base_sha"])
    require(
        manifest.get("version") == 1
        and manifest.get("reviewer") == REVIEWER
        and manifest.get("mode") == "dry-run",
        "invalid discovery manifest",
    )
    if api.source == "connector-snapshot":
        require(
            api.observed_at > datetime.fromisoformat(manifest["generated_at"]),
            "capture new connector observations after discovery for validation",
        )
    expected = [
        item
        for item in manifest["prs"]
        if item["repository"] == repository and item["number"] == number
    ]
    require(
        len(expected) == 1 and expected[0]["head_sha"] == head and expected[0]["base_sha"] == base,
        "findings do not match discovery snapshot",
    )
    api.public_repo(repository)
    endpoint = f"/repos/{repository}/pulls/{number}"

    def current_pr():
        pr = api.get(endpoint)
        current = snapshot(repository, pr)
        require(
            current["number"] == number
            and current["head_sha"] == head
            and current["base_sha"] == base,
            "PR head or base changed; rediscover and review",
        )
        return pr

    pr = current_pr()
    reviews, _, existing_inline, claude, completed = read_history(api, repository, number)
    require(not claude, "Claude or legacy AI already commented; skip this PR")
    require(
        not completed or completed["commit_id"] != head,
        "this head already has a completed ChatGPT review",
    )
    file_list = list(api.pages(endpoint + "/files"))
    require(
        len(file_list) == pr["changed_files"],
        "incomplete GitHub file list; cannot validate",
    )
    files = {file["filename"]: file for file in file_list}
    comments, checks, fixes = (
        findings[key] for key in ("comments", "commit_checks", "fixed_threads")
    )
    require(
        all(isinstance(items, list) for items in (comments, checks, fixes)),
        "findings collections must be arrays",
    )
    require(all(isinstance(comment, dict) for comment in comments), "invalid inline finding")
    require(
        len(comments) + len(fixes) <= policy.max_comments,
        "too many comments or replies",
    )
    require(len(checks) <= policy.max_commit_checks, "too many commit checks")
    require(
        sum(comment.get("kind") == "ci" for comment in comments) <= 5,
        "too many CI findings",
    )
    inline, seen = [], set()
    for comment in comments:
        payload = validate_comment(comment, files, policy)
        fingerprint = json.dumps(payload, sort_keys=True)
        require(fingerprint not in seen, "duplicate inline finding")
        seen.add(fingerprint)
        inline.append(payload)
    body_lines = [f"Reviewed commit: `{head}`", ""]
    if checks:
        require(
            pr["commits"] <= 250,
            "GitHub commit list is capped; cannot validate commit checks",
        )
        commits = {commit["sha"] for commit in api.pages(endpoint + "/commits")}
        require(len(commits) == pr["commits"], "incomplete GitHub commit list")
        body_lines.append("## Commit checks")
        for check in checks:
            exact_keys(check, ("sha", "message"))
            require(commit_sha(check["sha"]) in commits, "commit check is outside this PR")
            message = check["message"]
            require(
                isinstance(message, str)
                and message.strip()
                and len(message) <= 600
                and not any(char in message for char in "\r\n\x00")
                and "<!--" not in message,
                "invalid commit-check message",
            )
            body_lines.append(f"- {check['sha'][:12]}: {message}")
    if completed and inline:
        # Reuse the existing comment/review instead of opening another review
        # just to attach new inline comments. Locations remain diff-validated.
        body_lines.extend(["", "## Findings"])
        for comment in inline:
            body_lines.append(
                f"- `{comment['path']}:{comment['line']}` ({comment['side']}): {comment['body']}"
            )
    body_lines.extend(["", marker(head)])
    body = "\n".join(body_lines).strip()
    require(len(body) <= 10000, "review body is too large")
    replies = reply_previews(
        fixes,
        threads,
        reviews,
        existing_inline if fixes else [],
        repository,
        number,
        head,
        now,
    )
    current_pr()  # Catch changes during the API reads as well as before them.
    payload = {
        "repo_full_name": repository,
        "pr_number": number,
        "action": "COMMENT",
        "commit_id": head,
        "review": body,
        "file_comments": inline,
    }
    update = None
    if completed:
        old_body = completed.get("body") or ""
        extended = old_body + "\n\n---\n\n" + body
        require(
            len(extended) <= 60000,
            "extended bot comment exceeds size limit; archive history manually",
        )
        update = {
            "repo_full_name": repository,
            "pr_number": number,
            "comment_kind": completed["comment_kind"],
            "comment_id": completed["id"],
            "reviewed_commit": head,
            "body": extended,
            "expected_body_sha256": hashlib.sha256(old_body.encode()).hexdigest(),
        }
        payload = None
    digest = hashlib.sha256(
        json.dumps(
            {"review": payload, "comment_update": update, "replies": replies},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return {
        "version": 1,
        "mode": "dry-run",
        "publishing_enabled": False,
        "github_read_source": api.source,
        "live_revalidation": api.source == "authenticated-rest",
        "validated_at": now.isoformat(),
        "identity_from_connector_evidence": BOT,
        "expected_base_sha": base,
        "payload_sha256": digest,
        "review_preview": payload,
        "comment_update_preview": update,
        "reply_previews": replies,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    config_argument(parser)
    client_argument(parser)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument(
        "--identity",
        required=True,
        type=Path,
        help="recent authenticated GitHub connector identity observation",
    )
    parser.add_argument(
        "--threads", type=Path, help="recent connector thread observations for replies"
    )
    parser.add_argument("findings", type=Path)
    args = parser.parse_args()
    policy = Policy.load(args.config)
    emit(
        validate(
            read_json(args.findings),
            read_json(args.manifest),
            policy,
            github_client(args, policy),
            read_json(args.identity),
            read_json(args.threads) if args.threads else None,
        )
    )


if __name__ == "__main__":
    run_cli(main)
