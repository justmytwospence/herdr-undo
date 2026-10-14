import sys
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import undo as u  # noqa: E402

# layout.export as herdr 0.9.4 returns it: right split, the right half split down.
TREE = {"type": "split", "direction": "right", "ratio": 0.5,
        "first": {"type": "pane", "pane_id": "w1:p1", "cwd": "/private/tmp"},
        "second": {"type": "split", "direction": "down", "ratio": 0.3,
                   "first": {"type": "pane", "pane_id": "w1:p2", "cwd": "/usr"},
                   "second": {"type": "pane", "pane_id": "w1:p3", "label": "etcpane", "cwd": "/private/etc"}}}
PI = {"agent": "pi", "kind": "path", "source": "herdr:pi", "value": "/s/a b.jsonl"}


def pane(pid, tab="w1:t1", ws="w1", **extra):
    return {"pane_id": pid, "tab_id": tab, "workspace_id": ws, **extra}


def shadow():
    return {
        "panes": {"w1:p1": pane("w1:p1", cwd="/private/tmp", foreground_cwd="/private/tmp/sub"),
                  "w1:p2": pane("w1:p2", cwd="/usr", agent="pi", agent_session=PI),
                  "w1:p3": pane("w1:p3", cwd="/private/etc", label="etcpane"),
                  "w1:p4": pane("w1:p4", tab="w1:t2", cwd="/var"),
                  "w2:p1": pane("w2:p1", tab="w2:t1", ws="w2", cwd="/opt")},
        "tabs": {"w1:t1": {"tab_id": "w1:t1", "workspace_id": "w1", "number": 1, "label": "main"},
                 "w1:t2": {"tab_id": "w1:t2", "workspace_id": "w1", "number": 2, "label": "two"},
                 "w2:t1": {"tab_id": "w2:t1", "workspace_id": "w2", "number": 1, "label": "1"}},
        "workspaces": {"w1": {"workspace_id": "w1", "number": 1, "label": "A", "active_tab_id": "w1:t1"},
                       "w2": {"workspace_id": "w2", "number": 2, "label": "B", "active_tab_id": "w2:t1"}},
        "trees": {"w1:t1": TREE,
                  "w1:t2": {"type": "pane", "pane_id": "w1:p4", "cwd": "/var"},
                  "w2:t1": {"type": "pane", "pane_id": "w2:p1", "cwd": "/opt"}},
    }


class Resume(unittest.TestCase):
    def test_table(self):
        self.assertEqual(u.resume_command({"agent_session": PI}), "pi --session '/s/a b.jsonl'")
        self.assertEqual(u.resume_command({"agent_session": {"agent": "claude", "value": "abc"}}), "claude --resume abc")
        self.assertEqual(u.resume_command({"agent_session": {"agent": "codex", "value": "t1"}}), "codex resume t1")
        self.assertEqual(u.resume_command({"agent_session": {"agent": "opencode", "value": "s"}}), "opencode --session s")
        self.assertIsNone(u.resume_command({"agent_session": {"agent": "droid", "value": "x"}}))
        self.assertIsNone(u.resume_command({"agent": "pi"}))


class Trees(unittest.TestCase):
    def test_spec_tree_keeps_shape_and_prefers_foreground_cwd(self):
        spec = u.spec_tree(TREE, shadow()["panes"])
        self.assertEqual(spec["second"]["ratio"], 0.3)
        self.assertEqual(spec["first"], {"type": "pane", "cwd": "/private/tmp/sub", "was": "w1:p1"})
        self.assertEqual(spec["second"]["first"]["resume"], "pi --session '/s/a b.jsonl'")
        self.assertEqual(spec["second"]["second"]["label"], "etcpane")

    def test_apply_tree_placeholders_for_survivors(self):
        spec = u.spec_tree(TREE, shadow()["panes"])
        out = u.apply_tree(spec, keep={"w1:p1", "w1:p3"})
        self.assertEqual(out["first"]["command"], u.PLACEHOLDER)
        self.assertEqual(out["second"]["second"]["command"], u.PLACEHOLDER)
        self.assertNotIn("label", out["second"]["second"])  # the survivor keeps its own
        self.assertEqual(out["second"]["first"], {"type": "pane", "cwd": "/usr"})
        self.assertNotIn("was", str(out))
        self.assertNotIn("resume", str(out))


class Capture(unittest.TestCase):
    def test_pane_in_split_is_a_pane_entry(self):
        e = u.capture("pane_closed", {"pane_id": "w1:p2", "workspace_id": "w1"}, shadow())
        self.assertEqual((e["kind"], e["pane_id"], e["tab_id"], e["index"]), ("pane", "w1:p2", "w1:t1", 0))
        self.assertEqual(len(u.leaves(e["tree"])), 3)

    def test_last_pane_of_tab_is_a_tab_entry(self):
        e = u.capture("pane_closed", {"pane_id": "w1:p4", "workspace_id": "w1"}, shadow())
        self.assertEqual((e["kind"], e["label"], e["index"]), ("tab", "two", 1))
        self.assertEqual(e["workspace"]["label"], "A")

    def test_last_pane_of_space_is_a_space_entry(self):
        e = u.capture("pane_closed", {"pane_id": "w2:p1", "workspace_id": "w2"}, shadow())
        self.assertEqual((e["kind"], e["label"], e["index"]), ("workspace", "B", 1))
        self.assertEqual([t["label"] for t in e["tabs"]], ["1"])

    def test_space_keeps_tab_order(self):
        e = u.capture("workspace_closed", {"workspace_id": "w1"}, shadow())
        self.assertEqual([t["label"] for t in e["tabs"]], ["main", "two"])

    def test_unknown_ids(self):
        self.assertIsNone(u.capture("pane_closed", {"pane_id": "zz", "workspace_id": "w1"}, shadow()))
        self.assertIsNone(u.capture("tab_closed", {"tab_id": "zz", "workspace_id": "w1"}, shadow()))

    def test_covers(self):
        tab = u.capture("pane_closed", {"pane_id": "w1:p4", "workspace_id": "w1"}, shadow())
        self.assertTrue(u.covers(tab, "tab_closed", {"tab_id": "w1:t2", "workspace_id": "w1"}))
        self.assertFalse(u.covers(tab, "workspace_closed", {"workspace_id": "w1"}))
        space = u.capture("pane_closed", {"pane_id": "w2:p1", "workspace_id": "w2"}, shadow())
        self.assertTrue(u.covers(space, "tab_closed", {"tab_id": "w2:t1", "workspace_id": "w2"}))
        self.assertTrue(u.covers(space, "workspace_closed", {"workspace_id": "w2"}))


class Remap(unittest.TestCase):
    def test_older_entries_follow_a_restored_space(self):
        pane = u.capture("pane_closed", {"pane_id": "w1:p2", "workspace_id": "w1"}, shadow())
        ids = {"w1": "w9", "w1:t1": "w9:t3", "w1:p1": "w9:p7", "w1:p3": "w9:p8"}
        out = u.remap([pane], ids)[0]
        self.assertEqual((out["workspace_id"], out["tab_id"], out["pane_id"]), ("w9", "w9:t3", "w1:p2"))
        self.assertEqual([leaf["was"] for leaf in u.leaves(out["tree"])], ["w9:p7", "w1:p2", "w9:p8"])
        self.assertEqual(out["workspace"]["workspace_id"], "w9")
        self.assertEqual(out["tree"]["first"]["cwd"], "/private/tmp/sub")  # values, not keys, untouched


class Stack(unittest.TestCase):
    def test_limit_and_age(self):
        stack = [{"kind": "tab", "at": 0}]
        for i in range(30):
            stack = u.push(stack, {"kind": "tab", "n": i}, now=u.MAX_AGE + 10)
        self.assertEqual(len(stack), u.STACK_LIMIT)
        self.assertEqual(stack[-1]["n"], 29)
        self.assertNotIn(0, [e["at"] for e in stack])

    def test_store_and_suppress(self):
        with tempfile.TemporaryDirectory() as d:
            store = u.Store(Path(d))
            self.assertIsNone(store.pop())
            store.push({"kind": "tab", "label": "x"})
            self.assertEqual(store.top()["label"], "x")
            self.assertEqual(store.pop()["label"], "x")
            store.suppress(["w1:p9"])
            self.assertTrue(store.suppressed("w1:p9"))
            self.assertFalse(store.suppressed("w1:p8"))

    def test_handle_coalesces_and_suppresses(self):
        with tempfile.TemporaryDirectory() as d:
            store = u.Store(Path(d))
            last = u.handle("pane_closed", {"pane_id": "w1:p4", "workspace_id": "w1"}, shadow(), store, (None, 0))
            u.handle("tab_closed", {"tab_id": "w1:t2", "workspace_id": "w1"}, shadow(), store, last)
            store.suppress(["w1:p2"])
            u.handle("pane_closed", {"pane_id": "w1:p2", "workspace_id": "w1"}, shadow(), store, last)
            self.assertEqual(len(u.Store(Path(d))._read(store.stack_path, [])), 1)


if __name__ == "__main__":
    unittest.main()
