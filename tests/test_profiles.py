"""Repository profiles must not mix firmware conventions into other projects."""

import json
import unittest
from pathlib import Path
from unittest import mock

import test_prepare
from test_review import BASE, BOT, BOT_ID, HEAD, NOW, REPOSITORY, FakeAPI, api_routes, findings

from openwrt_ai.common import Policy, ReviewError
from openwrt_ai.review.discover import discover
from openwrt_ai.review.prepare import reference_requests
from openwrt_ai.review.validate import validate


class ProfileTests(unittest.TestCase):
    def test_supplied_profiles_load_with_triage_disabled(self):
        root = Path(__file__).resolve().parents[1]
        for path in (root / "configs").glob("*.json"):
            with self.subTest(profile=path.name):
                self.assertFalse(Policy.load(path).triage["enabled"])

    def test_procd_pr_can_be_discovered_and_validated(self):
        target = "openwrt/procd"
        data = json.loads(json.dumps(api_routes()).replace(REPOSITORY, target))
        policy = Policy.load(Path(__file__).resolve().parents[1] / "configs/procd.json")
        api = FakeAPI(data)
        manifest = discover(api, policy, repository=target, number=1)
        proposal = json.loads(json.dumps(findings()).replace(REPOSITORY, target))
        result = validate(
            proposal,
            manifest,
            policy,
            api,
            {"login": BOT, "id": BOT_ID, "observed_at": NOW.isoformat()},
            now=NOW,
        )
        self.assertFalse(result["publishing_enabled"])
        self.assertEqual(manifest["prs"][0]["repository"], target)

    def test_procd_preparation_uses_its_source_and_base_rules(self):
        fixture = test_prepare.PreparationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.policy = Policy.load(Path(__file__).resolve().parents[1] / "configs/procd.json")
        item = {**fixture.item(), "repository": "openwrt/procd"}
        result = fixture.prepare(items=[item])
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["references"], [])
        self.assertEqual(fixture.runner.cloned, ["https://github.com/openwrt/procd.git"])
        prepared = result["prs"][0]
        self.assertEqual(Path(prepared["context"], "rules.md").read_text(), "Trusted base rules\n")
        self.assertEqual(Path(prepared["worktree"], "file.c").read_text(), "first\nnew\nlast\n")

    def test_reference_scope_applies_even_to_explicit_selection(self):
        policy = Policy(
            {
                "version": 1,
                "repositories": ["openwrt/openwrt", "openwrt/procd"],
                "reference_repositories": [
                    {
                        "url": "https://example.org/public.git",
                        "ref": "v1.2",
                        "repositories": ["OpenWrt/OpenWrt"],
                        "match_paths": ["**"],
                    }
                ],
            }
        )
        item = {
            "repository": "openwrt/procd",
            "number": 1,
            "base_sha": BASE,
            "head_sha": HEAD,
            "changed_paths": ["package/demo/patches/fix.patch"],
        }
        git = mock.Mock()
        self.assertEqual(reference_requests(git, [item], policy, [0]), ([], []))
        self.assertEqual(git.mock_calls, [])
        requests, failures = reference_requests(
            git, [{**item, "repository": "openwrt/openwrt"}], policy, []
        )
        self.assertEqual(failures, [])
        self.assertEqual(requests[0]["ref"], "v1.2")

    def test_reference_scope_cannot_escape_repository_allowlist(self):
        with self.assertRaises(ReviewError):
            Policy(
                {
                    "version": 1,
                    "repositories": ["openwrt/procd"],
                    "reference_repositories": [
                        {
                            "url": "https://example.org/public.git",
                            "ref": "v1",
                            "repositories": ["foreign/repo"],
                        }
                    ],
                }
            )

    def test_invalid_triage_policy_is_rejected(self):
        for config in (
            {"enabled": "true", "max_issues": 8, "allowed_labels": []},
            {"enabled": True, "max_issues": 51, "allowed_labels": []},
            {"enabled": True, "max_issues": 8, "allowed_labels": ["bug\n"]},
        ):
            with self.subTest(config=config), self.assertRaises(ReviewError):
                Policy({"version": 1, "repositories": ["openwrt/procd"], "issue_triage": config})
