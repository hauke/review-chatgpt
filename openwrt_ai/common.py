"""Trusted policy and authenticated, read-only GitHub access. Python 3.11+, stdlib only."""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BOT = "openwrt-ai"
BOT_ID = 276137552
REVIEWER = "chatgpt-work-v1"
TOKEN_ENV = "CHATGPT_REVIEW_GITHUB_TOKEN"
SHA = re.compile(r"[0-9a-f]{40}\Z")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+\Z")


class ReviewError(Exception):
    """An expected failure which must never be interpreted as a clean review."""


class GitHubError(ReviewError):
    """A transport/API failure: stop discovery instead of repeating failed reads."""


def require(condition, message):
    if not condition:
        raise ReviewError(message)


def repo_name(value):
    require(isinstance(value, str) and REPO.fullmatch(value), "invalid repository name")
    require(value.split("/")[1] not in {".", ".."}, "invalid repository name")
    return value.lower()


def positive_int(value, label):
    require(type(value) is int and value > 0, f"invalid {label}")
    return value


def commit_sha(value):
    require(isinstance(value, str) and SHA.fullmatch(value), "invalid commit SHA")
    return value


def read_json(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def emit(value):
    json.dump(value, sys.stdout, indent=2, ensure_ascii=True)
    sys.stdout.write("\n")


def run_cli(main):
    try:
        main()
    except (ReviewError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"chatgpt-review: {error}", file=sys.stderr)
        sys.exit(1)


def config_argument(parser):
    parser.add_argument("--config", required=True, type=Path, help="trusted policy JSON")


class Policy:
    def __init__(self, data):
        require(isinstance(data, dict), "policy must be an object")
        allowed = {
            "version",
            "repositories",
            "max_prs",
            "max_comments",
            "max_comment_chars",
            "max_commit_checks",
            "reference_repositories",
            "issue_triage",
        }
        require(not data.keys() - allowed, "unknown policy key")
        require(data.get("version") == 1, "unsupported policy version")
        repos = data.get("repositories")
        require(isinstance(repos, list) and repos, "repository allowlist is required")
        self.repositories = tuple(dict.fromkeys(repo_name(repo) for repo in repos))
        self.triage = data.get(
            "issue_triage", {"enabled": False, "max_issues": 8, "allowed_labels": []}
        )
        require(
            isinstance(self.triage, dict)
            and set(self.triage) == {"enabled", "max_issues", "allowed_labels"},
            "issue_triage requires enabled, max_issues and allowed_labels",
        )
        require(
            type(self.triage["enabled"]) is bool
            and type(self.triage["max_issues"]) is int
            and 1 <= self.triage["max_issues"] <= 50,
            "invalid issue triage limits",
        )
        require(
            isinstance(self.triage["allowed_labels"], list)
            and all(
                isinstance(label, str)
                and label.strip()
                and len(label) <= 100
                and not any(ord(char) < 32 for char in label)
                for label in self.triage["allowed_labels"]
            ),
            "invalid triage label allowlist",
        )
        self.max_prs = data.get("max_prs", 8)
        require(
            type(self.max_prs) is int and 1 <= self.max_prs <= 16,
            "max_prs must be between 1 and 16",
        )
        self.max_comments = data.get("max_comments", 20)
        self.max_comment_chars = data.get("max_comment_chars", 3000)
        self.max_commit_checks = data.get("max_commit_checks", 20)
        for name in ("max_comments", "max_comment_chars", "max_commit_checks"):
            positive_int(getattr(self, name), name)
        require(
            self.max_comments <= 50
            and self.max_comment_chars <= 10000
            and self.max_commit_checks <= 50,
            "policy limits exceed hard limits",
        )
        self.references = data.get("reference_repositories", [])
        require(isinstance(self.references, list), "reference_repositories must be a list")
        for reference in self.references:
            require(
                isinstance(reference, dict)
                and "url" in reference
                and not reference.keys() - {"url", "ref", "ref_from", "match_paths", "repositories"}
                and (("ref" in reference) != ("ref_from" in reference)),
                "reference requires url and exactly one of ref or ref_from",
            )
            parsed = urllib.parse.urlsplit(reference["url"])
            require(
                parsed.scheme == "https"
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and parsed.port in (None, 443),
                "reference must use public HTTPS",
            )
            if "ref" in reference:
                require(
                    isinstance(reference["ref"], str)
                    and reference["ref"]
                    and not reference["ref"].startswith("-")
                    and not any(ord(char) < 33 for char in reference["ref"]),
                    "invalid reference ref",
                )
            else:
                source = reference["ref_from"]
                require(
                    isinstance(source, dict) and set(source) == {"file", "variable", "prefix"},
                    "ref_from requires file, variable and prefix",
                )
                files = source["file"] if isinstance(source["file"], list) else [source["file"]]
                require(files, "version metadata files must not be empty")
                for path in files:
                    safe_source_path(
                        path.replace("{series}", "6.1") if isinstance(path, str) else path
                    )
                variables = (
                    source["variable"]
                    if isinstance(source["variable"], list)
                    else [source["variable"]]
                )
                require(
                    variables
                    and all(
                        isinstance(variable, str)
                        and re.fullmatch(
                            r"[A-Za-z_][A-Za-z0-9_.-]*",
                            variable.replace("{series}", "6.1"),
                        )
                        for variable in variables
                    ),
                    "invalid version variable",
                )
                require(
                    isinstance(source["prefix"], str)
                    and re.fullmatch(
                        r"[A-Za-z0-9._/-]*", source["prefix"].replace("{series}", "6.1")
                    ),
                    "invalid ref prefix",
                )
            patterns = reference.get("match_paths", [])
            scope = reference.get("repositories", list(self.repositories))
            require(
                isinstance(scope, list)
                and scope
                and all(repo_name(repo) in self.repositories for repo in scope),
                "reference repository scope must be in the policy allowlist",
            )
            if "repositories" in reference:
                reference["repositories"] = list(dict.fromkeys(repo_name(repo) for repo in scope))
            require(
                isinstance(patterns, list)
                and all(
                    isinstance(pattern, str)
                    and pattern
                    and not pattern.startswith("/")
                    and ".." not in pattern.split("/")
                    and not any(ord(char) < 32 for char in pattern)
                    for pattern in patterns
                ),
                "match_paths must contain relative path globs",
            )

    @classmethod
    def load(cls, path):
        return cls(read_json(path))

    def allow(self, repository):
        repository = repo_name(repository)
        require(repository in self.repositories, "repository is not allowlisted")
        return repository


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ReviewError("GitHub API redirected; update the trusted allowlist explicitly")


class GitHub:
    """GET only, with a verified bot user token; no anonymous/account fallback."""

    source = "authenticated-rest"

    def __init__(self, token=None):
        token = os.environ.get(TOKEN_ENV) if token is None else token
        require(
            isinstance(token, str) and token and all(33 <= ord(c) <= 126 for c in token),
            f"set {TOKEN_ENV} through protected runtime secrets to an openwrt-ai read token",
        )
        self._token = token
        self._identity = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler(), NoRedirect())

    def get(self, path):
        require(
            (path == "/user" or path.startswith("/repos/"))
            and not any(char in path for char in "\r\n#"),
            "invalid GitHub API path",
        )
        if self._identity is None:
            identity = self._request("/user")
            require(
                isinstance(identity, dict)
                and identity.get("login") == BOT
                and identity.get("id") == BOT_ID,
                "GitHub token must authenticate as openwrt-ai; refusing repository reads",
            )
            self._identity = {"login": BOT, "id": BOT_ID}
        if path == "/user":
            return dict(self._identity)
        return self._request(path)

    def _request(self, path):
        request = urllib.request.Request(
            "https://api.github.com" + path,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": REVIEWER,
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": f"Bearer {self._token}",
            },
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            reset = error.headers.get("X-RateLimit-Reset", "unknown")
            error.close()
            raise GitHubError(
                f"GitHub GET {path}: HTTP {error.code}; rate limit reset (Unix seconds): {reset}"
            ) from error
        except urllib.error.URLError as error:
            reason = str(error.reason).replace(self._token, "[REDACTED]")
            raise GitHubError(f"GitHub GET failed: {reason}") from error

    def pages(self, path, parameters=None):
        require("?" not in path, "pagination path must not include query parameters")
        page = 1
        while True:
            query = urllib.parse.urlencode({**(parameters or {}), "per_page": 100, "page": page})
            data = self.get(f"{path}?{query}")
            require(isinstance(data, list), "GitHub pagination response is not a list")
            yield from data
            if len(data) < 100:
                break
            page += 1

    def public_repo(self, repository):
        repository = repo_name(repository)
        data = self.get(f"/repos/{repository}")
        require(
            data.get("private") is False and data.get("visibility", "public") == "public",
            "repository is not public",
        )
        require(repo_name(data["full_name"]) == repository, "repository identity changed")
        return data


def marker(sha):
    return f"<!-- {REVIEWER}:complete:{commit_sha(sha)} -->"


def safe_source_path(path):
    require(
        isinstance(path, str)
        and path
        and not path.startswith("/")
        and not any(part in ("", ".", "..", ".git") for part in path.split("/"))
        and "\\" not in path
        and not any(ord(char) < 32 for char in path),
        "invalid upstream source path",
    )
    return path


def completion_sha(body):
    matches = re.findall(
        r"^<!-- chatgpt-work-v1:complete:([0-9a-f]{40}) -->\r?$",
        body or "",
        re.MULTILINE,
    )
    return matches[-1] if matches else None


def last_review(reviews):
    """Only submitted COMMENT reviews with this reviewer's exact completion marker."""
    matches = []
    for review in reviews:
        body = review.get("body") or ""
        sha = completion_sha(body)
        if (
            review.get("user", {}).get("login") == BOT
            and review.get("user", {}).get("id") == BOT_ID
            and review.get("state") == "COMMENTED"
            and review.get("submitted_at")
            and isinstance(sha, str)
            and SHA.fullmatch(sha)
            and (sha == review.get("commit_id") or f"Reviewed commit: `{sha}`" in body)
        ):
            # Editing a review body cannot change GitHub's original commit_id.
            # The appended explicit checkpoint records the newly reviewed head.
            matches.append({**review, "commit_id": sha})
    return max(
        matches,
        key=lambda review: (
            review.get("updated_at") or review["submitted_at"],
            review["id"],
        ),
        default=None,
    )


def snapshot(repository, pr, previous=None):
    number = positive_int(pr["number"], "PR number")
    require(pr["state"] == "open" and not pr.get("merged_at"), "PR is closed or merged")
    require(
        repo_name(pr["base"]["repo"]["full_name"]) == repository,
        "unexpected PR base repository",
    )
    require(pr["base"]["repo"].get("private") is False, "PR base is not public")
    head_repo = pr["head"].get("repo")
    require(
        head_repo is not None and head_repo.get("private") is False,
        "PR head repository is deleted or not public",
    )
    return {
        "repository": repository,
        "number": number,
        "head_sha": commit_sha(pr["head"]["sha"]),
        "base_sha": commit_sha(pr["base"]["sha"]),
        "base_ref": pr["base"]["ref"],
        "head_repository": repo_name(head_repo["full_name"]),
        "previous_sha": commit_sha(previous["commit_id"]) if previous else None,
        "previous_review_id": previous["id"]
        if previous and previous.get("comment_kind", "review") == "review"
        else None,
        "previous_comment_id": previous["id"] if previous else None,
        "previous_comment_kind": previous.get("comment_kind", "review") if previous else None,
    }
