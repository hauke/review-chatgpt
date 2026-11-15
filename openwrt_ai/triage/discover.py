"""Read public issue candidates for future triage; disabled by default, no GitHub writes."""

import argparse
import heapq
from datetime import UTC, datetime

from openwrt_ai.common import (
    Policy,
    ReviewError,
    config_argument,
    emit,
    positive_int,
    require,
    run_cli,
)
from openwrt_ai.connector import client_argument, github_client


def discover(api, policy, maximum=None, repository=None):
    maximum = policy.triage["max_issues"] if maximum is None else maximum
    require(type(maximum) is int and 1 <= maximum <= 50, "max-issues must be between 1 and 50")
    repositories = (policy.allow(repository),) if repository is not None else policy.repositories
    result = {
        "version": 1,
        "task": "issue-triage",
        "mode": "dry-run",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "disabled",
        "triage_performed": False,
        "publishing_enabled": False,
        "issues": [],
        "failed": [],
        "scan_complete": False,
        "deferred": None,
    }
    if not policy.triage["enabled"]:
        return result
    require(api is not None, "issue discovery requires authenticated GitHub reads")
    result.update(status="discovered", github_read_source=api.source)

    def pending(repo):
        api.public_repo(repo)
        for issue in api.pages(
            f"/repos/{repo}/issues",
            {"state": "open", "sort": "updated", "direction": "asc"},
        ):
            require(isinstance(issue, dict), "invalid issue response")
            # GitHub's issues endpoint also returns pull requests.
            if "pull_request" not in issue and issue.get("state") == "open":
                yield repo, issue

    stream = heapq.merge(
        *(pending(repo) for repo in repositories),
        key=lambda pair: (pair[1].get("updated_at", ""), pair[0]),
    )
    try:
        for repo, issue in stream:
            number = positive_int(issue["number"], "issue number")
            title, body = issue.get("title"), issue.get("body") or ""
            require(isinstance(title, str) and isinstance(body, str), "invalid issue text")
            labels = issue.get("labels", [])
            require(
                isinstance(labels, list)
                and all(
                    isinstance(label, dict) and isinstance(label.get("name"), str)
                    for label in labels
                ),
                "invalid issue labels",
            )
            result["issues"].append(
                {
                    "repository": repo,
                    "number": number,
                    "url": f"https://github.com/{repo}/issues/{number}",
                    "title": title[:1000],
                    "body": body[:20000],
                    "text_truncated": len(title) > 1000 or len(body) > 20000,
                    "updated_at": issue.get("updated_at"),
                    "labels": [label["name"] for label in labels],
                    "comments_included": False,
                }
            )
            if len(result["issues"]) >= maximum:
                break
        else:
            result["scan_complete"] = getattr(api, "listing_complete", True)
            if result["scan_complete"]:
                result["deferred"] = 0
    except (ReviewError, KeyError, ValueError, TypeError) as error:
        result["status"] = "failed"
        result["failed"].append({"scope": "issue discovery", "error": str(error)})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    config_argument(parser)
    client_argument(parser)
    parser.add_argument("--max-issues", type=int)
    parser.add_argument("--repository", help="restrict discovery to one allowlisted repository")
    args = parser.parse_args()
    policy = Policy.load(args.config)
    # Disabled profiles do not require credentials, snapshots or network access.
    api = github_client(args, policy) if policy.triage["enabled"] else None
    result = discover(api, policy, args.max_issues, args.repository)
    emit(result)
    if result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    run_cli(main)
