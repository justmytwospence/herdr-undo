"""Reopen the last closed Herdr pane, tab or space.

A daemon keeps a shadow of every space, tab and pane layout, so that when a close
event arrives (which carries only IDs) the state just before it is still known; it
pushes that onto an undo stack. The `reopen` action pops the stack and rebuilds
what was closed: same split tree, ratios, directories and labels, with agent
sessions resumed. Processes and scrollback cannot come back.
"""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import shlex
import socket
import subprocess
import sys
import time

STACK_LIMIT = 20
MAX_AGE = 24 * 60 * 60
REFRESH_EVERY = 3.0        # seconds; pane renames emit no event
DEBOUNCE = 0.2             # seconds of quiet before refreshing after events
COALESCE = 2.0             # a tab/space close right after the pane close that caused it
SUPPRESS_FOR = 15.0        # seconds the action's own closes are ignored
PLACEHOLDER = ["sleep", "600"]

SUBSCRIPTIONS = [
    "pane.created", "pane.closed", "pane.updated", "pane.moved", "pane.agent_detected",
    "layout.updated", "tab.created", "tab.closed", "tab.renamed", "tab.moved",
    "workspace.created", "workspace.closed", "workspace.renamed", "workspace.moved",
    "workspace.reordered", "workspace.updated",
]


# --- socket API ---------------------------------------------------------------

def rpc(method, params=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(10)
        conn.connect(os.environ["HERDR_SOCKET_PATH"])
        conn.sendall(json.dumps({"id": "undo", "method": method,
                                 "params": params or {}}).encode() + b"\n")
        with conn.makefile("rb") as stream:
            line = stream.readline(64 * 1024 * 1024)
    response = json.loads(line)
    if "error" in response:
        raise RuntimeError(f"{method}: {response['error'].get('message', response['error'])}")
    return response["result"]


def session_files():
    endpoint = os.path.abspath(os.environ["HERDR_SOCKET_PATH"])
    key = hashlib.sha256(endpoint.encode()).hexdigest()[:20]
    root = Path(os.environ.get("HERDR_PLUGIN_STATE_DIR") or Path.home() / ".cache/herdr-undo") / key
    root.mkdir(parents=True, exist_ok=True)
    return root


# --- pure helpers (unit tested) -----------------------------------------------

def resume_command(pane):
    """The shell command that resumes the agent session a pane was running, or None."""
    session = pane.get("agent_session") or {}
    agent = (session.get("agent") or pane.get("agent") or "").lower()
    value = session.get("value")
    if not value:
        return None
    q = shlex.quote(value)
    if agent == "pi":
        return f"pi --session {q}"
    if agent == "claude":
        return f"claude --resume {q}"
    if agent == "codex":
        return f"codex resume {q}"
    if agent == "opencode":
        return f"opencode --session {q}"
    return None


def leaf_spec(node, panes):
    """A layout.apply leaf rebuilding a pane: directory, label, and what to resume."""
    pane = panes.get(node.get("pane_id"), {})
    spec = {"type": "pane"}
    cwd = pane.get("foreground_cwd") or pane.get("cwd") or node.get("cwd")
    if cwd:
        spec["cwd"] = cwd
    label = pane.get("label") or node.get("label")
    if label:
        spec["label"] = label
    resume = resume_command(pane)
    if resume:
        spec["resume"] = resume
    return spec


def spec_tree(node, panes):
    """The saved form of a tab: the split tree with each pane replaced by its spec,
    remembering the original pane ID (`was`) so survivors can be put back."""
    if node.get("type") == "split":
        return {"type": "split", "direction": node["direction"], "ratio": node["ratio"],
                "first": spec_tree(node["first"], panes), "second": spec_tree(node["second"], panes)}
    spec = leaf_spec(node, panes)
    if node.get("pane_id"):
        spec["was"] = node["pane_id"]
    return spec


def leaves(node):
    if node.get("type") == "split":
        return leaves(node["first"]) + leaves(node["second"])
    return [node]


def apply_tree(node, keep=frozenset()):
    """The layout.apply root for a saved tree. Leaves whose original pane still exists
    (`was` in `keep`) become placeholders that the surviving pane will replace."""
    if node.get("type") == "split":
        return {"type": "split", "direction": node["direction"], "ratio": node["ratio"],
                "first": apply_tree(node["first"], keep), "second": apply_tree(node["second"], keep)}
    out = {"type": "pane"}
    if node.get("cwd"):
        out["cwd"] = node["cwd"]
    if node.get("was") in keep:
        out["command"] = PLACEHOLDER
    elif node.get("label"):
        out["label"] = node["label"]
    return out


def capture(event, data, shadow):
    """The undo entry for a close event, from the shadow taken before it, or None."""
    panes, tabs, spaces, trees = shadow["panes"], shadow["tabs"], shadow["workspaces"], shadow["trees"]

    def tab_entry(tab_id):
        tab = tabs.get(tab_id)
        if not tab or tab_id not in trees:
            return None
        siblings = [t for t in tabs.values() if t["workspace_id"] == tab["workspace_id"]]
        siblings.sort(key=lambda t: t.get("number", 0))
        return {"kind": "tab", "workspace_id": tab["workspace_id"], "tab_id": tab_id,
                "label": tab.get("label"), "index": siblings.index(tab),
                "tree": spec_tree(trees[tab_id], panes),
                "workspace": space_entry(tab["workspace_id"], only=tab_id)}

    def space_entry(ws_id, only=None):
        ws = spaces.get(ws_id)
        if not ws:
            return None
        own = sorted((t for t in tabs.values() if t["workspace_id"] == ws_id),
                     key=lambda t: t.get("number", 0))
        order = sorted(spaces.values(), key=lambda w: w.get("number", 0))
        entry = {"kind": "workspace", "workspace_id": ws_id, "label": ws.get("label"),
                 "index": order.index(ws), "worktree": ws.get("worktree"),
                 "active": ws.get("active_tab_id")}
        if only is None:
            entry["tabs"] = [{"tab_id": t["tab_id"], "label": t.get("label"),
                              "tree": spec_tree(trees[t["tab_id"]], panes)}
                             for t in own if t["tab_id"] in trees]
        return entry

    if event == "workspace_closed":
        return space_entry(data["workspace_id"])
    if event == "tab_closed":
        return tab_entry(data["tab_id"])
    if event == "pane_closed":
        pane = panes.get(data["pane_id"])
        if not pane or pane["tab_id"] not in trees:
            return None
        tab_id = pane["tab_id"]
        if len(leaves(trees[tab_id])) <= 1:  # the tab (maybe the space) goes with it
            same_space = [t for t in tabs.values() if t["workspace_id"] == pane["workspace_id"]]
            return space_entry(pane["workspace_id"]) if len(same_space) <= 1 else tab_entry(tab_id)
        entry = tab_entry(tab_id)
        entry.update(kind="pane", pane_id=data["pane_id"])
        return entry
    return None


def covers(entry, event, data):
    """Whether an entry already restores what a later close event reports."""
    if event == "tab_closed":
        return entry.get("tab_id") == data["tab_id"] or (
            entry["kind"] == "workspace" and entry["workspace_id"] == data.get("workspace_id"))
    if event == "workspace_closed":
        return entry["kind"] == "workspace" and entry["workspace_id"] == data["workspace_id"]
    return False


def push(stack, entry, now):
    stack = [e for e in stack if now - e.get("at", now) < MAX_AGE]
    stack.append({**entry, "at": now})
    return stack[-STACK_LIMIT:]


ID_KEYS = ("workspace_id", "tab_id", "pane_id", "was")


def remap(value, mapping):
    """Rewrite old IDs to the ones a restore created, so older entries still find
    their space, tab and surviving panes."""
    if isinstance(value, list):
        return [remap(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: mapping.get(v, v) if k in ID_KEYS and isinstance(v, str) else remap(v, mapping)
                for k, v in value.items()}
    return value


def describe(entry):
    if entry["kind"] == "pane":
        spec = next((leaf for leaf in leaves(entry["tree"]) if leaf.get("was") == entry["pane_id"]), {})
        return f"pane {spec.get('label') or Path(spec.get('cwd') or '~').name}"
    if entry["kind"] == "tab":
        return f"tab {entry.get('label')}"
    return f"space {entry.get('label')}"


# --- state files ----------------------------------------------------------------

class Store:
    def __init__(self, root):
        self.root = root
        self.stack_path = root / "stack.json"
        self.suppress_path = root / "suppress.json"

    def _locked(self, fn):
        with (self.root / "stack.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return fn()

    def _read(self, path, default):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return default

    def _write(self, path, value):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value))
        tmp.replace(path)

    def push(self, entry):
        def go():
            self._write(self.stack_path, push(self._read(self.stack_path, []), entry, time.time()))
        self._locked(go)

    def top(self):
        return self._locked(lambda: (self._read(self.stack_path, []) or [None])[-1])

    def pop(self):
        def go():
            stack = [e for e in self._read(self.stack_path, []) if time.time() - e.get("at", 0) < MAX_AGE]
            entry = stack.pop() if stack else None
            self._write(self.stack_path, stack)
            return entry
        return self._locked(go)

    def remap(self, mapping):
        if mapping:
            self._locked(lambda: self._write(self.stack_path, remap(self._read(self.stack_path, []), mapping)))

    def suppress(self, ids):
        def go():
            now = time.time()
            current = {k: v for k, v in self._read(self.suppress_path, {}).items() if v > now}
            current.update({i: now + SUPPRESS_FOR for i in ids})
            self._write(self.suppress_path, current)
        self._locked(go)

    def suppressed(self, ident):
        return self._locked(lambda: self._read(self.suppress_path, {}).get(ident, 0) > time.time())


# --- daemon -------------------------------------------------------------------

def read_shadow():
    snap = rpc_snapshot()
    trees = {}
    for tab in snap["tabs"]:
        try:
            trees[tab["tab_id"]] = rpc("layout.export", {"tab_id": tab["tab_id"]})["layout"]["root"]
        except RuntimeError:
            pass  # closed meanwhile
    return {"panes": {p["pane_id"]: p for p in snap["panes"]},
            "tabs": {t["tab_id"]: t for t in snap["tabs"]},
            "workspaces": {w["workspace_id"]: w for w in snap["workspaces"]},
            "trees": trees}


def rpc_snapshot():
    return rpc("session.snapshot")["snapshot"]


def handle(event, data, shadow, store, last):
    """Record a close. `last` is (entry, monotonic time) of the previous record."""
    ident = data.get("pane_id") if event == "pane_closed" else data.get("tab_id") if event == "tab_closed" else data.get("workspace_id")
    if store.suppressed(ident):
        return last
    entry, when = last
    if entry and time.monotonic() - when < COALESCE and covers(entry, event, data):
        return last
    new = capture(event, data, shadow)
    if new:
        store.push(new)
        return new, time.monotonic()
    return last


def daemon(root):
    with (root / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        store = Store(root)
        while True:
            try:
                run(store)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                print(f"{time.strftime('%F %T')} {error}", file=sys.stderr, flush=True)
            if not Path(os.environ["HERDR_SOCKET_PATH"]).exists():
                return  # server gone
            time.sleep(2)


def run(store):
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.connect(os.environ["HERDR_SOCKET_PATH"])
    conn.sendall(json.dumps({"id": "undo-sub", "method": "events.subscribe", "params": {
        "subscriptions": [{"type": t} for t in SUBSCRIPTIONS]}}).encode() + b"\n")
    buffer = b""
    shadow = read_shadow()
    refreshed, dirty_since, last = time.monotonic(), None, (None, 0.0)
    while True:
        ready, _, _ = select.select([conn], [], [], DEBOUNCE)
        if ready:
            chunk = conn.recv(1 << 20)
            if not chunk:
                raise OSError("subscription closed")
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                message = json.loads(line)
                event = message.get("event")
                if not event:
                    continue
                if event.endswith("_closed"):
                    last = handle(event, message["data"], shadow, store, last)
                dirty_since = time.monotonic()
        now = time.monotonic()
        quiet = dirty_since is not None and now - dirty_since >= DEBOUNCE
        if quiet or now - refreshed >= REFRESH_EVERY:
            shadow = read_shadow()
            refreshed, dirty_since = now, None


# --- restore ------------------------------------------------------------------

def notify(title, body=None):
    try:
        rpc("notification.show", {"title": title, "body": body})
    except (OSError, RuntimeError):
        print(title, body or "", file=sys.stderr)


def build_tab(workspace_id, tree, label, keep=frozenset(), focus=True):
    """layout.apply a saved tree as a new tab; returns (tab_id, [(saved leaf, new pane_id)])."""
    params = {"workspace_id": workspace_id, "root": apply_tree(tree, keep), "focus": focus}
    if label:
        params["tab_label"] = label
    layout = rpc("layout.apply", params)["layout"]
    pairs = list(zip(leaves(tree), (leaf["pane_id"] for leaf in leaves(layout["root"]))))
    return layout["tab_id"], pairs


def pair_ids(pairs, keep=frozenset()):
    return {spec["was"]: new for spec, new in pairs if spec.get("was") and spec["was"] not in keep}


def resume(pairs, keep=frozenset()):
    for spec, pane_id in pairs:
        if spec.get("resume") and spec.get("was") not in keep:
            rpc("pane.send_text", {"pane_id": pane_id, "text": spec["resume"] + "\r"})


def live():
    snap = rpc_snapshot()
    return ({p["pane_id"]: p for p in snap["panes"]}, {t["tab_id"]: t for t in snap["tabs"]},
            {w["workspace_id"]: w for w in snap["workspaces"]})


def restore_space(entry, store, tabs=None):
    worktree = entry.get("worktree") or {}
    tabs = tabs if tabs is not None else entry.get("tabs") or []
    first_cwd = next((leaf.get("cwd") for t in tabs for leaf in leaves(t["tree"]) if leaf.get("cwd")), None)
    if worktree.get("is_linked_worktree") and Path(worktree.get("checkout_path", "")).is_dir():
        created = rpc("worktree.open", {"path": worktree["checkout_path"], "label": entry.get("label"), "focus": True})
    else:
        created = rpc("workspace.create", {"cwd": first_cwd, "label": entry.get("label"), "focus": True})
    ws = created.get("workspace") or created.get("workspace_info") or {}
    ws_id = ws["workspace_id"]
    default_tab = ws.get("active_tab_id") or (created.get("tab") or {}).get("tab_id")
    restored, ids = [], {}
    if entry.get("workspace_id"):
        ids[entry["workspace_id"]] = ws_id
    for tab in tabs:
        tab_id, pairs = build_tab(ws_id, tab["tree"], tab.get("label"))
        resume(pairs)
        restored.append(tab_id)
        ids.update(pair_ids(pairs))
        if tab.get("tab_id"):
            ids[tab["tab_id"]] = tab_id
    if restored and default_tab:
        store.suppress([default_tab] + [p for p, info in live()[0].items() if info["tab_id"] == default_tab])
        rpc("tab.close", {"tab_id": default_tab})
    try:
        rpc("workspace.move", {"workspace_id": ws_id, "insert_index": entry.get("index", 0)})
    except RuntimeError:
        pass
    return ids


def restore_tab(entry, store):
    panes, tabs, spaces = live()
    ws_id = entry["workspace_id"]
    if ws_id not in spaces:
        space = dict(entry.get("workspace") or {"label": None, "index": 0})
        return restore_space(space, store, tabs=[{"tab_id": entry.get("tab_id"),
                                                  "label": entry.get("label"), "tree": entry["tree"]}])
    tab_id, pairs = build_tab(ws_id, entry["tree"], entry.get("label"))
    resume(pairs)
    try:
        rpc("tab.move", {"tab_id": tab_id, "insert_index": entry.get("index", 0)})
    except RuntimeError:
        pass
    return {**pair_ids(pairs), entry["tab_id"]: tab_id}


def restore_pane(entry, store):
    panes, tabs, spaces = live()
    old_tab = entry["tab_id"]
    if old_tab not in tabs:
        return restore_tab({**entry, "kind": "tab"}, store)
    survivors = {leaf["was"] for leaf in leaves(entry["tree"])
                 if leaf.get("was") in panes and panes[leaf["was"]]["tab_id"] == old_tab
                 and leaf.get("was") != entry["pane_id"]}
    extras = [p for p, info in panes.items() if info["tab_id"] == old_tab and p not in survivors]
    tab_id, pairs = build_tab(entry["workspace_id"], entry["tree"], tabs[old_tab].get("label"), keep=survivors)
    placeholders = [new for spec, new in pairs if spec.get("was") in survivors]
    store.suppress(placeholders + [old_tab])
    target = None
    for spec, new in pairs:
        if spec.get("was") in survivors:
            rpc("pane.move", {"pane_id": spec["was"], "destination": {
                "type": "tab", "tab_id": tab_id, "split": "right", "target_pane_id": new}})
            rpc("pane.close", {"pane_id": new})
        elif spec.get("was") == entry["pane_id"]:
            target = new
    for pane_id in extras:  # panes opened in the tab after the close: keep them, at the end
        rpc("pane.move", {"pane_id": pane_id, "destination": {"type": "tab", "tab_id": tab_id, "split": "right"}})
    if old_tab in live()[1]:
        rpc("tab.close", {"tab_id": old_tab})
    resume(pairs, keep=survivors)
    try:
        rpc("tab.move", {"tab_id": tab_id, "insert_index": entry.get("index", 0)})
    except RuntimeError:
        pass
    if target:
        try:
            rpc("pane.focus", {"pane_id": target})
        except RuntimeError:
            pass
    return {**pair_ids(pairs, keep=survivors), old_tab: tab_id}


def reopen(root):
    store = Store(root)
    entry = store.pop()
    if not entry:
        notify("Nothing to reopen", "No pane, tab or space was closed in the last 24 hours.")
        return 1
    try:
        ids = {"pane": restore_pane, "tab": restore_tab, "workspace": restore_space}[entry["kind"]](entry, store)
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as error:
        store.push({k: v for k, v in entry.items() if k != "at"})
        notify(f"Could not reopen {describe(entry)}", str(error))
        return 1
    store.remap(ids)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["daemon", "reopen", "list"])
    parser.add_argument("--ensure", action="store_true", help="start the daemon in the background")
    args = parser.parse_args()
    root = session_files()
    if args.mode == "daemon" and args.ensure:
        with (root / "daemon.log").open("ab") as log:
            subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "daemon"],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True, close_fds=True)
    elif args.mode == "daemon":
        daemon(root)
    elif args.mode == "reopen":
        sys.exit(reopen(root))
    else:
        stack = Store(root)._read(Store(root).stack_path, [])
        for entry in reversed(stack):
            print(f"{time.strftime('%H:%M:%S', time.localtime(entry['at']))}  {describe(entry)}")


if __name__ == "__main__":
    main()
