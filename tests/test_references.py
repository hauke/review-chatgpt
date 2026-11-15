"""Offline source-context, version-resolution and cross-site search tests."""

import unittest
from pathlib import Path

import test_prepare

from openwrt_ai.common import Policy, ReviewError
from openwrt_ai.review import references as inspector

PATCH = """--- a/driver.c
+++ b/driver.c
@@ -1,5 +1,5 @@
 int send_packet(void);
 int handle_packet(void)
 {
-    return send_packet();
+    return safe_send_packet();
 }
"""


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_prepare.PreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        (f.repo / "driver.c").write_text(
            "int send_packet(void);\nint handle_packet(void)\n{\n    return send_packet();\n}\n"
        )
        (f.repo / "other.c").write_text("int another_path(void) { return send_packet(); }\n")
        (f.repo / "package/demo").mkdir(parents=True)
        (f.repo / "package/demo/Makefile").write_text("PKG_VERSION:=1.2\n")
        f.git("add", ".")
        f.git("commit", "-m", "Upstream context fixture")
        f.base = f.git("rev-parse", "HEAD")
        f.git("tag", "v1.2")
        f.git("tag", "v1.3")
        f.git("checkout", "-b", "context-feature")
        patch = f.repo / "package/demo/patches/100-fix.patch"
        patch.parent.mkdir()
        patch.write_text(PATCH)
        f.git("add", ".")
        f.git("commit", "-m", "Change patch")
        f.head = f.git("rev-parse", "HEAD")
        f.git("update-ref", "refs/pull/1/head", f.head)
        self.reference = {
            "url": "https://example.org/public.git",
            "match_paths": ["package/demo/**"],
            "ref_from": {
                "file": "package/demo/Makefile",
                "variable": "PKG_VERSION",
                "prefix": "v",
            },
        }

    def prepare(self):
        self.fixture.policy = Policy(
            {
                "version": 1,
                "repositories": [test_prepare.REPOSITORY],
                "reference_repositories": [self.reference],
            }
        )
        return self.fixture.prepare()

    def commit_head(self):
        f = self.fixture
        f.git("add", ".")
        f.git("commit", "-m", "Update proposed metadata")
        f.head = f.git("rev-parse", "HEAD")
        f.git("update-ref", "refs/pull/1/head", f.head)

    def test_matching_reference_is_automatic_and_pinned(self):
        result = self.prepare()
        self.assertEqual(result["failed"], [])
        self.assertEqual(len(result["references"]), 1)
        reference = result["references"][0]
        self.assertEqual(reference["sha"], self.fixture.base)
        self.assertEqual(reference["ref"], "v1.2")
        self.assertEqual(
            {binding["revision"] for binding in reference["for_prs"]}, {"base", "head"}
        )

    def test_unrelated_paths_do_not_clone_reference(self):
        self.reference["match_paths"] = ["unrelated/**"]
        self.assertEqual(self.prepare()["references"], [])
        self.assertEqual(len(self.fixture.runner.cloned), 1)

    def test_version_bump_fetches_both_versions_with_one_reference_clone(self):
        (self.fixture.repo / "package/demo/Makefile").write_text("PKG_VERSION:=1.3\n")
        self.commit_head()
        result = self.prepare()
        self.assertEqual({reference["ref"] for reference in result["references"]}, {"v1.2", "v1.3"})
        self.assertEqual(len(self.fixture.runner.cloned), 2)

    def test_make_expressions_are_never_executed(self):
        sentinel = self.fixture.root / "executed"
        (self.fixture.repo / "package/demo/Makefile").write_text(
            f"PKG_VERSION:=$(shell touch {sentinel})\n"
        )
        self.commit_head()
        references = self.prepare()["references"]
        self.assertTrue(any("error" in reference for reference in references))
        self.assertFalse(sentinel.exists())

    def test_literal_variable_and_file_fallbacks(self):
        self.reference["ref_from"]["file"] = [
            "missing/Makefile",
            "package/demo/Makefile",
        ]
        self.reference["ref_from"]["variable"] = ["PKG_UPSTREAM_VERSION", "PKG_VERSION"]
        self.assertEqual(self.prepare()["references"][0]["ref"], "v1.2")

    def test_inspector_reads_full_function_and_finds_untouched_call_site(self):
        f = self.fixture
        prepared = self.prepare()
        report = inspector.inspect(prepared, test_prepare.REPOSITORY, 1, 0, f.runner)
        patch = report["patches"][0]
        self.assertIn("send_packet", patch["search_symbols"])
        self.assertTrue(
            any(
                match["path"] == "other.c" and not match["file_changed_by_patch"]
                for match in patch["matches"]
            )
        )
        self.assertIn("int handle_packet", Path(patch["files"][0]["content_file"]).read_text())
        self.assertEqual(patch["apply_check"]["exit_code"], 0)
        self.assertFalse(patch["missing_site_candidates_are_findings"])
        self.assertEqual(
            f.runner.value("status", "--porcelain", cwd=prepared["references"][0]["worktree"]),
            "",
        )

    def test_invalid_patch_paths_are_not_followed(self):
        paths, _, _, warnings = inspector.patch_targets("--- a/../../etc/passwd\n+++ /etc/passwd\n")
        self.assertEqual(paths, [])
        self.assertTrue(warnings)

    def test_kernel_series_selects_matching_version_with_old_file_layout(self):
        f = self.fixture
        f.git("checkout", "main")
        (f.repo / "include").mkdir()
        (f.repo / "include/kernel-6.1").write_text("LINUX_VERSION-6.1 = .24\n")
        f.git("add", ".")
        f.git("commit", "-m", "Kernel version metadata")
        f.base = f.git("rev-parse", "HEAD")
        f.git("tag", "v6.1.24")
        f.git("checkout", "-b", "kernel-feature")
        patch = f.repo / "target/linux/generic/backport-6.1/100-example.patch"
        patch.parent.mkdir(parents=True)
        patch.write_text(PATCH)
        self.commit_head()
        self.reference["match_paths"] = ["target/linux/**"]
        self.reference["ref_from"] = {
            "file": ["target/linux/generic/kernel-{series}", "include/kernel-{series}"],
            "variable": "LINUX_VERSION-{series}",
            "prefix": "v{series}",
        }
        result = self.prepare()
        self.assertEqual(result["references"][0]["ref"], "v6.1.24")
        self.assertNotIn("error", result["references"][0])

    def test_missing_tag_is_reported_without_fallback_to_master(self):
        (self.fixture.repo / "package/demo/Makefile").write_text("PKG_VERSION:=9.9\n")
        self.commit_head()
        references = self.prepare()["references"]
        failed = [reference for reference in references if "error" in reference]
        self.assertEqual(failed[0]["ref"], "v9.9")
        self.assertNotIn("master", [reference.get("ref") for reference in references])

    def test_proposed_version_cannot_inject_git_options(self):
        (self.fixture.repo / "package/demo/Makefile").write_text("PKG_VERSION:=--all\n")
        self.commit_head()
        self.reference["ref_from"]["prefix"] = ""
        result = self.prepare()
        self.assertTrue(
            any("Git option" in reference.get("error", "") for reference in result["references"])
        )

    def test_invalid_reference_policy_is_rejected(self):
        for changes in (
            {"ref": "main"},
            {"match_paths": ["../secret"]},
            {
                "ref_from": {
                    "file": "../secret",
                    "variable": "PKG_VERSION",
                    "prefix": "v",
                }
            },
        ):
            with self.subTest(changes=changes), self.assertRaises(ReviewError):
                Policy(
                    {
                        "version": 1,
                        "repositories": [test_prepare.REPOSITORY],
                        "reference_repositories": [{**self.reference, **changes}],
                    }
                )
