# Placement: flags, edge cases, teardown syntax

Read this when you choose a `spawn-fleet.py` placement, need a non-default flag, or write the teardown by hand.

## All spawn flags

- `--placement split|workspace|window` — where the fleet appears (default `workspace`)
- `--window` — alias for `--placement window`
- `--direction right|left|up|down` — where each new pane goes (default `right`)
- `--focus true|false` — move the UI to the fleet (default `false` for `split`, `true` otherwise)
- `--per-workspace 1|2` — sessions per workspace (default 2)
- `--prefix <name>` — workspace title prefix (default `fleet`)
- `--model <model>` — model for every session
- `--claude-arg <flag>` — repeatable escape hatch, passed to `claude` verbatim
- `--timeout <seconds>` — how long to wait for live sockets (default 120)
- `--permission-mode <mode>` — default `auto`, or `bypass` for `--dangerously-skip-permissions`; match the class you send from (SKILL.md section 1)

## Placement facts

- `split` needs a cmux terminal, because it splits **your** pane (found via `cmux identify`). The script exits with an error outside cmux. Its teardown closes the new panes only. It never closes your workspace.
- `split` keeps `--focus false` so the user's keystrokes stay in their own pane.
- More than 3 side panes get too narrow to read. Use `--direction down`, or `--placement workspace`.
- `--placement window` opens the window with an extra empty shell workspace, and `cmux close-window` can return `OK` and leave the window open. Expect to ask the user for a Cmd+W. `split` and `workspace` tear down cleanly, so prefer them.

## On Linux (cmux-gtk)

- `--placement window` does not exist. The script stops before it creates anything and tells you to use `workspace` or `split`.
- `--direction left` lands like `right`, and `up` lands like `down`. cmux-gtk splits by orientation and always puts the new pane second. The script prints a note when you ask for a direction it cannot honour.
- `split` has no caller identity to read, so it uses the selected workspace and its focused pane instead. That is the caller whenever the caller is the pane the user last touched. Check the split landed where you meant.
- The cmux window must be on a visible desktop for the whole spawn. cmux-gtk builds a pane's terminal only while its window renders, so a spawn started behind a hidden window fails with a message that says so, and takes its own empty pane back out.
- Teardown closes panes by UUID, and a workspace disappears once its last pane goes. There is nothing extra to close.

## Teardown syntax per placement

Teardown is off by default (SKILL.md section 4). These are the commands you hand the user, and the commands you run once the user asks.

The spawn script prints the exact teardown with real UUIDs. When you must write it by hand:

```bash
kill <pid> <pid>                                    # always first; pids from peer-addr.py

cmux close-surface --workspace <uuid> --surface <uuid>   # placement split, one per pane
cmux close-workspace --workspace <uuid>                  # placement workspace
cmux close-window --window <uuid>                        # placement window

python3 ~/.claude/skills/peer-sessions/scripts/peer-addr.py   # confirm gone
```

On cmux-gtk the three close commands collapse into one, and it takes the UUID positionally:

```bash
kill <pid> <pid>
cmux surface close <pane-uuid>                       # one per pane, any placement
python3 ~/.claude/skills/peer-sessions/scripts/peer-addr.py
```

Use UUIDs, not refs. Refs are indexes: each close renumbers the rest, so a saved ref can point at another pane. Get UUIDs with `cmux --id-format both` (details in `manual-rig.md`).

A `split` fleet sits in the user's own workspace, so `close-workspace` there closes the user's work too. Close only what you made.
