"""Run: python -B -m unittest -v test_github_action (no network calls)."""

import contextlib
import copy
import html
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import call, patch
from urllib.error import HTTPError

import github_action as action


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "docs with spaces"
        self.repo.mkdir()
        self.env = {
            "RUNNER_TEMP": str(self.root),
            "GITHUB_OUTPUT": str(self.root / "output"),
            "GITHUB_STEP_SUMMARY": str(self.root / "job.md"),
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_EVENT_PATH": str(self.root / "event.json"),
            "GITHUB_TOKEN": "test-token",
        }
        self.event = {"repository": {"full_name": "owner/docs"}, "pull_request": {
            "number": 7, "head": {"repo": {"full_name": "owner/docs"}}}}
        Path(self.env["GITHUB_EVENT_PATH"]).write_text(json.dumps(self.event))
        self.summary = self.root / "comment.md"
        self.summary.write_text(action.MARKER + "\n## docprop: Nothing needs review\n")

    def outputs(self):
        return dict(line.split("=", 1) for line in
                    Path(self.env["GITHUB_OUTPUT"]).read_text().splitlines())

    def test_real_cli_clean_drift_and_error_preserve_documents(self):
        (self.repo / ".docprop.toml").write_text(
            '[[mirror]]\ncanonical = "source.md"\ncopy = "copy.md"\n')
        (self.repo / "source.md").write_text("# Price\n\nThe price is $25.\n")
        for status, body in (("0", "# Price\n\nThe price is $25.\n"),
                             ("1", "# Price\n\nThe price is $20.\n"),
                             ("2", "# Price\n\nThe price is $20.\n")):
            with self.subTest(status=status):
                (self.repo / "copy.md").write_text(body)
                if status == "2":
                    (self.repo / ".docprop.toml").write_text("invalid = [")
                before = {p.name: p.read_bytes() for p in self.repo.iterdir()}
                result = subprocess.run(
                    [sys.executable, "-B", action.__file__, "check", str(self.repo)],
                    env={**os.environ, **self.env}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                outputs = self.outputs()
                self.assertEqual(outputs["exit-code"], status)
                self.assertEqual(outputs["needs-review"], str(status == "1").lower())
                summary = Path(outputs["summary-path"]).read_text()
                self.assertTrue(summary.startswith(action.MARKER))
                self.assertIn(summary, Path(self.env["GITHUB_STEP_SUMMARY"]).read_text())
                if status != "2":
                    report = json.loads(Path(outputs["report-path"]).read_text())
                    self.assertEqual(report["needs_review"], status == "1")
                    self.assertEqual(report["mirrors"][0]["state"],
                                     "DRIFTED" if status == "1" else "IN_SYNC")
                else:
                    self.assertNotIn("report-path", outputs)
                    self.assertIn("Check failed", summary)
                self.assertEqual(before, {p.name: p.read_bytes() for p in self.repo.iterdir()})
                Path(self.env["GITHUB_OUTPUT"]).unlink()

    def test_invalid_cli_payload_becomes_error_with_summary(self):
        for payload in ("not json", "[]", '{"tool":"other","needs_review":false}',
                        '{"tool":"docprop","needs_review":true}'):
            with self.subTest(payload=payload), patch.dict(os.environ, self.env), \
                    patch.object(action.subprocess, "run", return_value=
                                 subprocess.CompletedProcess([], 0, payload, "")), \
                    contextlib.redirect_stdout(io.StringIO()):
                action.check(str(self.repo))
                outputs = self.outputs()
                self.assertEqual(outputs["exit-code"], "2")
                self.assertNotIn("report-path", outputs)
                self.assertIn("Invalid docprop report", Path(outputs["summary-path"]).read_text())
                Path(self.env["GITHUB_OUTPUT"]).unlink()

    def test_comment_paginates_ignores_user_marker_and_updates_clean_result(self):
        foreign = {"id": 2, "user": {"login": "human"}, "body": action.MARKER}
        existing = {"id": 3, "user": {"login": "github-actions[bot]"},
                    "body": action.MARKER + "\n## docprop: Needs review"}
        with patch.dict(os.environ, self.env), patch.object(action, "api") as api:
            api.side_effect = [[foreign] * 100, [existing], {}]
            action.comment(str(self.summary))
            self.assertEqual(api.call_args_list, [
                call("GET", "/repos/owner/docs/issues/7/comments?per_page=100&page=1"),
                call("GET", "/repos/owner/docs/issues/7/comments?per_page=100&page=2"),
                call("PATCH", "/repos/owner/docs/issues/comments/3",
                     {"body": self.summary.read_text()})])
            api.reset_mock(side_effect=True)
            existing["body"] = self.summary.read_text()
            api.return_value = [existing]
            action.comment(str(self.summary))
            self.assertEqual(api.call_count, 1)

    def test_comment_creates_once_and_permission_failure_keeps_summary(self):
        with patch.dict(os.environ, self.env), patch.object(action, "api") as api:
            api.side_effect = [[], {}]
            action.comment(str(self.summary))
            self.assertEqual(api.call_args_list[-1], call(
                "POST", "/repos/owner/docs/issues/7/comments", {"body": self.summary.read_text()}))
            api.side_effect = HTTPError("https://api.github.com", 403, "Forbidden", {}, None)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                action.comment(str(self.summary))
            self.assertIn("comment forbidden", out.getvalue())
            self.assertTrue(self.summary.is_file())
            api.side_effect = HTTPError("https://api.github.com", 500, "Server error", {}, None)
            with self.assertRaises(HTTPError):
                action.comment(str(self.summary))

    def test_fork_push_and_missing_token_do_not_call_github(self):
        for event_name, head, token in (("push", "owner/docs", "test-token"),
                                        ("pull_request", "fork/docs", "test-token"),
                                        ("pull_request", None, "test-token"),
                                        ("pull_request", "owner/docs", "")):
            with self.subTest(event=event_name, head=head, token=bool(token)):
                self.event["pull_request"]["head"]["repo"] = {"full_name": head} if head else None
                Path(self.env["GITHUB_EVENT_PATH"]).write_text(json.dumps(self.event))
                with patch.dict(os.environ, {**self.env, "GITHUB_EVENT_NAME": event_name,
                                             "GITHUB_TOKEN": token}), \
                        patch.object(action, "api") as api, \
                        contextlib.redirect_stdout(io.StringIO()):
                    action.comment(str(self.root / "nonexistent-summary"))
                    api.assert_not_called()

    def test_rendering_is_escaped_bounded_and_commands_are_portable(self):
        downstream, target, copy_path = "FAQ's notes", "spec#price;false", "copy's name.md"
        report = {"tool": "docprop", "repo": "/runner/private/path", "needs_review": True,
                  "links": {"enabled": True, "items": [{
                      "state": "UNRECONCILED", "downstream_path": "faq.md",
                      "downstream": downstream, "target_ref": target,
                      "advice": "</pre><script>alert('x')</script>&", "acknowledge": "/runner/tool"}]},
                  "mirrors": [{"state": "DRIFTED", "copy": copy_path, "canonical": "spec.md",
                               "changed_lines": 1, "sections": ["Price"], "diff": ["-old", "+new"],
                               "sync": "/runner/python /runner/docprop.py"}],
                  "counts": {"stale": 0, "unbaselined": 1, "broken": 0,
                             "mirrors_drifted": 1, "mirrors_in_sync": 0}}
        before = copy.deepcopy(report)
        rendered = action.markdown(report)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;/pre&gt;&lt;script&gt;", rendered)
        self.assertNotIn("/runner/", rendered)
        decoded = html.unescape(rendered)
        self.assertIn(shlex.join(["doc-lattice", "reconcile", downstream, "--ref", target]), decoded)
        self.assertIn(shlex.join(["python", "/path/to/docprop/docprop.py", "sync",
                                  copy_path, "--repo", "."]), decoded)
        self.assertEqual(report, before)
        report["mirrors"][0].update(canonical_section="price", copy_section="quoted-price")
        section_report = html.unescape(action.markdown(report))
        self.assertIn("--section quoted-price", section_report)
        self.assertIn("copy's name.md#quoted-price", section_report)
        report["mirrors"][0] = {"state": "INVALID", "canonical": "spec.md", "copy": copy_path,
                                "canonical_section": "price", "copy_section": "quoted-price",
                                "note": "Section is missing"}
        self.assertIn("MIRROR INVALID", action.markdown(report))
        bounded = action.markdown(None, "<script>😀&" * 20000)
        self.assertLessEqual(len(bounded.encode()), 60000)
        self.assertIn("Report truncated", bounded)
        self.assertEqual(bounded.count("</pre>"), 1)


if __name__ == "__main__":
    unittest.main()
