"""Publish a validated PR review using a dedicated openwrt-ai credential."""

import argparse
import fcntl
import hashlib
import os
import stat
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from openwrt_ai.common import (
    GitHub,
    GitHubHTTPError,
    Policy,
    ReviewError,
    commit_sha,
    config_argument,
    emit,
    last_review,
    positive_int,
    read_json,
    repo_name,
    require,
    run_cli,
    snapshot,
)
from openwrt_ai.review.history import own_author, read_history
from openwrt_ai.review.validate import validate

PUBLISH_TOKEN_ENV = "CHATGPT_REVIEW_PUBLISH_TOKEN"


def comment_endpoint(repository, number, kind, comment_id):
    repo = f"/repos/{repo_name(repository)}"
    number = positive_int(number, "PR number")
    comment_id = positive_int(comment_id, "existing comment ID")
    require(kind in ("review", "issue"), "unknown existing comment kind")
    if kind == "review":
        return f"{repo}/pulls/{number}/reviews/{comment_id}"
    return f"{repo}/issues/comments/{comment_id}"


class GitHubPublisher(GitHub):
    def __init__(self):
        token = os.environ.get(PUBLISH_TOKEN_ENV)
        require(token and all(33 <= ord(c) <= 126 for c in token), f"set {PUBLISH_TOKEN_ENV}")
        super().__init__(token=token)

    def create_review(self, repository, number, head, body, comments):
        self.get("/user")
        endpoint = f"/repos/{repo_name(repository)}/pulls/{positive_int(number, 'PR number')}"
        return self._request(
            endpoint + "/reviews",
            method="POST",
            payload={
                "event": "COMMENT",
                "commit_id": commit_sha(head),
                "body": body,
                "comments": comments,
            },
        )

    def update_body(self, repository, number, kind, comment_id, body):
        self.get("/user")
        return self._request(
            comment_endpoint(repository, number, kind, comment_id),
            method="PUT" if kind == "review" else "PATCH",
            payload={"body": body},
        )


@contextmanager
def pr_lock(directory, repository, number):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    require(
        stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
        "publication state directory must be private and owned by the publisher account",
    )
    name = repository.replace("/", "__") + f"__{number}"
    fd = os.open(directory / (name + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w"):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ReviewError("a publisher is already running for this PR") from error
        yield directory / (name + ".pending")


def publish(findings, manifest, preview, policy, api, state_dir):
    require(findings.get("fixed_threads") == [], "automatic thread replies are unsupported")
    require(
        preview.get("version") == 1
        and preview.get("mode") == "dry-run"
        and preview.get("publishing_enabled") is False,
        "a validated dry-run preview is required",
    )
    repository = policy.allow(findings["repository"])
    number = positive_int(findings["number"], "PR number")
    head, base = commit_sha(findings["head_sha"]), commit_sha(findings["base_sha"])
    endpoint = f"/repos/{repository}/pulls/{number}"
    result = {"repository": repository, "number": number, "reviewed_commit": head}

    def check_pr():
        current = snapshot(repository, api.get(endpoint))
        require(
            (current["number"], current["head_sha"], current["base_sha"]) == (number, head, base),
            "PR head or base changed; rediscover and review",
        )

    with pr_lock(state_dir, repository, number) as pending:
        identity = {**api.get("/user"), "observed_at": datetime.now(UTC).isoformat()}
        api.public_repo(repository)
        check_pr()
        _, _, _, claude, previous = read_history(api, repository, number)
        require(not claude, "Claude or legacy AI already commented; skip this PR")
        if pending.exists():
            require(
                previous and pending.read_text(encoding="ascii") == previous["commit_id"],
                f"previous publication outcome is unknown; reconcile GitHub before clearing {pending}",
            )
            pending.unlink()
        if previous and previous["commit_id"] == head:
            return {**result, "status": "already-published", "comment_id": previous["id"]}

        validated = validate(findings, manifest, policy, api, identity)
        for key in (
            "expected_base_sha",
            "payload_sha256",
            "review_preview",
            "comment_update_preview",
            "reply_previews",
        ):
            require(
                preview.get(key) == validated[key], "live review differs from preview; regenerate"
            )
        review = validated["review_preview"]
        update = validated["comment_update_preview"]
        if update:
            comment_id = update["comment_id"]
            kind = update["comment_kind"]
            current = api.get(comment_endpoint(repository, number, kind, comment_id))
            require(
                own_author(current)
                and current.get("id") == comment_id
                and isinstance(current.get("body"), str)
                and hashlib.sha256(current["body"].encode()).hexdigest()
                == update["expected_body_sha256"]
                and (kind != "review" or last_review([current])),
                "existing review body or ownership changed; regenerate the preview",
            )
            body = update["body"]
        else:
            kind = "review"
            body = review["review"]
        check_pr()
        # Persist before sending so a timeout or termination cannot cause a blind retry.
        with pending.open("x", encoding="ascii") as stream:
            stream.write(head)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            response = (
                api.update_body(repository, number, kind, comment_id, body)
                if update
                else api.create_review(repository, number, head, body, review["file_comments"])
            )
        except GitHubHTTPError as error:
            if error.status in {400, 401, 403, 404, 405, 410, 422, 429}:
                pending.unlink()
            raise
        require(
            own_author(response)
            and response.get("body") == body
            and (not update or response.get("id") == comment_id)
            and (kind != "review" or (last_review([response]) or {}).get("commit_id") == head)
            and (update or response.get("commit_id") == head),
            "GitHub response did not confirm publication; reconcile GitHub before retrying",
        )
        published_id = positive_int(response["id"], "published comment ID")
        pending.unlink()
        return {
            **result,
            "status": "updated" if update else "published",
            "comment_id": published_id,
            "comment_kind": kind,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    config_argument(parser)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--preview", required=True, type=Path)
    parser.add_argument(
        "--state-dir",
        required=True,
        type=Path,
        help="persistent private directory shared by all runs of this publisher",
    )
    parser.add_argument("findings", type=Path)
    args = parser.parse_args()
    emit(
        publish(
            read_json(args.findings),
            read_json(args.manifest),
            read_json(args.preview),
            Policy.load(args.config),
            GitHubPublisher(),
            args.state_dir,
        )
    )


if __name__ == "__main__":
    run_cli(main)
