"""Offline tests: stub doc-lattice/LLM only at their process/network boundaries."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import doc_rewrite as app


FRONT = "---\nid: sample\nderives_from:\n  - ref: source\n    seen: old\n---\n"


class RewriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        (self.repo / ".git").mkdir()
        (self.repo / "a.md").write_text("# A\nnew fact\n", encoding="utf-8")
        (self.repo / "b.md").write_text(FRONT + "# B\nold fact\n", encoding="utf-8")
        (self.repo / "c.md").write_text(FRONT + "# C\nold explanation\n", encoding="utf-8")
        self.paths = {letter: self.repo / f"{letter}.md" for letter in "abc"}

    def test_topological_sort_and_cycle(self):
        def payload(cycle=False):
            edges = [{"upstream": "a", "downstream": "b"},
                     {"upstream": "b", "downstream": "c"}]
            if cycle:
                edges.append({"upstream": "c", "downstream": "a"})
            return {"nodes": [{"id": k, "path": str(v)} for k, v in self.paths.items()],
                    "edges": edges, "ambiguous_targets": []}
        with patch.object(app, "lattice", return_value=payload()):
            paths, order = app.load_graph(self.repo, "doc-lattice", None)
        self.assertEqual(paths, self.paths)
        self.assertEqual(order, ["a", "b", "c"])
        with patch.object(app, "lattice", return_value=payload(cycle=True)):
            with self.assertRaisesRegex(app.RewriteError, "cycle"):
                app.load_graph(self.repo, "doc-lattice", None)

    def test_check_only_consumes_stale_and_refuses_broken(self):
        rows = [{"state": "OK", "source_id": "c", "target_ref": "b"},
                {"state": "STALE", "source_id": "b", "target_ref": "a#fact"}]
        with patch.object(app, "lattice", return_value={"edges": rows}):
            self.assertEqual(app.stale_edges(self.repo, "doc-lattice", None, self.paths),
                             [{"source_id": "b", "target_ref": "a#fact", "upstream_id": "a"}])
        rows[0]["state"] = "BROKEN"
        with patch.object(app, "lattice", return_value={"edges": rows}):
            with self.assertRaisesRegex(app.RewriteError, "broken"):
                app.stale_edges(self.repo, "doc-lattice", None, self.paths)

    def test_proposal_only_does_not_modify_docs(self):
        before = self.paths["b"].read_text()
        edge = {"source_id": "b", "target_ref": "a", "upstream_id": "a"}
        answer = ({"verdict": "edit", "new_text": FRONT + "# B\nnew fact\n",
                   "reason": "source changed"}, {"prompt": 10, "completion": 5, "total": 15})
        folder = self.repo / "proposals"
        with (patch.object(app, "git", return_value="HEAD"),
              patch.object(app, "load_graph", return_value=(self.paths, ["a", "b", "c"])),
              patch.object(app, "stale_edges", return_value=[edge]),
              patch.object(app, "old_text", return_value="# A\nold fact\n"),
              patch.object(app, "request_llm", return_value=answer) as llm):
            count = app.run_pipeline(self.repo, base="HEAD", config=None, binary="doc-lattice",
                                     folder=folder, log=folder / "proposals.jsonl", apply=False,
                                     model="test", api_url="https://invalid", key="test")
        self.assertEqual(count, 1)
        self.assertEqual(llm.call_count, 1)
        self.assertEqual(self.paths["b"].read_text(), before)
        self.assertIn("+new fact", next(folder.glob("*.diff")).read_text())
        record = json.loads((folder / "proposals.jsonl").read_text().strip())
        self.assertEqual(record["decision"], "")
        self.assertEqual(record["tokens"]["total"], 15)

    def test_apply_discovers_newly_stale_child_and_reconciles_each(self):
        first = {"source_id": "b", "target_ref": "a", "upstream_id": "a"}
        second = {"source_id": "c", "target_ref": "b", "upstream_id": "b"}
        answers = [
            ({"verdict": "edit", "new_text": FRONT + "# B\nnew fact\n", "reason": "a"},
             {"prompt": 1, "completion": 2, "total": 3}),
            ({"verdict": "edit", "new_text": FRONT + "# C\nnew explanation\n", "reason": "b"},
             {"prompt": 1, "completion": 2, "total": 3}),
        ]
        with (patch.object(app, "git", return_value="HEAD"),
              patch.object(app, "load_graph", return_value=(self.paths, ["a", "b", "c"])),
              patch.object(app, "stale_edges", side_effect=[[first], [second], []]),
              patch.object(app, "old_text", return_value="old"),
              patch.object(app, "request_llm", side_effect=answers) as llm,
              patch.object(app, "command", return_value="") as proc):
            folder = self.repo / "proposals"
            count = app.run_pipeline(self.repo, base="HEAD", config=None, binary="doc-lattice",
                                     folder=folder, log=folder / "proposals.jsonl", apply=True,
                                     model="test", api_url="https://invalid", key="test")
        self.assertEqual(count, 2)
        self.assertEqual(llm.call_count, 2)
        self.assertIn("new explanation", self.paths["c"].read_text())
        calls = [call.args[0] for call in proc.call_args_list]
        self.assertEqual([c[:2] for c in calls], [["doc-lattice", "reconcile"]] * 2)
        self.assertEqual(len((folder / "proposals.jsonl").read_text().splitlines()), 2)

    def test_rejects_frontmatter_change_even_with_apply(self):
        before = self.paths["b"].read_text()
        answer = ({"verdict": "edit", "new_text": before.replace("seen: old", "seen: fake"),
                   "reason": "bad"}, {"prompt": 0, "completion": 0, "total": 0})
        edge = {"source_id": "b", "target_ref": "a", "upstream_id": "a"}
        with (patch.object(app, "git", return_value="HEAD"),
              patch.object(app, "load_graph", return_value=(self.paths, ["a", "b", "c"])),
              patch.object(app, "stale_edges", return_value=[edge]),
              patch.object(app, "old_text", return_value="old"),
              patch.object(app, "request_llm", return_value=answer)):
            with self.assertRaisesRegex(app.RewriteError, "Unsafe"):
                app.run_pipeline(self.repo, base="HEAD", config=None, binary="doc-lattice",
                                 folder=self.repo / "proposals", log=self.repo / "log.jsonl",
                                 apply=True, model="test", api_url="https://invalid", key="test")
        self.assertEqual(self.paths["b"].read_text(), before)

    def test_replay_uses_detached_commit_not_current_tree(self):
        # Test git worktree checkout/cleanup without requiring doc-lattice or network.
        repo = self.repo / "history"
        repo.mkdir()
        def git(*args):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        git("init", "-q")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.com")
        (repo / "source.md").write_text("old")
        git("add", ".")
        git("commit", "-qm", "base")
        (repo / "source.md").write_text("new")
        git("commit", "-qam", "upstream edit")
        changed_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo,
                                                  text=True).strip()
        (repo / "source.md").write_text("uncommitted")
        with (patch.dict("os.environ", {"OPENAI_API_KEY": "test"}),
              patch.object(app, "run_pipeline", return_value=0) as pipeline):
            app.main(["replay", "--repo", str(repo), "--commit", changed_commit])
        checkout = pipeline.call_args.args[0]
        parent = subprocess.check_output(["git", "rev-parse", "HEAD^1"], cwd=repo,
                                         text=True).strip()
        self.assertEqual(pipeline.call_args.kwargs["base"], parent)
        self.assertEqual((repo / "source.md").read_text(), "uncommitted")
        self.assertFalse(checkout.exists())
        self.assertEqual(len(subprocess.check_output(["git", "worktree", "list", "--porcelain"],
                                                      cwd=repo, text=True).split("worktree ")) - 1, 1)


class DiffTests(unittest.TestCase):
    def test_missing_final_newline_does_not_merge_lines(self):
        diff = app.render_diff("a\nold", "a\nold\nnew\n", "doc.md")
        self.assertIn("-old\n\\ No newline at end of file\n", diff)
        self.assertIn("+old\n+new\n", diff)
        self.assertNotIn("old+old", diff)


if __name__ == "__main__":
    unittest.main()
