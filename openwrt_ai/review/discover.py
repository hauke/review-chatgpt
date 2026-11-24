#!/usr/bin/env python3
"""Discover public PRs needing ChatGPT review; never write to GitHub."""

import argparse
import heapq
from datetime import UTC, datetime

from openwrt_ai.common import (
    REVIEWER,
    GitHubError,
    Policy,
    ReviewError,
    config_argument,
    emit,
    positive_int,
    pr_created_at,
    require,
    run_cli,
    snapshot,
)
from openwrt_ai.connector import client_argument, github_client
from openwrt_ai.review.history import read_history


def discover(api, policy, maximum=None, repository=None, number=None):
    maximum = policy.max_prs if maximum is None else maximum
    require(type(maximum) is int and 1 <= maximum <= 16, "max-prs must be between 1 and 16")
    require(
        (repository is None) == (number is None),
        "--repository and --pr must be used together",
    )
    if repository is not None:
        repository = policy.allow(repository)
        positive_int(number, "PR number")

    def pending(repo):
        try:
            api.public_repo(repo)
            if number is not None:
                yield repo, api.get(f"/repos/{repo}/pulls/{number}")
            else:
                for pr in api.pages(
                    f"/repos/{repo}/pulls",
                    {
                        "state": "open",
                        "sort": "created",
                        "direction": "desc",
                    },
                ):
                    yield repo, pr
        except ReviewError as error:
            raise ReviewError(f"{repo}: {error}") from error

    if repository is None:
        # Merge newest-first streams lazily across repositories.
        stream = heapq.merge(
            *(pending(repo) for repo in policy.repositories),
            key=lambda pair: pr_created_at(pair[1]),
            reverse=True,
        )
    else:
        stream = pending(repository)
    candidates, skipped, failures = [], [], []
    scan_complete = False
    try:
        for repository, pr in stream:
            try:
                pr_number = positive_int(pr["number"], "PR number")
                require(
                    number is None or pr_number == number,
                    "GitHub returned a different PR",
                )
                _, _, _, claude, previous = read_history(api, repository, pr_number)
                item = snapshot(repository, pr, previous)
                if claude:
                    skipped.append({**item, "reason": "Claude or legacy AI already commented"})
                elif item["head_sha"] == item["previous_sha"]:
                    skipped.append({**item, "reason": "already reviewed by ChatGPT"})
                else:
                    candidates.append(item)
                    if len(candidates) >= maximum:
                        # Specific-PR mode has no other pending items to scan.
                        scan_complete = number is not None
                        break
            except GitHubError as error:
                failures.append(
                    {
                        "repository": repository,
                        "number": pr.get("number"),
                        "error": str(error),
                    }
                )
                break
            except (ReviewError, KeyError, ValueError, TypeError) as error:
                # Keep successful discoveries reviewable, but make failures
                # explicit and return a failing exit status from the CLI.
                failures.append(
                    {
                        "repository": repository,
                        "number": pr.get("number"),
                        "error": str(error),
                    }
                )
        else:
            scan_complete = number is not None or getattr(api, "listing_complete", True)
    except ReviewError as error:
        failures.append(
            {
                "repository": None,
                "number": None,
                "scope": "repository listing",
                "error": str(error),
            }
        )
    return {
        "version": 1,
        "reviewer": REVIEWER,
        "mode": "dry-run",
        "generated_at": datetime.now(UTC).isoformat(),
        "prs": candidates,
        "scan_complete": scan_complete,
        "deferred": 0 if scan_complete and not failures else None,
        "skipped": skipped,
        "failed": failures,
        "github_read_source": api.source,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    config_argument(parser)
    client_argument(parser)
    parser.add_argument("--max-prs", type=int)
    parser.add_argument("--repository", help="allowlisted repository for a specific-PR dry run")
    parser.add_argument("--pr", type=int, help="review this PR without scanning open PRs")
    args = parser.parse_args()
    policy = Policy.load(args.config)
    result = discover(github_client(args, policy), policy, args.max_prs, args.repository, args.pr)
    emit(result)
    if result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    run_cli(main)
