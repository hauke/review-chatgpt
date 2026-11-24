"""Offline connector-observation transport and freshness regression tests."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from test_review import (
    BASE,
    BOT,
    BOT_ID,
    HEAD,
    REPOSITORY,
    api_routes,
    findings,
    policy,
    pr,
    review,
    validator,
)

from openwrt_ai.common import ReviewError
from openwrt_ai.connector import ConnectorSnapshot
from openwrt_ai.review.discover import discover


class ConnectorTests(unittest.TestCase):
    def data(self):
        now = datetime.now(UTC)
        return {
            "version": 1,
            "source": "connector-snapshot",
            "observed_at": now.isoformat(),
            "identity": {"login": BOT, "id": BOT_ID},
            "resources": {
                path.partition("?")[0]: {"complete": True, "data": value}
                for path, value in api_routes().items()
            },
        }

    def client(self, data):
        return ConnectorSnapshot(data, policy())

    def test_discovery_uses_connector_without_any_token_or_network(self):
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch(
                "urllib.request.build_opener",
                side_effect=AssertionError("network forbidden"),
            ),
        ):
            result = discover(self.client(self.data()), policy(), repository=REPOSITORY, number=1)
        self.assertEqual(result["prs"][0]["head_sha"], HEAD)
        self.assertEqual(result["github_read_source"], "connector-snapshot")

    def test_incomplete_candidate_listing_does_not_claim_full_scan(self):
        result = discover(self.client(self.data()), policy())
        self.assertFalse(result["scan_complete"])
        self.assertIsNone(result["deferred"])

    def test_candidate_listing_uses_opening_time_and_skips_claude(self):
        data = self.data()
        resources = data["resources"]
        root = f"/repos/{REPOSITORY}"
        resources[f"{root}/pulls"]["data"].extend(
            [
                {
                    **pr(2),
                    "created_at": "2026-11-13T12:00:00Z",
                    "updated_at": "2026-11-13T12:00:00Z",
                },
                {**pr(3), "created_at": "2026-11-13T13:00:00Z"},
            ]
        )
        for path in (
            f"{root}/pulls/2/reviews",
            f"{root}/pulls/2/comments",
            f"{root}/issues/2/comments",
        ):
            resources[path] = {"complete": True, "data": []}
        resources[f"{root}/pulls/3/reviews"] = {
            "complete": True,
            "data": [review(user={"login": "claude[bot]"})],
        }

        result = discover(self.client(data), policy(), maximum=1)
        self.assertEqual([item["number"] for item in result["prs"]], [2])
        self.assertEqual([item["number"] for item in result["skipped"]], [3])
        self.assertEqual(result["failed"], [])

    def test_old_capture_requires_creation_time_only_for_batch(self):
        data = self.data()
        root = f"/repos/{REPOSITORY}"
        del data["resources"][f"{root}/pulls"]["data"][0]["created_at"]
        del data["resources"][f"{root}/pulls/1"]["data"]["created_at"]

        client = self.client(data)
        batch = discover(client, policy())
        self.assertEqual(batch["prs"], [])
        self.assertIn("created_at", batch["failed"][0]["error"])
        specific = discover(client, policy(), repository=REPOSITORY, number=1)
        self.assertEqual([item["number"] for item in specific["prs"]], [1])

    def test_missing_and_incomplete_resources_fail_closed(self):
        for remove in (True, False):
            data = self.data()
            path = f"/repos/{REPOSITORY}/pulls/1/reviews"
            if remove:
                del data["resources"][path]
            else:
                data["resources"][path]["complete"] = False
            with self.assertRaises(ReviewError):
                list(self.client(data).pages(path))

    def test_wrong_identity_stale_future_and_naive_observations(self):
        for changes in (
            {"identity": {"login": "hauke", "id": 78494}},
            {"observed_at": (datetime.now(UTC) - timedelta(minutes=6)).isoformat()},
            {"observed_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat()},
            {"observed_at": "2026-11-15T12:00:00"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ReviewError):
                self.client({**self.data(), **changes})

    def test_resources_cannot_target_other_repositories_or_hosts(self):
        for path in ("https://attacker.invalid", "/repos/attacker/repo", "/user"):
            data = self.data()
            data["resources"][path] = {"complete": True, "data": {}}
            with self.assertRaises(ReviewError):
                self.client(data)

    def test_snapshot_expires_during_reads(self):
        data = self.data()
        now = datetime.fromisoformat(data["observed_at"])
        clock = mock.Mock(return_value=now)
        api = ConnectorSnapshot(data, policy(), clock=clock)
        clock.return_value = now + timedelta(minutes=6)
        with self.assertRaisesRegex(ReviewError, "stale"):
            api.public_repo(REPOSITORY)

    def test_validation_requires_new_capture_and_labels_snapshot_evidence(self):
        old_data = self.data()
        manifest = discover(self.client(old_data), policy(), repository=REPOSITORY, number=1)
        identity = {**old_data["identity"], "observed_at": old_data["observed_at"]}
        with self.assertRaisesRegex(ReviewError, "after discovery"):
            validator.validate(findings(), manifest, policy(), self.client(old_data), identity)
        fresh = self.data()
        identity["observed_at"] = fresh["observed_at"]
        result = validator.validate(findings(), manifest, policy(), self.client(fresh), identity)
        self.assertFalse(result["publishing_enabled"])
        self.assertFalse(result["live_revalidation"])
        self.assertEqual(result["github_read_source"], "connector-snapshot")

    def test_both_cli_entrypoints_work_with_connector_and_no_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def save(name, value):
                path = root / name
                path.write_text(json.dumps(value))
                return str(path)

            config = save("policy.json", {"version": 1, "repositories": [REPOSITORY]})
            discovery_snapshot = save("discovery-github.json", self.data())
            environment = {"PATH": os.defpath}
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-E",
                    "-s",
                    "-m",
                    "openwrt_ai.review.discover",
                    "--config",
                    config,
                    "--repository",
                    REPOSITORY,
                    "--pr",
                    "1",
                    "--github-snapshot",
                    discovery_snapshot,
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=True,
            )
            manifest = save("discovery.json", json.loads(result.stdout))
            fresh = self.data()
            validation_snapshot = save("validation-github.json", fresh)
            identity = save(
                "identity.json",
                {**fresh["identity"], "observed_at": fresh["observed_at"]},
            )
            findings_path = save("findings.json", findings())
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-E",
                    "-s",
                    "-m",
                    "openwrt_ai.review.validate",
                    "--config",
                    config,
                    "--manifest",
                    manifest,
                    "--identity",
                    identity,
                    "--github-snapshot",
                    validation_snapshot,
                    findings_path,
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=True,
            )
            preview = json.loads(result.stdout)
            self.assertFalse(preview["publishing_enabled"])
            self.assertFalse(preview["live_revalidation"])
            self.assertEqual(preview["github_read_source"], "connector-snapshot")

    def test_refreshed_connector_head_change_invalidates_findings(self):
        manifest = discover(self.client(self.data()), policy(), repository=REPOSITORY, number=1)
        fresh = self.data()
        fresh["resources"][f"/repos/{REPOSITORY}/pulls/1"]["data"]["head"]["sha"] = BASE
        identity = {**fresh["identity"], "observed_at": fresh["observed_at"]}
        with self.assertRaisesRegex(ReviewError, "changed"):
            validator.validate(
                copy.deepcopy(findings()),
                manifest,
                policy(),
                self.client(fresh),
                identity,
            )
