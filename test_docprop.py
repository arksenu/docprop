"""Tests for docprop v1 (flag-only). End-to-end tests use the real doc-lattice and Git.

Run: .work/tools/venv/bin/python -B -m unittest -v test_docprop
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import docprop

LATTICE = Path(shutil.which(docprop.default_lattice()) or docprop.default_lattice())


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def run_main(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = docprop.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class SignalTests(unittest.TestCase):
    def test_removed_phrase_and_number_are_found_in_downstream(self) -> None:
        old = "**Job 2 — Expansion:** deferred to v2.\nRanked 2nd with score 0.723."
        new = "**Job 2 — Competitive Gap Intelligence (primary):** the main flow.\nRanked 2nd with score 0.730."
        phrases, numbers = docprop.removed_signals(old, new)
        self.assertIn(("job", "2", "expansion"), phrases)
        self.assertIn("0.723", numbers)
        spans, score = docprop.matched_spans(
            "See **Job 2 — Expansion:** (deferred to v2); Linear scored 0.723.", phrases, numbers)
        self.assertTrue(any(span.startswith("Job 2 — Expansion") for span in spans), spans)
        self.assertFalse(any("*" in span for span in spans), spans)
        self.assertIn("0.723", spans)
        self.assertGreater(score, 3)

    def test_wording_still_present_upstream_is_not_flagged(self) -> None:
        old = "Annual billing gets a 10% discount.\nThe basic plan costs $20 per month."
        new = "The basic plan costs $25 per month.\nAnnual billing gets a 10% discount."
        phrases, numbers = docprop.removed_signals(old, new)
        spans, _ = docprop.matched_spans("Yes: annual billing gets a 10% discount.", phrases, numbers)
        self.assertEqual(spans, [])
        spans, _ = docprop.matched_spans("It costs $20 per month today.", phrases, numbers)
        self.assertEqual(spans, ["costs $20 per month"])

    def test_stopword_only_phrases_are_ignored(self) -> None:
        phrases, _ = docprop.removed_signals("it is in the", "something else entirely")
        self.assertEqual(phrases, set())


class StructureTests(unittest.TestCase):
    def test_passages_keep_line_numbers_headings_items_and_fences(self) -> None:
        text = ("---\nid: x\n---\n# Title\n\nFirst paragraph\ncontinues here.\n\n## Part\n"
                "- item one\n- item two\n  continued\n\n```\ncode\n\nmore\n```\n| a | b |\n")
        got = [(p.line, p.heading, p.text) for p in docprop.passages(text)]
        self.assertEqual(got[0], (4, "Title", "# Title"))
        self.assertEqual(got[1], (6, "Title", "First paragraph\ncontinues here."))
        self.assertEqual(got[3], (10, "Part", "- item one"))
        self.assertEqual(got[4], (11, "Part", "- item two\n  continued"))
        self.assertEqual(got[5], (14, "Part", "```\ncode\n\nmore\n```"))
        self.assertEqual(got[6], (19, "Part", "| a | b |"))

    def test_split_header_strips_metadata_and_leading_comments(self) -> None:
        header, body = docprop.split_header("---\nid: x\n---\n<!-- synced copy -->\n# T\nbody\n")
        self.assertEqual(header, "---\nid: x\n---\n<!-- synced copy -->\n")
        self.assertEqual(body, "# T\nbody\n")
        self.assertEqual(docprop.split_header("# Plain\n"), ("", "# Plain\n"))


class MirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name).resolve()
        write(self.repo / "brief.md", """
            # Brief

            **Status:** Active

            ## Scope

            Job 2 is primary.
            """)
        write(self.repo / "copy/brief.md", """
            <!-- Canonical version: brief.md at the root. -->
            # Brief

            **Status:** Active

            ## Scope

            Job 1 is primary.
            """)
        write(self.repo / ".docprop.toml", '[[mirror]]\ncanonical = "brief.md"\ncopy = "copy/brief.md"\n')

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_drift_is_reported_by_section_and_sync_keeps_header(self) -> None:
        code, out, _ = run_main("check", "--repo", str(self.repo), "--format", "json")
        self.assertEqual(code, docprop.EXIT_FINDINGS)
        report = json.loads(out)
        self.assertFalse(report["links"]["enabled"])
        mirror = report["mirrors"][0]
        self.assertEqual((mirror["state"], mirror["sections"], mirror["changed_lines"]),
                         ("DRIFTED", ["Scope"], 1))
        code, out, _ = run_main("sync", "copy/brief.md", "--repo", str(self.repo))
        self.assertEqual(code, docprop.EXIT_CLEAN, out)
        synced = (self.repo / "copy/brief.md").read_text(encoding="utf-8")
        self.assertTrue(synced.startswith("<!-- Canonical version: brief.md at the root. -->\n# Brief\n"))
        self.assertIn("Job 2 is primary.", synced)
        code, out, _ = run_main("check", "--repo", str(self.repo))
        self.assertEqual(code, docprop.EXIT_CLEAN, out)
        self.assertIn("MIRROR OK", out)
        _, out, _ = run_main("sync", "copy/brief.md", "--repo", str(self.repo))
        self.assertIn("nothing written", out)

    def test_sync_preserves_crlf_line_endings(self) -> None:
        copy = self.repo / "copy/brief.md"
        copy.write_bytes(copy.read_bytes().replace(b"\n", b"\r\n"))
        run_main("sync", "copy/brief.md", "--repo", str(self.repo))
        data = copy.read_bytes()
        self.assertIn(b"Job 2 is primary.\r\n", data)
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))

    def test_invalid_configs_are_errors(self) -> None:
        cases = {
            'colour = "x"\n': "Unknown key",
            '[[mirror]]\ncanonical = "brief.md"\n': "exactly the string keys",
            '[[mirror]]\ncanonical = "brief.md"\ncopy = "../outside.md"\n': "inside the repo",
            '[[mirror]]\ncanonical = "brief.md"\ncopy = "notes.txt"\n': "Markdown file",
            '[[mirror]]\ncanonical = "brief.md"\ncopy = "brief.md"\n': "same file",
        }
        for body, message in cases.items():
            (self.repo / ".docprop.toml").write_text(body, encoding="utf-8")
            code, _, err = run_main("check", "--repo", str(self.repo))
            self.assertEqual(code, docprop.EXIT_ERROR, body)
            self.assertIn(message, err, body)

    def test_sync_rejects_undeclared_copy(self) -> None:
        code, _, err = run_main("sync", "brief.md", "--repo", str(self.repo))
        self.assertEqual(code, docprop.EXIT_ERROR)
        self.assertIn("not a declared mirror copy", err)


@unittest.skipUnless(LATTICE.is_file(), "doc-lattice is not installed")
class SectionQuoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        self.source = self.repo / "source.md"
        self.copy = self.repo / "copy.md"
        self.config = self.repo / ".docprop.toml"
        self.source.write_text("# Source\n\n## Prices {#price}\n\nCosts $25.\n\n"
                               "### Seats\n\nIncludes 5 seats.\n\n## Other\nSource only.\n")
        self.prefix = "<!-- retained -->\n# Copy\n\nIntroduction.\n\n## Our pricing {#quote-price}\n"
        self.suffix = "## Next\n\nKeep this exactly.  \n"
        self.copy.write_text(self.prefix + "\nCosts $20.\n\n" + self.suffix)
        self.config.write_text('[[mirror]]\ncanonical = "source.md"\ncopy = "copy.md"\n'
                               'canonical_section = "price"\ncopy_section = "quote-price"\n')

    def check(self):
        code, out, err = run_main("check", "--repo", str(self.repo), "--format", "json")
        self.assertNotEqual(code, docprop.EXIT_ERROR, err)
        return code, json.loads(out)

    def sync(self, section="quote-price"):
        return run_main("sync", "copy.md", "--section", section, "--repo", str(self.repo))

    def test_quote_sync_preserves_heading_surroundings_mode_and_is_idempotent(self):
        code, report = self.check()
        self.assertEqual(code, docprop.EXIT_FINDINGS)
        [item] = report["mirrors"]
        self.assertEqual(item["copy_section"], "quote-price")
        self.assertEqual(item["canonical_section"], "price")
        self.assertIn("--section quote-price", item["sync"])
        self.assertNotIn("Source only", "\n".join(item["diff"]))
        self.copy.chmod(0o640)
        self.assertEqual(self.sync()[0], docprop.EXIT_CLEAN)
        after = self.copy.read_bytes()
        self.assertTrue(after.startswith(self.prefix.encode()))
        self.assertTrue(after.endswith(self.suffix.encode()))
        self.assertIn(b"### Seats\n\nIncludes 5 seats.", after)
        self.assertNotIn(b"## Prices", after)
        self.assertEqual(self.copy.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.check()[0], docprop.EXIT_CLEAN)
        self.assertIn("nothing written", self.sync()[1])
        self.assertEqual(self.copy.read_bytes(), after)
        self.source.write_text(self.source.read_text().replace("Source only", "Unrelated edit"))
        self.assertEqual(self.check()[0], docprop.EXIT_CLEAN)
        self.source.write_text(self.source.read_text().replace("Costs $25", "Costs $30"))
        self.assertEqual(self.check()[0], docprop.EXIT_FINDINGS)

    def test_newlines_metadata_fences_and_unicode_separators(self):
        self.source.write_text("## Source {#price}\n\n```markdown\n## Example only\n```\n"
                               "A\u2028## Still one line\n")
        for newline in ("\n", "\r\n", "\r"):
            with self.subTest(newline=repr(newline)):
                prefix = ("---\nid: copy\n---\n" + self.prefix).replace("\n", newline)
                # Mixed newlines outside the selection must remain byte-for-byte intact.
                suffix = self.suffix.replace("\n", "\r\n")
                self.copy.write_bytes((prefix + newline + "Old." + newline + suffix).encode())
                code, _, err = self.sync()
                self.assertEqual(code, docprop.EXIT_CLEAN, err)
                result = self.copy.read_bytes()
                self.assertTrue(result.startswith(prefix.encode()))
                self.assertTrue(result.endswith(suffix.encode()))
                self.assertIn("A\u2028## Still one line".encode(), result)
                self.assertEqual(self.check()[0], docprop.EXIT_CLEAN)

    def test_explicit_ids_survive_heading_renames(self):
        self.source.write_text(self.source.read_text().replace("Prices {#price}", "New prices {#price}"))
        self.copy.write_text(self.copy.read_text().replace("Our pricing {#quote-price}", "Fees {#quote-price}"))
        self.assertEqual(self.sync()[0], docprop.EXIT_CLEAN)
        self.assertIn("## Fees {#quote-price}", self.copy.read_text())
        self.assertEqual(self.check()[0], docprop.EXIT_CLEAN)

    def test_markdown_hard_breaks_count_as_drift(self):
        self.source.write_text("## Price {#price}\nFirst line.  \nSecond line.\n")
        self.copy.write_text(self.prefix + "First line.\nSecond line.\n" + self.suffix)
        self.assertEqual(self.check()[0], docprop.EXIT_FINDINGS)
        self.assertEqual(self.sync()[0], docprop.EXIT_CLEAN)
        self.assertIn("First line.  \nSecond line.", self.copy.read_text())
        self.assertEqual(self.check()[0], docprop.EXIT_CLEAN)

    def test_end_of_file_without_newline_and_empty_destination(self):
        self.source.write_text("## Prices {#price}\nCosts $25.")
        for copy in (self.prefix + "Old.\n" + self.suffix, "## Quote {#quote-price}"):
            with self.subTest(copy=copy):
                self.copy.write_text(copy)
                code, _, err = self.sync()
                self.assertEqual(code, docprop.EXIT_CLEAN, err)
                self.assertIn("\nCosts $25.", self.copy.read_text())
                self.assertEqual(self.check()[0], docprop.EXIT_CLEAN)
                self.assertIn("nothing written", self.sync()[1])

    def test_invalid_anchors_and_unsafe_content_refuse_writes(self):
        cases = {
            "missing": "## Different\nNew text.\n",
            "duplicate explicit": "## One {#price}\nA\n## Two {#price}\nB\n",
            "unclosed fence": "## Price {#price}\n```\nUnclosed code.\n",
            "wrong level": "# Price {#price}\nNew text.\n",
            "colliding destination ID": "## Price {#price}\n### Subsection {#quote-price}\nNew.\n",
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                self.source.write_text(text)
                before = self.copy.read_bytes()
                code, report = self.check()
                self.assertEqual(code, docprop.EXIT_FINDINGS)
                self.assertEqual(report["mirrors"][0]["state"], "INVALID")
                self.assertEqual(self.sync()[0], docprop.EXIT_ERROR)
                self.assertEqual(self.copy.read_bytes(), before)

    def test_invalid_destination_and_failed_replace_keep_original_file(self):
        original = self.copy.read_bytes()
        for body in ("## Wrong\nKeep.\n", self.prefix + "Old.\n## Duplicate {#quote-price}\nKeep.\n"):
            with self.subTest(body=body):
                self.copy.write_text(body)
                self.assertEqual(self.check()[1]["mirrors"][0]["state"], "INVALID")
                self.assertEqual(self.sync()[0], docprop.EXIT_ERROR)
                self.assertEqual(self.copy.read_text(), body)
        self.copy.write_bytes(original)
        with patch.object(Path, "replace", side_effect=PermissionError("Replacement denied")):
            code, _, err = self.sync()
        self.assertEqual(code, docprop.EXIT_ERROR)
        self.assertIn("Replacement denied", err)
        self.assertEqual(self.copy.read_bytes(), original)
        self.assertEqual(list(self.repo.glob(".docprop-*")), [])

    def test_generated_anchor_collisions_include_non_atx_headings(self):
        self.config.write_text(self.config.read_text().replace('"price"', '"prices"'))
        for text in ("## Prices\nA\n## Prices\nB\n", "Prices\n------\nA\n## Prices\nB\n"):
            with self.subTest(text=text):
                self.source.write_text(text)
                self.assertEqual(self.check()[1]["mirrors"][0]["state"], "INVALID")
                before = self.copy.read_bytes()
                self.assertEqual(self.sync()[0], docprop.EXIT_ERROR)
                self.assertEqual(self.copy.read_bytes(), before)

    def test_two_independent_copies_require_a_section_selector(self):
        self.copy.write_text(self.copy.read_text() + "\n## Another {#second}\nOld second.\n")
        entry = self.config.read_text()
        self.config.write_text(entry + entry.replace('"quote-price"', '"second"'))
        before = self.copy.read_bytes()
        self.assertEqual(run_main("sync", "copy.md", "--repo", str(self.repo))[0], docprop.EXIT_ERROR)
        self.assertEqual(self.copy.read_bytes(), before)
        self.assertEqual(self.sync()[0], docprop.EXIT_CLEAN)
        self.assertIn("Old second.", self.copy.read_text())
        self.assertEqual(self.sync("second")[0], docprop.EXIT_CLEAN)
        self.assertEqual(self.check()[0], docprop.EXIT_CLEAN)

    def test_config_rejects_incomplete_duplicate_and_overlapping_destinations(self):
        entry = self.config.read_text()
        cases = [entry.replace('copy_section = "quote-price"\n', ''),
                 entry.replace('copy_section = "quote-price"', 'copy_section = ""'),
                 entry.replace('copy_section = "quote-price"', 'copy_section = "#quote-price"'),
                 entry + entry,
                 entry + '[[mirror]]\ncanonical = "source.md"\ncopy = "copy.md"\n',
                 entry + entry.replace('"quote-price"', '"child"')]
        self.copy.write_text(self.prefix + "\n### Child {#child}\nOld.\n" + self.suffix)
        before = self.copy.read_bytes()
        for text in cases:
            with self.subTest(config=text):
                self.config.write_text(text)
                self.assertEqual(run_main("check", "--repo", str(self.repo))[0], docprop.EXIT_ERROR)
                self.assertEqual(self.sync()[0], docprop.EXIT_ERROR)
                self.assertEqual(self.copy.read_bytes(), before)

    def test_same_file_disjoint_sections_work_and_overlapping_ones_fail(self):
        self.config.write_text(self.config.read_text().replace('canonical = "source.md"',
                                                             'canonical = "copy.md"'))
        self.copy.write_text("## Source {#price}\nNew.\n\n" + self.copy.read_text())
        self.assertEqual(self.sync()[0], docprop.EXIT_CLEAN)
        self.assertEqual(self.check()[0], docprop.EXIT_CLEAN)
        self.copy.write_text("## Source {#price}\nNew.\n### Copy {#quote-price}\nOld.\n")
        before = self.copy.read_bytes()
        self.assertEqual(self.sync()[0], docprop.EXIT_ERROR)
        self.assertEqual(self.copy.read_bytes(), before)


@unittest.skipUnless(LATTICE.is_file(), "doc-lattice is not installed")
class LinkTests(unittest.TestCase):
    """A section link (faq <- spec#pricing) plus an upstream that itself has a link."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name).resolve()
        git(self.repo, "init", "-q")
        write(self.repo / ".doc-lattice.yml", "lattice_format: 2\ndocs_roots: [docs]\n")
        write(self.repo / "docs/vision.md", "---\nid: vision\n---\n# Vision\n\nCheap and simple.\n")
        self.spec = self.repo / "docs/spec.md"
        write(self.spec, """
            ---
            id: spec
            derives_from:
              - ref: vision
            ---
            # Spec

            ## Pricing

            The basic plan costs $20 per month and includes 3 seats.
            Annual billing gets a 10% discount.

            ## Support

            Email support only.
            """)
        write(self.repo / "docs/faq.md", """
            ---
            id: faq
            derives_from:
              - ref: spec#pricing
            ---
            # FAQ

            ## How much does it cost?

            The basic plan costs $20 per month with 3 seats included.

            ## Can I get a discount?

            Yes: annual billing gets a 10% discount.
            """)
        self.reconcile()
        self.c1 = commit(self.repo, "Baseline")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def reconcile(self) -> None:
        subprocess.run([str(LATTICE), "reconcile", "--all"], cwd=self.repo, check=True,
                       capture_output=True, text=True)

    def edit_spec(self, old: str, new: str) -> None:
        self.spec.write_text(self.spec.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    def check(self, *extra: str) -> tuple[int, dict]:
        code, out, err = run_main("check", "--repo", str(self.repo), "--format", "json", *extra)
        self.assertNotEqual(code, docprop.EXIT_ERROR, err)
        return code, json.loads(out)

    def test_clean_repo_needs_no_review(self) -> None:
        code, report = self.check()
        self.assertEqual(code, docprop.EXIT_CLEAN)
        self.assertEqual(report["links"]["items"], [])

    def test_stale_link_finds_reviewed_commit_change_and_passage(self) -> None:
        self.edit_spec("$20 per month and includes 3 seats", "$25 per month and includes 5 seats")
        c2 = commit(self.repo, "Raise price")
        self.edit_spec("Email support only.", "Email and chat support.")
        commit(self.repo, "Add chat support")  # newer commit, same Pricing section as c2
        code, report = self.check()
        self.assertEqual(code, docprop.EXIT_FINDINGS)
        [item] = report["links"]["items"]
        self.assertEqual((item["state"], item["downstream"], item["target_ref"]),
                         ("STALE", "faq", "spec#pricing"))
        self.assertEqual(item["baseline"]["commit"], self.c1)
        self.assertNotEqual(item["baseline"]["commit"], c2)
        self.assertIn("-The basic plan costs $20 per month and includes 3 seats.", item["upstream_diff"])
        self.assertNotIn("Email", "\n".join(item["upstream_diff"]))
        [passage] = item["passages"]
        self.assertEqual(passage["heading"], "How much does it cost?")
        self.assertEqual(passage["matched"], ["plan costs $20 per month"])
        faq_lines = (self.repo / "docs/faq.md").read_text(encoding="utf-8").split("\n")
        self.assertIn("$20", faq_lines[passage["line"] - 1])
        self.assertIn("reconcile faq --ref 'spec#pricing'", item["acknowledge"])
        code, out, _ = run_main("check", "--repo", str(self.repo))
        self.assertIn("Look here first: 1 downstream passage(s)", out)
        self.reconcile()
        self.assertEqual(self.check()[0], docprop.EXIT_CLEAN)

    def test_uncommitted_upstream_edit_compares_with_head(self) -> None:
        self.edit_spec("Email support only.", "Email and chat support.")
        c2 = commit(self.repo, "Unrelated support change")
        self.edit_spec("includes 3 seats", "includes 5 seats")  # not committed
        _, report = self.check()
        [item] = report["links"]["items"]
        self.assertEqual(item["baseline"]["commit"], c2)
        self.assertEqual(item["passages"], [])  # "3" is too short to be a number signal

    def test_baseline_missing_from_history_reports_it_and_base_fallback_works(self) -> None:
        self.edit_spec("$20", "$22")
        self.reconcile()  # reviewed text is never committed
        self.edit_spec("$22", "$30")
        _, report = self.check()
        [item] = report["links"]["items"]
        self.assertIsNone(item["baseline"])
        self.assertIn("not in Git history", item["note"])
        _, report = self.check("--base", "HEAD")
        [item] = report["links"]["items"]
        self.assertEqual(item["baseline"]["commit"], "HEAD")
        self.assertTrue(item["upstream_diff"])

    def test_unbaselined_link_needs_review(self) -> None:
        write(self.repo / "docs/onboarding.md",
              "---\nid: onboarding\nderives_from:\n  - ref: spec#support\n---\n# Onboarding\n")
        code, report = self.check()
        self.assertEqual(code, docprop.EXIT_FINDINGS)
        [item] = report["links"]["items"]
        self.assertEqual(item["state"], "UNRECONCILED")
        self.assertEqual(report["counts"]["unbaselined"], 1)

    def test_baseline_found_in_revision_from_before_markings_were_added(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            git(repo, "init", "-q")
            plain = "# Spec\n\n## Pricing\n\nThe basic plan costs $20 per month.\n"
            write(repo / "docs/spec.md", plain)
            c0 = commit(repo, "Spec before any markings")
            write(repo / ".doc-lattice.yml", "lattice_format: 2\ndocs_roots: [docs]\n")
            write(repo / "docs/spec.md", "<!-- doc-lattice\nid: spec\n-->\n" + plain)
            write(repo / "docs/faq.md", "---\nid: faq\nderives_from:\n  - ref: spec#pricing\n---\n"
                                        "# FAQ\n\nIt costs $20 per month.\n")
            subprocess.run([str(LATTICE), "reconcile", "--all"], cwd=repo, check=True,
                           capture_output=True)
            spec = repo / "docs/spec.md"
            spec.write_text(spec.read_text(encoding="utf-8").replace("$20", "$30"), encoding="utf-8")
            commit(repo, "Add markings and raise the price")
            code, out, err = run_main("check", "--repo", str(repo), "--format", "json")
            self.assertEqual(code, docprop.EXIT_FINDINGS, err)
            [item] = json.loads(out)["links"]["items"]
            self.assertEqual(item["baseline"]["commit"], c0)
            self.assertEqual(item["passages"][0]["matched"], ["costs $20 per month"])


if __name__ == "__main__":
    unittest.main()
