# Manual rigging, forks, and paths that the skill does not cover

Read this when `spawn-fleet.py` does not fit: unusual layouts, filming, forks, or a `claude`/`cmux` capability that the skill does not name.

Two cmux builds exist, and their CLIs differ. Every command in this file up to "The same rig on Linux" is **cmux on macOS**. For cmux-gtk on Linux, read that section instead — the grammar there is not a dialect of this one. To tell them apart, run `cmux identify`: cmux-gtk answers a bare `cmux linux v…`, macOS answers JSON.

## Launch hygiene

Three things stop a hand-launched peer before it ever registers. All three apply to both builds:

- **Inherited session vars.** Prefix every launch with `unset CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_SESSION_ID`. A peer that inherits them publishes no record, so `peer-addr.py` and `ListAgents` never see it.
- **`env -u` instead of `unset`.** `env -u VAR claude …` runs the `claude` *binary* and skips the user's shell function of the same name, which is often where their standing flags live. Type `unset` into the shell that cmux opened, and let `claude` resolve normally.
- **An untrusted folder.** A session started in a folder the user has not approved stops on a trust prompt and never boots. Launch in a directory they already use.

## The cmux object model

A **window** holds workspaces. A workspace holds panes. A pane holds a **surface** (the terminal that you type into). Workspaces and surfaces use short refs (`workspace:26`, `surface:93`). Windows use full UUIDs.

**A ref is an index, not an identity.** Close one surface and the rest renumber, so a ref that you saved a minute ago can now point at another pane. Ask for UUIDs and hold those instead:

```bash
cmux --id-format both new-split right --workspace W --surface S --focus false
# → OK surface:22 (BFB087B6-...) workspace:10 (CCA4E168-...)
```

`--id-format` goes before the subcommand. It takes `refs`, `uuids`, or `both`. Two commands ignore it: `new-workspace` prints a ref only (trade it for a UUID with `cmux --id-format both tree --all`), and `new-window` prints a UUID only.

## Where am I?

```bash
cmux --id-format both identify
```

`caller` is the pane that ran the command. `focused` is the pane the user looks at. They differ often, so build from `caller`:

```json
"caller": { "window_id": "888C66E8-...", "workspace_id": "CCA4E168-...",
            "surface_id": "C087AFB0-...", "pane_ref": "pane:11" }
```

This is how `spawn-fleet.py --placement split` finds the pane to split, and how `--placement workspace` finds the window to build in.

## A session beside your own pane

This is what `--placement split` does. Split your own surface, then chain each next split off the pane you just made, so the panes line up in order:

```bash
cmux --id-format both new-split right \
  --workspace <caller workspace_id> --surface <caller surface_id> --focus false
# → OK surface:22 (BFB087B6-...) workspace:10 (CCA4E168-...)

cmux send     --workspace <ws> --surface <new surface> "unset CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_SESSION_ID; cd /tmp/lab && claude --name alpha --permission-mode auto"
cmux send-key --workspace <ws> --surface <new surface> Enter
```

`--focus false` matters. Without it the UI jumps to the new pane, and the user's next keystrokes go to the peer.

Teardown stays off by default here too (SKILL.md section 4). Once the user asks: `cmux close-surface --workspace <ws> --surface <uuid>`, one call per pane, after you kill the PIDs. Never `close-workspace` — that workspace is the user's.

## One workspace, two sessions

```bash
cmux new-workspace --name peer-demo --cwd <dir> \
  --command "unset CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_SESSION_ID; claude --name alpha --permission-mode auto" --focus true
# → OK workspace:26

cmux new-split right --workspace workspace:26 --focus true
# → OK surface:93 workspace:26
cmux send     --workspace workspace:26 --surface surface:93 "unset CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_SESSION_ID; claude --name beta --permission-mode auto"
cmux send-key --workspace workspace:26 --surface surface:93 Enter
```

`new-split` gives you a shell, not a session. You start the second session when you type into that shell. `new-split` prints the new surface id. You need `cmux tree --workspace W` only for the first pane's surface, because cmux never prints that surface back.

## A separate window

```bash
cmux new-window
# → OK 013D22DA-F68D-43E8-A7CB-F6B7E7251336

cmux new-workspace --name space-lab --cwd <dir> \
  --window 013D22DA-F68D-43E8-A7CB-F6B7E7251336 \
  --command "unset CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_SESSION_ID; claude --name gamma --permission-mode auto" --focus true
```

Then split, send, and send-key as above. `cmux list-windows` shows the windows that exist.

## Watch the panes

```bash
cmux read-screen --workspace W --surface S --lines 30
```

To wait for a session, poll for the spinner. A session that works renders `Word… (12s`. An idle session does not contain `… (`. Check for a stalled permission prompt in the same loop. "The session waits for a human" and "the session is done" look the same on screen:

```bash
screen=$(cmux read-screen --workspace W --surface S --lines 40)
grep -qF '… ('                     <<< "$screen"   # still working
grep -qF 'Do you want to proceed?' <<< "$screen"   # stalled — needs an answer
```

Give the poll a large time ceiling. Research plus writing runs for several minutes. Poll only when you need eyes on the screen. If you asked the peer to reply, end your turn instead.

## The same rig on Linux (cmux-gtk)

cmux-gtk speaks a different CLI. `spawn-fleet.py` detects it and adapts; drive it by hand with the commands here. Global `--json` wraps every answer as `{"ok": true, "result": …}` and hands you UUIDs, so none of the macOS `--id-format` machinery applies.

**There are no windows.** Workspaces are the top level. `--placement window` has no equivalent, and the RPC layer's `window.*` methods are not exposed on the CLI.

Three shapes to keep in mind, all measured on v0.62.0-alpha.11:

- **A pane grows its terminal only while the cmux window is on screen.** On a hidden desktop the pane exists in the layout with nothing behind it. `send-key` and `read-screen` both look that terminal up and quietly do nothing when it is missing, so an early Enter is dropped and the typed line never runs. Wait for `read-screen` to answer before you type.
- **`surface split` splits the focused pane of the *selected* workspace.** `surface focus` moves the focus without selecting anything, so pin the workspace *and* the pane before every split.
- **Indexes renumber on every close.** `workspace select` and `workspace close` take an index, so resolve one from `tree` at the moment you use it and never save it. Pane UUIDs are stable; close by UUID.

```bash
cmux identify                                  # bare line ⇒ this is cmux-gtk
cmux --json workspace current                  # → id, index, focused_panel_id
cmux --json tree                               # → every workspace with its index

# one workspace, two sessions
WS=$(cmux --json workspace new --directory /tmp/lab --title peer-demo \
     | jq -r .result.workspace_id)             # this also selects the new workspace
A=$(cmux --json surface list --workspace $WS | jq -r .result.panels[0].id)

LAUNCH='unset CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_SESSION_ID; cd /tmp/lab && claude --name alpha'
until cmux --json surface read-screen --surface $A >/dev/null 2>&1; do sleep 0.5; done
cmux --json surface send-text --surface $A "$LAUNCH"
cmux --json surface send-key Return --surface $A          # the key is Return, not Enter

cmux --json workspace select "$(cmux --json tree | jq -r \
  ".result.workspaces[] | select(.id==\"$WS\") | .index")"
cmux --json surface focus $A
B=$(cmux --json surface split --orientation vertical | jq -r .result.panel_id)
```

`--orientation horizontal` puts the new pane to the right, `vertical` puts it below. cmux-gtk always places the new pane second, so there is no way to land left or up.

Teardown, once the user asks, after you kill the PIDs:

```bash
cmux surface close <pane-uuid>     # positional, one call per pane
```

A workspace disappears on its own once its last pane closes, so closing the panes is the whole job. Read a pane with `cmux --json surface read-screen --surface S`; it has no `--lines` flag and returns the visible screen as one `result.text` string.

## Fork a session

`--fork-session` starts a peer from the history of an existing conversation, under a **new** session id:

```bash
claude --resume <sessionId> --fork-session --name reviewer-a --permission-mode auto
```

The fork starts with the full transcript and a fresh id. The original session continues, and you can address both. Without `--fork-session`, `--resume` uses the same id and the sessions collide.

Session id vs socket: the socket (`uds:...`) is the address of a running *process*. The session id is the address of a *conversation on disk*. Get your own id from `peer-addr.py --me`. Get another session's id from `sessionId` in `~/.claude/sessions/<pid>.json`.

To fork one session N ways is better than to write the same long brief N times.

## When you need something that this file does not cover

Read the help. Do not guess. Do not assume a thing is impossible:

```bash
COLUMNS=200 claude --help    # a pipe through head kills it with SIGPIPE;
claude agents --help         # narrow terminals wrap it into noise
cmux --help
```

Fleet flags that come up often: `--add-dir` (a second writable directory), `--model` / `--effort` (cheap workers beside one expensive reviewer), `--agent` / `--append-system-prompt` (a standing role, so you do not repeat it in each brief), `--worktree` (own git worktree per session when the fleet edits one repo). Check the spelling of each flag in the help before you use it. Flags change between versions.

cmux also has more commands than the ones above: `respawn-pane`, `swap-pane`, `find-window`, `pipe-pane`, `notify`, `set-progress`.
