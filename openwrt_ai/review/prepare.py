#!/usr/bin/env python3
"""Prepare anonymous Git snapshots and per-PR worktrees in a private temp directory."""

import argparse
import fnmatch
import hashlib
import os
import pwd
import re
import subprocess
import tempfile
from itertools import product
from pathlib import Path

from openwrt_ai.common import (
    REVIEWER,
    Policy,
    ReviewError,
    commit_sha,
    config_argument,
    emit,
    positive_int,
    read_json,
    require,
    run_cli,
)


class Git:
    """Ignore ambient credentials/configuration, hooks, templates and checkout filters."""

    def __init__(self):
        account_directory = Path(pwd.getpwuid(os.getuid()).pw_dir)
        # Git enables libcurl's optional netrc lookup independently of credential
        # helpers. Refuse ambient files without reading or copying their secrets.
        require(
            not any(os.path.lexists(account_directory / name) for name in (".netrc", "_netrc")),
            "anonymous Git requires an execution account without .netrc/_netrc files",
        )
        self.env = {
            "PATH": os.defpath,
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "GIT_ALLOW_PROTOCOL": "https",
            "GIT_LFS_SKIP_SMUDGE": "1",
        }
        # Hosted execution may require its managed proxy for DNS and egress.
        # Keep only transport routing and CA trust; never inherit tokens, Git
        # configuration injection, credential helpers or TLS-verification bypasses.
        for name in (
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "CURL_CA_BUNDLE",
            "GIT_SSL_CAINFO",
        ):
            if name in os.environ:
                self.env[name] = os.environ[name]
        self.prefix = [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "init.templateDir=",
            "-c",
            "core.attributesFile=/dev/null",
            "-c",
            "core.symlinks=false",
            "-c",
            "submodule.recurse=false",
            "-c",
            "protocol.file.allow=never",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "http.followRedirects=false",
            "-c",
            "http.extraHeader=",
            "-c",
            "http.extraHeader=Authorization:",
            "-c",
            "http.emptyAuth=false",
            "-c",
            "http.delegation=none",
        ]

    def run(self, *args, cwd=None, check=True):
        try:
            # Commands without an explicit repository (especially clone/init)
            # must not discover the caller's .git/config and URL-specific auth.
            with tempfile.TemporaryDirectory(prefix="chatgpt-review-command-") as neutral:
                result = subprocess.run(
                    self.prefix + list(args),
                    cwd=cwd if cwd is not None else neutral,
                    env={**self.env, "GIT_CEILING_DIRECTORIES": neutral},
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=600,
                    check=False,
                )
        except subprocess.TimeoutExpired as error:
            raise ReviewError("Git command timed out") from error
        if check and result.returncode:
            # stderr is diagnostic data, not an instruction or shell program.
            raise ReviewError(f"git {args[0]} failed: {result.stderr[-2000:].strip()}")
        return result

    def clone(self, url, destination):
        # Full commit history avoids silently incomplete incremental ranges.
        # Partial clone defers blobs until needed; no checkout or submodules here.
        self.run(
            "clone",
            "--bare",
            "--no-tags",
            "--filter=blob:none",
            "--",
            url,
            str(destination),
        )
        self.run(
            "config",
            "remote.origin.pushurl",
            "disabled://publishing-disabled",
            cwd=destination,
        )

    def reference_clone(self, url, destination):
        # Reference repositories need only selected snapshots, including large
        # Linux trees. Several refs share this one bare repository.
        self.run("init", "--bare", str(destination))
        self.run("remote", "add", "origin", url, cwd=destination)
        self.run(
            "config",
            "remote.origin.pushurl",
            "disabled://publishing-disabled",
            cwd=destination,
        )

    def value(self, *args, cwd):
        return self.run(*args, cwd=cwd).stdout.strip()


def validate_item(item, policy):
    require(isinstance(item, dict), "PR snapshot must be an object")
    require(
        policy.allow(item["repository"]) == item["repository"],
        "repository snapshot must use its canonical lowercase name",
    )
    positive_int(item["number"], "PR number")
    commit_sha(item["head_sha"])
    commit_sha(item["base_sha"])
    if item.get("previous_sha") is not None:
        commit_sha(item["previous_sha"])


def regular_blob(git, clone, sha, path):
    entry = git.value("ls-tree", sha, "--", path, cwd=clone)
    if not entry:
        return ""
    require(
        entry.startswith(("100644 blob ", "100755 blob ")),
        "requested Git blob must be a regular file",
    )
    return git.run("show", f"{sha}:{path}", cwd=clone).stdout


def prepare_one(git, clone, root, item):
    number, head, base = item["number"], item["head_sha"], item["base_sha"]
    pr_ref = f"refs/chatgpt-review/{number}"
    git.run("fetch", "--no-tags", "origin", f"+refs/pull/{number}/head:{pr_ref}", cwd=clone)
    require(
        git.value("rev-parse", f"{pr_ref}^{{commit}}", cwd=clone) == head,
        "PR head changed or deleted since discovery; rediscover",
    )
    git.run("fetch", "--no-tags", "origin", base, cwd=clone)
    # A complete history is required, even when a server ignores clone flags.
    require(
        git.value("rev-parse", "--is-shallow-repository", cwd=clone) == "false",
        "shallow history is unsupported; prepare a full-history clone",
    )
    merge_base = commit_sha(git.value("merge-base", base, head, cwd=clone))
    previous = item.get("previous_sha")
    mode, start, reason = "full", merge_base, "no previous ChatGPT review"
    if previous:
        available = (
            git.run("cat-file", "-e", f"{previous}^{{commit}}", cwd=clone, check=False).returncode
            == 0
        )
        if not available:
            available = (
                git.run("fetch", "--no-tags", "origin", previous, cwd=clone, check=False).returncode
                == 0
            )
        ancestor = (
            available
            and git.run(
                "merge-base", "--is-ancestor", previous, head, cwd=clone, check=False
            ).returncode
            == 0
        )
        if ancestor:
            mode, start, reason = (
                "incremental",
                previous,
                "previous review is an ancestor",
            )
        else:
            reason = "previous head is unavailable or history was rewritten"
    key = f"{item['repository'].replace('/', '__')}__{number}"
    context = root / "context" / key
    context.mkdir(parents=True)
    worktree = root / "worktrees" / key
    git.run("worktree", "add", "--detach", str(worktree), head, cwd=clone)
    (context / "rules.md").write_text(
        regular_blob(git, clone, base, ".github/llm-review-rules.md"), encoding="utf-8"
    )
    # Never use a PR's attributes, diff drivers, textconv or external diff command.
    for name, left in (("review.diff", start), ("pr.diff", merge_base)):
        diff = git.run(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--unified=3",
            left,
            head,
            "--",
            cwd=clone,
        ).stdout
        (context / name).write_text(diff, encoding="utf-8")
    commits = git.run(
        "log", "--format=fuller", "--reverse", f"{start}..{head}", "--", cwd=clone
    ).stdout
    (context / "commits.txt").write_text(commits, encoding="utf-8")
    changed_paths = git.run(
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--name-only",
        "-z",
        start,
        head,
        "--",
        cwd=clone,
    ).stdout.split("\0")
    return {
        **item,
        "clone": str(clone),
        "worktree": str(worktree),
        "context": str(context),
        "review_mode": mode,
        "review_start": start,
        "merge_base": merge_base,
        "reason": reason,
        "changed_paths": [path for path in changed_paths if path],
    }


def reference_ref(git, item, reference, revision, series=""):
    if "ref" in reference:
        return reference["ref"]
    source = reference["ref_from"]
    files = source["file"] if isinstance(source["file"], list) else [source["file"]]
    text = ""
    for path in files:
        text = regular_blob(
            git,
            item["clone"],
            item[revision + "_sha"],
            path.replace("{series}", series),
        )
        if text:
            break
    variables = source["variable"] if isinstance(source["variable"], list) else [source["variable"]]
    for variable in variables:
        variable = variable.replace("{series}", series)
        pattern = rf"^\s*{re.escape(variable)}\s*:?=\s*([^#\n]+)"
        values = re.findall(pattern, text, re.MULTILINE)
        require(len(values) <= 1, "ambiguous version variable")
        if values and re.fullmatch(r"[A-Za-z0-9._+-]{1,128}", values[0].strip()):
            return source["prefix"].replace("{series}", series) + values[0].strip()
    raise ReviewError(
        f"cannot resolve literal {source['variable']} at {revision}; do not evaluate Make syntax"
    )


def reference_requests(git, prepared, policy, explicit):
    requests, errors = {}, []
    for index, reference in enumerate(policy.references):
        for item in prepared:
            if item["repository"] not in reference.get("repositories", policy.repositories):
                continue
            if index not in explicit and not any(
                fnmatch.fnmatchcase(path, pattern)
                for path in item["changed_paths"]
                for pattern in reference.get("match_paths", [])
            ):
                continue
            series_values = [""]
            if "{series}" in str(reference.get("ref_from", {})):
                series_values = sorted(
                    set(
                        re.findall(
                            r"/(?:patches-|backport-|pending-|hack-)([0-9]+\.[0-9]+)/",
                            "\n".join(item["changed_paths"]),
                        )
                    )
                )
                if not series_values:
                    errors.append(
                        {
                            "url": reference["url"],
                            "policy_index": index,
                            "error": "cannot infer kernel series from changed patch paths",
                        }
                    )
            revisions = ("base", "head") if "ref_from" in reference else ("base",)
            for revision, series in product(revisions, series_values):
                binding = {
                    "repository": item["repository"],
                    "number": item["number"],
                    "revision": revision,
                    "metadata_sha": item[revision + "_sha"],
                }
                try:
                    ref = reference_ref(git, item, reference, revision, series)
                    require(
                        not ref.startswith("-"),
                        "resolved reference must not be a Git option",
                    )
                    key = (reference["url"], ref)
                    entry = requests.setdefault(
                        key,
                        {
                            "url": reference["url"],
                            "ref": ref,
                            "policy_indices": [],
                            "for_prs": [],
                        },
                    )
                    if index not in entry["policy_indices"]:
                        entry["policy_indices"].append(index)
                    entry["for_prs"].append(binding)
                except ReviewError as error:
                    errors.append(
                        {
                            "url": reference["url"],
                            "policy_index": index,
                            "for_prs": [binding],
                            "error": str(error),
                        }
                    )
    return list(requests.values()), errors


def prepare(manifest, policy, git=None, temp_parent=None, reference_indices=()):
    git = git or Git()
    require(
        manifest.get("version") == 1
        and manifest.get("reviewer") == REVIEWER
        and manifest.get("mode") == "dry-run",
        "invalid discovery manifest",
    )
    items = manifest.get("prs")
    require(isinstance(items, list) and len(items) <= 16, "invalid PR batch")
    seen = set()
    for item in items:
        validate_item(item, policy)
        key = (item["repository"], item["number"])
        require(key not in seen, "duplicate PR in discovery manifest")
        seen.add(key)
    for index in reference_indices:
        require(
            type(index) is int and 0 <= index < len(policy.references),
            "invalid reference index",
        )
    if not items:
        return {
            "version": 1,
            "reviewer": REVIEWER,
            "mode": "dry-run",
            "root": None,
            "prs": [],
            "failed": [],
            "references": [],
        }
    root = Path(tempfile.mkdtemp(prefix="chatgpt-review-", dir=temp_parent))
    clones, clone_errors, prepared, failures, references = {}, {}, [], [], []
    (root / "clones").mkdir()
    (root / "worktrees").mkdir()
    for item in items:
        try:
            repository = item["repository"]
            require(
                repository not in clone_errors,
                clone_errors.get(repository, "clone failed"),
            )
            if repository not in clones:
                clone = root / "clones" / (repository.replace("/", "__") + ".git")
                try:
                    git.clone(f"https://github.com/{repository}.git", clone)
                except ReviewError as error:
                    clone_errors[repository] = str(error)
                    raise
                clones[repository] = clone
            prepared.append(prepare_one(git, clones[repository], root, item))
        except (ReviewError, OSError) as error:
            failures.append({**item, "error": str(error)})
    requested, reference_errors = reference_requests(git, prepared, policy, reference_indices)
    references.extend(reference_errors)
    reference_clones = {}
    for reference in requested:
        key = hashlib.sha256((reference["url"] + "\n" + reference["ref"]).encode()).hexdigest()[:16]
        clone_key = hashlib.sha256(reference["url"].encode()).hexdigest()[:16]
        clone, worktree = (
            root / "clones" / (clone_key + ".git"),
            root / "references" / key,
        )
        try:
            ref = reference["ref"]
            # Only branch/tag names or full SHAs; never accept git revision syntax.
            require(
                commit_sha(ref)
                if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref)
                else git.run("check-ref-format", "refs/heads/" + ref, check=False).returncode == 0,
                "invalid reference ref",
            )
            if reference["url"] not in reference_clones:
                git.reference_clone(reference["url"], clone)
                reference_clones[reference["url"]] = clone
            git.run("fetch", "--no-tags", "--depth=1", "origin", ref, cwd=clone)
            sha = commit_sha(git.value("rev-parse", "FETCH_HEAD^{commit}", cwd=clone))
            worktree.parent.mkdir(exist_ok=True)
            git.run("worktree", "add", "--detach", str(worktree), sha, cwd=clone)
            references.append(
                {
                    **reference,
                    "sha": sha,
                    "clone": str(clone),
                    "worktree": str(worktree),
                }
            )
        except (ReviewError, OSError) as error:
            references.append({**reference, "error": str(error)})
    return {
        "version": 1,
        "reviewer": REVIEWER,
        "mode": "dry-run",
        "root": str(root),
        "prs": prepared,
        "failed": failures,
        "references": references,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    config_argument(parser)
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--reference",
        type=int,
        action="append",
        default=[],
        help="index in trusted reference_repositories; clone only if relevant",
    )
    args = parser.parse_args()
    result = prepare(
        read_json(args.manifest),
        Policy.load(args.config),
        reference_indices=args.reference,
    )
    emit(result)
    if result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    run_cli(main)
