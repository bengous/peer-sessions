#!/usr/bin/env python3
"""Launch named Claude Code sessions in cmux and verify their live sockets."""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time

# An installed skill is often a managed directory whose contents are hashed to
# detect tampering. A __pycache__ written on first run changes that hash and
# reads as a modified skill, so keep the import below from leaving one.
sys.dont_write_bytecode = True

from peer_registry import live_records, normalized_name, record_identity, socket_live


UUID = re.compile(r"^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$")

# Drop the parent session vars before the peer starts. A child that inherits
# CLAUDE_CODE_CHILD_SESSION never publishes its own record, so it stays
# unreachable. `unset` runs inside the shell cmux opened, which keeps the
# user's own `claude` shell function and its flags. `env -u` would skip it.
ENV_HYGIENE = "unset CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_SESSION_ID"

# cmux-gtk splits by orientation, never by direction, and always puts the new
# pane second. Its own key handler maps left/right onto a horizontal split and
# up/down onto a vertical one.
GTK_ORIENTATION = {
    "right": "horizontal",
    "left": "horizontal",
    "up": "vertical",
    "down": "vertical",
}

# A pane exists in the layout before it owns a terminal. `send-key` and
# `read-screen` both look that terminal up and give up when it is missing, so
# an early Enter is simply dropped and the typed line never runs. cmux-gtk
# builds the terminal on its GTK thread, which only runs while the window is
# on screen: a hidden window means no terminal, however long the wait.
SURFACE_READY_TIMEOUT = 45


def detect_backend():
    """Return 'gtk' or 'macos'. cmux-gtk prints a bare line; cmux prints JSON."""
    try:
        result = subprocess.run(["cmux", "identify"], capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("error: cmux is not on PATH. Install cmux (macOS) or cmux-gtk (Linux).")
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        sys.exit(f"error: cmux identify failed. Is cmux running?\n  → {output}")
    try:
        json.loads(output)
    except ValueError:
        return "gtk"
    return "macos"


def cmux(*args):
    """Run cmux and ask for UUIDs beside refs. Refs renumber; UUIDs do not."""
    result = subprocess.run(
        ["cmux", "--id-format", "both", *args], capture_output=True, text=True
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0 or output.startswith("Error"):
        raise RuntimeError(f"cmux {' '.join(args)}\n  → {output}")
    return output


def cmux_gtk(*args):
    """Run cmux-gtk in JSON mode and hand back the unwrapped `result` object."""
    result = subprocess.run(["cmux", "--json", *args], capture_output=True, text=True)
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        output = (result.stdout + result.stderr).strip()
        raise RuntimeError(f"cmux {' '.join(args)}\n  → {output}")
    if not payload.get("ok"):
        message = (payload.get("error") or {}).get("message") or result.stdout.strip()
        raise RuntimeError(f"cmux {' '.join(args)}\n  → {message}")
    return payload.get("result") or {}


def id_from(output, kind):
    """Take the durable id for `kind` out of `OK surface:22 (UUID) workspace:10 (UUID)`."""
    tokens = output.replace("(", " ").replace(")", " ").split()
    for index, token in enumerate(tokens):
        if not token.startswith(f"{kind}:"):
            continue
        if index + 1 < len(tokens) and UUID.match(tokens[index + 1]):
            return tokens[index + 1]
        return token
    raise RuntimeError(f"no {kind} id in cmux output: {output!r}")


def workspace_uuid(ref):
    """`new-workspace` prints a ref only. Refs renumber, so trade it for a UUID."""
    if UUID.match(ref):
        return ref
    # `list-workspaces` covers the caller's window only, so read every window.
    match = re.search(
        rf"workspace {re.escape(ref)}(?![0-9])\s+([0-9A-Fa-f-]{{36}})",
        cmux("tree", "--all"),
    )
    return match.group(1) if match else ref


def caller_context():
    """Return the window, workspace, and surface UUIDs that this script runs in."""
    try:
        payload = json.loads(cmux("identify"))
    except (RuntimeError, ValueError):
        return {}
    caller = payload.get("caller") or {}
    return {
        "window": caller.get("window_id"),
        "workspace": caller.get("workspace_id"),
        "surface": caller.get("surface_id"),
    }


def caller_context_gtk():
    """Stand in for the caller with the focus.

    cmux-gtk carries no caller identity, so this reads the selected workspace
    and its focused pane instead. That matches the caller whenever the caller
    is the pane the user last touched, and misses when it is not.
    """
    try:
        current = cmux_gtk("workspace", "current")
    except RuntimeError:
        return {}
    return {
        "window": None,
        "workspace": current.get("id"),
        "surface": current.get("focused_panel_id"),
    }


def workspace_index_gtk(workspace):
    """Trade a workspace UUID for the index `workspace select` wants.

    Indexes shift every time a workspace closes, so resolve one the moment it
    is used and never keep it.
    """
    for entry in cmux_gtk("tree").get("workspaces", []):
        if entry.get("id") == workspace:
            return entry["index"]
    raise RuntimeError(f"no workspace {workspace} in the cmux tree")


def select_workspace_gtk(workspace):
    cmux_gtk("workspace", "select", str(workspace_index_gtk(workspace)))


def gtk_orientation(direction, count):
    """Map a direction onto a cmux-gtk split orientation, and warn about the fit."""
    if direction in ("left", "up"):
        landing = "right" if direction == "left" else "down"
        print(
            f"note: cmux-gtk always puts the new pane second, so "
            f"--direction {direction} lands like --direction {landing}.",
            file=sys.stderr,
        )
    if count > 3 and direction in ("right", "left"):
        print(
            f"note: {count} side panes get narrow. "
            "Use --direction down, or --placement workspace.",
            file=sys.stderr,
        )
    return GTK_ORIENTATION[direction]


def wait_for_surface_gtk(surface):
    """Block until the pane answers, so the launch command is not typed too early."""
    deadline = time.time() + SURFACE_READY_TIMEOUT
    while time.time() < deadline:
        try:
            cmux_gtk("surface", "read-screen", "--surface", surface)
            return
        except RuntimeError:
            time.sleep(0.5)
    raise RuntimeError(
        f"pane {surface} still had no terminal after {SURFACE_READY_TIMEOUT}s.\n"
        "  cmux-gtk builds a pane's terminal only while its window is on "
        "screen.\n"
        "  Put the cmux window on the visible desktop, then run this again."
    )


def split_gtk(workspace, anchor, orientation):
    """Split `anchor` and return the new pane.

    `surface split` acts on the focused pane of the *selected* workspace, and
    `surface focus` moves the focus without selecting anything. Pin both.
    """
    select_workspace_gtk(workspace)
    cmux_gtk("surface", "focus", anchor)
    return cmux_gtk("surface", "split", "--orientation", orientation)["panel_id"]


def parse_specs(raw):
    specs = []
    for item in raw:
        if ":" not in item:
            sys.exit(f"error: '{item}' is not NAME:DIR")
        name, _, directory = item.partition(":")
        name = name.strip()
        directory = os.path.abspath(os.path.expanduser(directory.strip()))
        if not name:
            sys.exit(f"error: '{item}' has an empty name")
        os.makedirs(directory, exist_ok=True)
        specs.append((name, directory))
    names = [name for name, _ in specs]
    duplicates = {name for name in names if names.count(name) > 1}
    if duplicates:
        sys.exit(f"error: duplicate session names: {', '.join(sorted(duplicates))}")
    return specs


def new_matching_records(before, wanted):
    return [
        record
        for record in live_records()
        if record_identity(record) not in before and normalized_name(record) in wanted
    ]


def ready_by_name(records):
    ready = {}
    for record in records:
        socket_path = record.get("messagingSocketPath")
        if not socket_live(socket_path):
            continue
        name = normalized_name(record)
        previous = ready.get(name)
        if previous is None or (record.get("startedAt") or 0) > (
            previous.get("startedAt") or 0
        ):
            ready[name] = record
    return ready


def cleanup_header(records):
    pids = sorted(
        {record.get("pid") for record in records if isinstance(record.get("pid"), int)}
    )
    print("\nTeardown is off by default: hand this block to the user, and run it")
    print("only when they ask. Kill the processes before closing their UI:")
    if pids:
        print("  kill " + " ".join(str(pid) for pid in pids))
    else:
        print("  No launched PID registered yet; inspect the panes before closing them.")


def print_cleanup_gtk(records, layout):
    """Close cmux-gtk panes by UUID.

    `workspace close` takes an index, and every close renumbers the rest, so a
    printed block of indexes goes stale between its own lines. Pane UUIDs do
    not move, and a workspace drops once its last pane closes.
    """
    cleanup_header(records)
    for surface in layout["surfaces"]:
        print(f"  cmux surface close {surface}")
    print("  python3 ~/.claude/skills/peer-sessions/scripts/peer-addr.py")


def print_cleanup(records, layout):
    cleanup_header(records)
    if layout["placement"] == "window":
        print(f"  cmux close-window --window {layout['window']}")
        print("  # close-window can return OK and leave the window open.")
        print("  # If it stays in cmux list-windows, ask the user for a Cmd+W.")
    elif layout["placement"] == "workspace":
        for workspace in layout["workspaces"]:
            print(f"  cmux close-workspace --workspace {workspace}")
    else:
        # Split placement lives in the caller's own workspace. Close the new
        # panes only. Never close this workspace or this window.
        for surface in layout["surfaces"]:
            print(
                f"  cmux close-surface --workspace {layout['workspace']} "
                f"--surface {surface}"
            )
    print("  python3 ~/.claude/skills/peer-sessions/scripts/peer-addr.py")


def launch_line(directory, command):
    return f"{ENV_HYGIENE}; cd {shlex.quote(directory)} && {command}"


def start_session(workspace, surface, directory, command):
    """Type a launch command into a shell that cmux already opened."""
    full = launch_line(directory, command)
    cmux("send", "--workspace", workspace, "--surface", surface, full)
    cmux("send-key", "--workspace", workspace, "--surface", surface, "Enter")


def start_session_gtk(surface, directory, command):
    """Type a launch command into a pane, once that pane can take one."""
    try:
        wait_for_surface_gtk(surface)
    except RuntimeError:
        # A pane that never grew a terminal is dead weight, and every retry
        # would leave one more behind. Take it back out.
        try:
            cmux_gtk("surface", "close", surface)
        except RuntimeError:
            pass
        raise
    cmux_gtk("surface", "send-text", "--surface", surface, launch_line(directory, command))
    cmux_gtk("surface", "send-key", "Return", "--surface", surface)


def build_splits(specs, launch_cmd, caller, direction, focus):
    """Put each session in a new pane beside the caller's pane."""
    workspace = caller.get("workspace")
    anchor = caller.get("surface")
    if not workspace or not anchor:
        sys.exit(
            "error: --placement split needs a cmux terminal. "
            "Run it inside cmux, or use --placement workspace|window."
        )
    if len(specs) > 3 and direction in ("right", "left"):
        print(
            f"note: {len(specs)} side panes get narrow. "
            "Use --direction down, or --placement workspace.",
            file=sys.stderr,
        )
    surfaces = []
    for name, directory in specs:
        output = cmux(
            "new-split",
            direction,
            "--workspace",
            workspace,
            "--surface",
            anchor,
            "--focus",
            focus,
        )
        surface = id_from(output, "surface")
        surfaces.append(surface)
        start_session(workspace, surface, directory, launch_cmd(name))
        anchor = surface  # chain, so the panes line up in spec order
    return {
        "placement": "split",
        "window": caller.get("window"),
        "workspace": workspace,
        "workspaces": [],
        "surfaces": surfaces,
    }


def build_splits_gtk(specs, launch_cmd, caller, direction, focus):
    """Put each session in a new pane beside the caller's pane."""
    workspace = caller.get("workspace")
    anchor = caller.get("surface")
    if not workspace or not anchor:
        sys.exit(
            "error: --placement split needs a cmux terminal. "
            "Run it inside cmux, or use --placement workspace."
        )
    orientation = gtk_orientation(direction, len(specs))
    surfaces = []
    for name, directory in specs:
        surface = split_gtk(workspace, anchor, orientation)
        surfaces.append(surface)
        start_session_gtk(surface, directory, launch_cmd(name))
        anchor = surface  # chain, so the panes line up in spec order
    # Splitting leaves the focus on the anchor, and the loop moved that anchor
    # down the chain. Put the focus where --focus asked for it.
    cmux_gtk("surface", "focus", surfaces[-1] if focus == "true" else caller["surface"])
    return {
        "placement": "split",
        "window": None,
        "workspace": workspace,
        "workspaces": [],
        "surfaces": surfaces,
    }


def build_workspaces_gtk(
    specs, launch_cmd, window, prefix, per_workspace, direction, focus
):
    """Put each group of sessions in its own workspace.

    `window` and `focus` are ignored: cmux-gtk has no windows, and it always
    selects the workspace it creates, so main() restores the focus afterwards.
    Both stay in the signature so main() can pick a builder without reshaping
    the call.
    """
    workspaces = []
    surfaces = []
    orientation = gtk_orientation(direction, per_workspace)
    groups = [
        specs[index : index + per_workspace]
        for index in range(0, len(specs), per_workspace)
    ]
    for group_index, group in enumerate(groups, 1):
        first_name, first_dir = group[0]
        # `workspace new` takes no command, so the opening pane gets typed into
        # like every other one. It also selects the workspace it just made.
        workspace = cmux_gtk(
            "workspace",
            "new",
            "--directory",
            first_dir,
            "--title",
            f"{prefix}-{group_index}",
        )["workspace_id"]
        workspaces.append(workspace)
        panels = cmux_gtk("surface", "list", "--workspace", workspace).get("panels")
        if not panels:
            raise RuntimeError(f"workspace {workspace} opened with no pane")
        anchor = panels[0]["id"]
        surfaces.append(anchor)
        start_session_gtk(anchor, first_dir, launch_cmd(first_name))

        for name, directory in group[1:]:
            surface = split_gtk(workspace, anchor, orientation)
            surfaces.append(surface)
            start_session_gtk(surface, directory, launch_cmd(name))
            anchor = surface
    return {"window": None, "workspaces": workspaces, "surfaces": surfaces}


def build_workspaces(specs, launch_cmd, window, prefix, per_workspace, direction, focus):
    """Put each group of sessions in its own workspace."""
    workspaces = []
    surfaces = []
    groups = [
        specs[index : index + per_workspace]
        for index in range(0, len(specs), per_workspace)
    ]
    for group_index, group in enumerate(groups, 1):
        first_name, first_dir = group[0]
        cmux_args = [
            "new-workspace",
            "--name",
            f"{prefix}-{group_index}",
            "--cwd",
            first_dir,
            "--command",
            f"{ENV_HYGIENE}; {launch_cmd(first_name)}",
        ]
        if window:
            cmux_args += ["--window", window]
        cmux_args += ["--focus", focus if group_index == 1 else "false"]
        workspace = workspace_uuid(id_from(cmux(*cmux_args), "workspace"))
        workspaces.append(workspace)

        for name, directory in group[1:]:
            output = cmux(
                "new-split", direction, "--workspace", workspace, "--focus", "false"
            )
            surface = id_from(output, "surface")
            surfaces.append(surface)
            start_session(workspace, surface, directory, launch_cmd(name))
    return {"window": window, "workspaces": workspaces, "surfaces": surfaces}


def main():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("specs", nargs="+", metavar="NAME:DIR")
    parser.add_argument(
        "--placement",
        choices=("split", "workspace", "window"),
        default="workspace",
        help="split: panes beside this one. workspace: new workspaces in this "
        "window. window: a new window (default: workspace)",
    )
    parser.add_argument(
        "--window", action="store_true", help="alias for --placement window"
    )
    parser.add_argument(
        "--direction",
        choices=("right", "left", "up", "down"),
        default="right",
        help="direction of each new pane (default: right)",
    )
    parser.add_argument(
        "--focus",
        choices=("true", "false"),
        help="move the UI to the fleet (default: false for split, true otherwise)",
    )
    parser.add_argument("--per-workspace", type=int, default=2, choices=(1, 2))
    parser.add_argument("--prefix", default="fleet")
    parser.add_argument("--model")
    parser.add_argument("--claude-arg", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument(
        "--permission-mode",
        default="auto",
        help="mode for every peer, or 'bypass' for "
        "--dangerously-skip-permissions (default: auto). Match the class you "
        "send from, or the peers hold your messages",
    )
    args = parser.parse_args()

    placement = "window" if args.window else args.placement
    focus = args.focus or ("false" if placement == "split" else "true")

    backend = detect_backend()
    if backend == "gtk" and placement == "window":
        sys.exit(
            "error: cmux-gtk has no windows. Use --placement workspace, or "
            "--placement split from inside a cmux pane."
        )

    specs = parse_specs(args.specs)
    before = {record_identity(record) for record in live_records()}
    wanted = {name for name, _ in specs}

    extra = list(args.claude_arg)
    if args.model:
        extra += ["--model", args.model]

    def launch_cmd(name):
        # A peer holds messages from a sender in a different permission class,
        # so `bypass` has to reach the peer as the flag, not as a mode name.
        if args.permission_mode == "bypass":
            mode = ["--dangerously-skip-permissions"]
        else:
            mode = ["--permission-mode", args.permission_mode]
        return " ".join(
            shlex.quote(value) for value in ["claude", "--name", name, *mode, *extra]
        )

    caller = caller_context_gtk() if backend == "gtk" else caller_context()

    if placement == "split":
        build = build_splits_gtk if backend == "gtk" else build_splits
        layout = build(specs, launch_cmd, caller, args.direction, focus)
        print(f"workspace {layout['workspace']} (this one)")
        print(f"panes {', '.join(layout['surfaces'])}")
    else:
        if placement == "window":
            window = cmux("new-window").split()[-1]
            print(f"window {window} (new)")
        elif backend == "gtk":
            window = None
        else:
            window = caller.get("window")
            print(f"window {window or '(focused)'} (this one)")
        build = build_workspaces_gtk if backend == "gtk" else build_workspaces
        layout = build(
            specs,
            launch_cmd,
            window,
            args.prefix,
            args.per_workspace,
            args.direction,
            focus,
        )
        layout["placement"] = placement
        print(f"workspaces {', '.join(layout['workspaces'])}")

    started = time.time()
    records = []
    ready = {}
    while time.time() - started < args.timeout:
        records = new_matching_records(before, wanted)
        ready = ready_by_name(records)
        if len(ready) == len(wanted):
            break
        time.sleep(2)

    elapsed = int(time.time() - started)
    records = new_matching_records(before, wanted)
    ready = ready_by_name(records)
    cleanup = print_cleanup_gtk if backend == "gtk" else print_cleanup

    if focus == "true" and layout["workspaces"]:
        # A new window opens on its own default workspace, so `--focus true` on
        # the first workspace does not stick. Select it again here, after every
        # session has booted. An earlier switch races the launch command that
        # cmux types into the new shell, and scrambles it.
        if backend == "gtk":
            select_workspace_gtk(layout["workspaces"][0])
        else:
            cmux("select-workspace", "--workspace", layout["workspaces"][0])
    elif backend == "gtk" and layout["workspaces"] and caller.get("workspace"):
        # `workspace new` always selects what it creates, so cmux-gtk needs the
        # caller's workspace put back by hand to honour `--focus false`.
        select_workspace_gtk(caller["workspace"])
    for name, _ in specs:
        if name in ready:
            record = ready[name]
            print(
                f"+ {name:<24} pid {record['pid']:<7} "
                f"uds:{record['messagingSocketPath']}"
            )
        else:
            print(f"- {name:<24} did not register a live socket in {args.timeout}s")

    missing = [name for name, _ in specs if name not in ready]
    if missing:
        print(
            f"\n{len(ready)}/{len(wanted)} ready after {elapsed}s. "
            "Check the pane — a stalled permission prompt never registers.",
            file=sys.stderr,
        )
        cleanup(records, layout)
        return 1

    print(f"\n{len(ready)} ready in {elapsed}s.")
    print("Send with a uds: address above, or a freshly resolved ListAgents name/ref.")
    cleanup(records, layout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
