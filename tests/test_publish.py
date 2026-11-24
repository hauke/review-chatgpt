"""Exercise live publisher checks against an offline GitHub HTTP fixture."""

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from test_review import (
    BOT,
    BOT_ID,
    HEAD,
    NOW,
    PREVIOUS,
    REPOSITORY,
    FakeAPI,
    agent_comment,
    api_routes,
    findings,
    policy,
    review,
)

from openwrt_ai.common import GitHubError, ReviewError, marker
from openwrt_ai.review.discover import discover
from openwrt_ai.review.publish import PUBLISH_TOKEN_ENV, GitHubPublisher, publish
from openwrt_ai.review.validate import validate

ROOT = f"/repos/{REPOSITORY}"
REVIEWS = f"{ROOT}/pulls/1/reviews"
ISSUES = f"{ROOT}/issues/1/comments"
PAGE = "?per_page=100&page=1"
IDENTITY = {"login": BOT, "id": BOT_ID}


class PublishTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        self.routes = {**api_routes(), "/user": IDENTITY}
        self.findings = findings()
        self.policy = policy()
        self.writes = []
        self.failure = None
        with mock.patch.dict("os.environ", {PUBLISH_TOKEN_ENV: "test-token"}, clear=True):
            self.api = GitHubPublisher()
        self.api.opener = mock.Mock()
        self.api.opener.open.side_effect = self.open

    def open(self, request, timeout):
        url = urlsplit(request.full_url)
        self.assertEqual((url.scheme, url.netloc), ("https", "api.github.com"))
        endpoint = url.path + ("?" + url.query if url.query else "")
        method = request.get_method()
        if method == "GET":
            return io.BytesIO(json.dumps(self.routes[endpoint]).encode())
        payload = json.loads(request.data)
        self.writes.append((method, endpoint, payload))
        if self.failure:
            raise self.failure
        if method == "POST" and endpoint == REVIEWS:
            result = review(body=payload["body"], commit_id=payload["commit_id"])
            self.routes[REVIEWS + PAGE].append(result)
        elif method == "PUT" and endpoint == REVIEWS + "/10":
            result = {**self.routes[endpoint], "body": payload["body"]}
            self.routes[REVIEWS + PAGE] = [result]
        elif method == "PATCH" and endpoint == ROOT + "/issues/comments/20":
            result = {**self.routes[endpoint], "body": payload["body"]}
            self.routes[ISSUES + PAGE] = [result]
        else:
            self.fail(f"unexpected write: {method} {endpoint}")
        return io.BytesIO(json.dumps(result).encode())

    def preview(self):
        api = FakeAPI(self.routes)
        self.manifest = discover(api, self.policy, repository=REPOSITORY, number=1)
        return validate(
            self.findings,
            self.manifest,
            self.policy,
            api,
            {**IDENTITY, "observed_at": NOW.isoformat()},
            now=NOW,
        )

    def publish(self, preview):
        return publish(self.findings, self.manifest, preview, self.policy, self.api, self.state)

    def test_write_is_submitted_at_the_validated_head_and_repeat_is_skipped(self):
        preview = self.preview()
        self.assertEqual(self.publish(preview)["status"], "published")
        self.assertEqual(self.publish(preview)["status"], "already-published")
        self.assertEqual(
            self.writes,
            [
                (
                    "POST",
                    REVIEWS,
                    {
                        "event": "COMMENT",
                        "commit_id": HEAD,
                        "body": preview["review_preview"]["review"],
                        "comments": preview["review_preview"]["file_comments"],
                    },
                )
            ],
        )
        self.assertIn(marker(HEAD), self.routes[REVIEWS + PAGE][0]["body"])

    def test_both_existing_comment_kinds_are_extended(self):
        for method, listing, endpoint, old in (
            ("PUT", REVIEWS + PAGE, REVIEWS + "/10", review(sha=PREVIOUS)),
            ("PATCH", ISSUES + PAGE, ROOT + "/issues/comments/20", agent_comment()),
        ):
            with self.subTest(method=method):
                self.routes[REVIEWS + PAGE] = []
                self.routes[ISSUES + PAGE] = []
                self.routes[listing] = [old]
                self.routes[endpoint] = old
                preview = self.preview()
                self.assertEqual(self.publish(preview)["status"], "updated")
                actual_method, actual_endpoint, payload = self.writes[-1]
                self.assertEqual((actual_method, actual_endpoint), (method, endpoint))
                self.assertTrue(payload["body"].startswith(old["body"] + "\n"))
                self.assertIn(marker(HEAD), payload["body"])

    def test_live_claude_comment_blocks_publication(self):
        preview = self.preview()
        self.routes[ISSUES + PAGE] = [{"user": {"login": "claude[bot]"}, "body": "Earlier review"}]
        with self.assertRaisesRegex(ReviewError, "Claude"):
            self.publish(preview)
        self.assertEqual(self.writes, [])

    def test_new_head_blocks_publication(self):
        preview = self.preview()
        self.routes[ROOT + "/pulls/1"]["head"]["sha"] = "4" * 40
        with self.assertRaisesRegex(ReviewError, "head or base changed"):
            self.publish(preview)
        self.assertEqual(self.writes, [])

    def test_wrong_live_identity_blocks_publication(self):
        preview = self.preview()
        self.routes["/user"] = {"login": "hauke", "id": 42}
        with self.assertRaisesRegex(ReviewError, "authenticate as openwrt-ai"):
            self.publish(preview)
        self.assertEqual(self.writes, [])

    def test_altered_preview_blocks_publication(self):
        preview = self.preview()
        preview["review_preview"]["file_comments"][0]["body"] = "Changed after validation"
        with self.assertRaisesRegex(ReviewError, "live review differs"):
            self.publish(preview)
        self.assertEqual(self.writes, [])

    def test_changed_comment_body_blocks_update(self):
        old = agent_comment()
        self.routes[ISSUES + PAGE] = [old]
        preview = self.preview()
        self.routes[ROOT + "/issues/comments/20"] = {**old, "body": old["body"] + " Changed"}
        with self.assertRaisesRegex(ReviewError, "body or ownership changed"):
            self.publish(preview)
        self.assertEqual(self.writes, [])

    def test_ambiguous_write_is_not_retried_until_reconciled(self):
        preview = self.preview()
        self.failure = urllib.error.URLError("timed out")
        with self.assertRaises(GitHubError):
            self.publish(preview)
        with self.assertRaisesRegex(ReviewError, "outcome is unknown"):
            self.publish(preview)
        self.assertEqual(len(self.writes), 1)

        self.routes[REVIEWS + PAGE] = [review(body=preview["review_preview"]["review"])]
        self.assertEqual(self.publish(preview)["status"], "already-published")
        self.assertEqual(list(self.state.glob("*.pending")), [])
        self.assertEqual(len(self.writes), 1)

    def test_review_read_token_is_not_used_for_publication(self):
        with mock.patch.dict(
            "os.environ", {"CHATGPT_REVIEW_GITHUB_TOKEN": "test-token"}, clear=True
        ):
            with self.assertRaisesRegex(ReviewError, PUBLISH_TOKEN_ENV):
                GitHubPublisher()
