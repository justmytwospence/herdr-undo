# herdr-undo

A Python-standard-library Herdr plugin that reopens the last closed **pane, tab or
space**: the same split tree and ratios, directories and labels, in the same place,
with agent sessions resumed. Repeat it to walk further back.

It is not a true undo. Closing a pane kills its processes, so what comes back is a
fresh shell in the same directory: scrollback, shell history, running jobs and
unsaved state are gone. An agent pane comes back running its resume command:

| Agent | Resumed with |
| --- | --- |
| pi | `pi --session <session file>` |
| Claude Code | `claude --resume <id>` |
| Codex | `codex resume <id>` |
| opencode | `opencode --session <id>` |

Other agents, and agents that never reported a session to Herdr, come back as a
plain shell.

## How it works

Herdr's close events carry only IDs, so a daemon (started by the plugin's startup
hook, one per server) keeps a shadow of every space, tab and `layout.export` tree,
refreshed after each structural event and every few seconds. On `pane_closed`,
`tab_closed` or `workspace_closed` it saves the pre-close state to an undo stack:
20 entries, kept 24 hours, in the plugin state directory (it survives a server
restart). Closing a tab's last pane records the tab, and a space's last pane the
space, once.

The `reopen` action pops the stack:

- **Pane:** rebuilds the tab from its saved tree in a new tab, moves every
  surviving pane (same terminal, still running) into its old slot, closes the old
  tab, and puts the new one where the old one was. Panes opened in the tab after
  the close are kept, at the end.
- **Tab:** rebuilds it in its space at its old position (or in a new space if that
  one is gone too).
- **Space:** a new space with its label, each tab in order, at its old position. A
  linked worktree whose checkout still exists reopens as a worktree space.

Restoring a space or tab rewrites the IDs in older entries, so undoing further
back lands in the restored space instead of creating another. With nothing to
reopen, or on failure, it shows a toast and keeps the entry.

## Install

```sh
herdr plugin install justmytwospence/herdr-undo --ref <commit> --yes
```

Then bind the action, for example in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+z"
type = "plugin_action"
command = "herdr-undo.reopen"
description = "reopen the last closed pane, tab or space"
```

(`prefix+z` is Herdr's default `zoom`; rebind that elsewhere.) Installing does not
run the startup hook: restart the server, or run
`python3 -B undo.py daemon --ensure` from the plugin root inside Herdr.
`python3 -B undo.py list` shows the stack.

## Limits

- Only closes made while the daemon was running can be undone.
- A label changed in the last three seconds before a close may be missed (Herdr
  emits no event for pane renames).
- Plugin-owned panes (such as a file viewer) come back as shells.

## Tests

```sh
python3 -B -m unittest discover -s tests
```
