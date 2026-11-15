#!/usr/bin/env python3
"""Collect upstream context and related sites for changed patches; never execute PR code."""

import argparse
import json
import re
from pathlib import Path

from openwrt_ai.common import (
    ReviewError,
    commit_sha,
    emit,
    positive_int,
    read_json,
    require,
    run_cli,
    safe_source_path,
)
from openwrt_ai.review.prepare import Git, regular_blob

MAX_FILES = 20
MAX_SYMBOLS = 24
MAX_MATCHES = 500
MAX_SOURCE_CHARS = 1000000
IGNORE = {
    "if",
    "else",
    "for",
    "while",
    "switch",
    "return",
    "sizeof",
    "defined",
    "likely",
    "unlikely",
}


def patch_targets(text):
    paths, headers, symbols, warnings = [], [], [], []
    for line in text.splitlines():
        if line.startswith(("--- ", "+++ ")):
            path = line[4:].split("\t", 1)[0]
            if path == "/dev/null":
                continue
            if path.startswith(("a/", "b/")):
                path = path[2:]
            try:
                safe_source_path(path)
                if path.startswith('"'):
                    warnings.append("quoted patch path needs manual inspection")
                    continue
                if path not in paths:
                    paths.append(path)
            except ReviewError:
                warnings.append("invalid patch path")
        elif line.startswith("@@"):
            headers.append(line)
            symbols.extend(re.findall(r"\b([A-Za-z_]\w{3,})\s*\(", line))
        elif line.startswith(("+", "-")):
            symbols.extend(re.findall(r"\b([A-Za-z_]\w{3,})\s*\(", line[1:]))
    symbols = list(dict.fromkeys(symbol for symbol in symbols if symbol not in IGNORE))
    return paths, headers, symbols, warnings


def inspect_patch(git, item, reference, path, output):
    safe_source_path(path)
    patch = regular_blob(git, item["clone"], commit_sha(item["head_sha"]), path)
    # OpenWrt patch data is read through Git blobs, not followed through symlinks.
    paths, headers, symbols, warnings = patch_targets(patch)
    report = {
        "patch": path,
        "upstream_paths": paths,
        "hunk_headers": headers,
        "search_symbols": symbols[:MAX_SYMBOLS],
        "warnings": warnings,
        "files": [],
        "matches": [],
        "symbol_search_performed": bool(symbols),
        "search_truncated": False,
        "files_truncated": len(paths) > MAX_FILES,
        "symbols_truncated": len(symbols) > MAX_SYMBOLS,
        "missing_site_candidates_are_findings": False,
    }
    sha = commit_sha(reference["sha"])
    clone = reference["clone"]
    for index, source_path in enumerate(paths[:MAX_FILES]):
        try:
            content = regular_blob(git, clone, sha, source_path)
            entry = git.value("ls-tree", sha, "--", source_path, cwd=clone)
            if not entry:
                report["files"].append({"path": source_path, "missing": True})
                continue
            if len(content) > MAX_SOURCE_CHARS:
                report["files"].append({"path": source_path, "too_large": True})
                continue
            destination = output / f"source-{index}.txt"
            destination.write_text(content, encoding="utf-8")
            report["files"].append(
                {
                    "path": source_path,
                    "content_file": str(destination),
                    "lines": len(content.splitlines()),
                }
            )
        except ReviewError as error:
            report["files"].append({"path": source_path, "error": str(error)})
    if symbols:
        arguments = ["grep", "-n", "-z", "-I", "-F"]
        for symbol in symbols[:MAX_SYMBOLS]:
            arguments.extend(["-e", symbol])
        search = git.run(*arguments, sha, "--", cwd=clone, check=False)
        require(search.returncode in (0, 1), "upstream symbol search failed")
        rows = list(re.finditer(r"([^\0]+)\0(\d+)\0([^\n]*)(?:\n|$)", search.stdout[:2000000]))
        report["search_truncated"] = len(rows) > MAX_MATCHES or len(search.stdout) > 2000000
        for row in rows[:MAX_MATCHES]:
            filename = row[1].removeprefix(sha + ":")
            report["matches"].append(
                {
                    "path": filename,
                    "line": int(row[2]),
                    "text": row[3][:1000],
                    "text_truncated": len(row[3]) > 1000,
                    "file_changed_by_patch": filename in paths,
                }
            )
    patch_file = output / "input.patch"
    patch_file.write_text(patch, encoding="utf-8")
    check = git.run(
        "apply",
        "--check",
        "--whitespace=nowarn",
        "--",
        str(patch_file),
        cwd=reference["worktree"],
        check=False,
    )
    report["apply_check"] = {
        "exit_code": check.returncode,
        "stderr": check.stderr[-4000:],
        "basis": "raw reference only; OpenWrt prerequisite patches are not applied",
    }
    return report


def inspect(prepared, repository, number, reference_index, git=None):
    git = git or Git()
    positive_int(number, "PR number")
    items = [
        item
        for item in prepared["prs"]
        if item["repository"] == repository and item["number"] == number
    ]
    require(len(items) == 1, "PR is absent or ambiguous in prepared manifest")
    require(
        type(reference_index) is int and 0 <= reference_index < len(prepared["references"]),
        "invalid prepared-reference index",
    )
    item, reference = items[0], prepared["references"][reference_index]
    require("error" not in reference and "sha" in reference, "reference preparation failed")
    require(
        any(
            binding["repository"] == repository and binding["number"] == number
            for binding in reference["for_prs"]
        ),
        "reference is not assigned to this PR",
    )
    output = Path(item["context"]) / ("upstream-" + str(reference_index))
    output.mkdir(exist_ok=False)
    patches = [path for path in item["changed_paths"] if path.endswith(".patch")]
    reports = []
    for index, path in enumerate(patches[:MAX_FILES]):
        directory = output / str(index)
        directory.mkdir()
        reports.append(inspect_patch(git, item, reference, path, directory))
    result = {
        "repository": repository,
        "number": number,
        "head_sha": item["head_sha"],
        "reference_url": reference["url"],
        "reference_ref": reference["ref"],
        "reference_sha": reference["sha"],
        "reference_bindings": reference["for_prs"],
        "patches": reports,
        "patches_truncated": len(patches) > MAX_FILES,
        "scope": "static upstream context; no semantic completeness claim",
    }
    (output / "report.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prepared", type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument(
        "--reference",
        required=True,
        type=int,
        help="index in prepared.json references, not the policy",
    )
    args = parser.parse_args()
    emit(inspect(read_json(args.prepared), args.repository, args.pr, args.reference))


if __name__ == "__main__":
    run_cli(main)
