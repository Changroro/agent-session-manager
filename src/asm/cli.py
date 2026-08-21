"""Headless CLI subcommands for asm.

Bare ``asm`` opens the TUI; these subcommands expose the same service layer
for scripts and agents. Destructive commands confirm first (skip with --yes)
and go through the same trash/recovery-snapshot paths as the TUI.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from asm.utils import TOP_PROJECT_CHART_ROWS, format_bytes

_TITLE_WIDTH = 60


# ── helpers ─────────────────────────────────────────────────────────────


def _out_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _fmt_ts(ts: float) -> str:
    if not ts:
        return "?"
    if ts > 1e12:
        ts /= 1000
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _role(m: dict) -> str:
    return m.get("type") or m.get("role") or "?"


def _find_claude_project_dir(session_id: str) -> str | None:
    """Encoded project dir name owning this session's JSONL, if any."""
    from asm.models import PROJECTS_DIR

    if PROJECTS_DIR.exists():
        for d in PROJECTS_DIR.iterdir():
            if d.is_dir() and (d / f"{session_id}.jsonl").exists():
                return d.name
    return None


def _print_table(title: str | None, columns: list[str], rows: list[tuple], justify: tuple = ()) -> None:
    """Render a rich table (used when stdout is a terminal).

    Plain-str cells are wrapped in Text so user data (session titles, paths)
    is never parsed as rich markup; pass a Text object for styled cells.
    """
    from rich import box
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    table = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold cyan", title_justify="left")
    for i, name in enumerate(columns):
        table.add_column(name, justify=justify[i] if i < len(justify) else "left", overflow="fold")
    for row in rows:
        table.add_row(*(c if isinstance(c, Text) else Text(str(c)) for c in row))
    Console().print(table)


def _src_cell(src: str):
    from rich.text import Text

    # Same source colors as the TUI (Claude orange / Codex teal).
    return Text("C", style="#D97757") if src == "claude" else Text("X", style="#10A37F")


def _sources(source: str) -> list[str]:
    from asm.services import codex_data

    srcs = []
    if source in ("all", "claude"):
        srcs.append("claude")
    if source in ("all", "codex") and codex_data.is_available():
        srcs.append("codex")
    return srcs


def _tagged_sessions(source: str, project: str | None) -> list[tuple]:
    """(SessionDetail, source, project_path) for the requested scope."""
    from asm.services import claude_data, codex_data

    rows: list[tuple] = []
    if "claude" in _sources(source):
        paths = [project] if project else [p.path for p in claude_data.get_projects()]
        for path in paths:
            for s in claude_data.get_project_sessions(path):
                rows.append((s, "claude", path))
    if "codex" in _sources(source):
        cwds = [project] if project else [p.path for p in codex_data.get_projects()]
        for cwd in cwds:
            for s in codex_data.get_project_sessions(cwd):
                rows.append((s, "codex", cwd))
    rows.sort(key=lambda r: r[0].last_modified, reverse=True)
    return rows


# ── cost ────────────────────────────────────────────────────────────────


def _prewarm_ledger() -> None:
    """Run the incremental ledger update with stderr progress (first backfill
    over a large ~/.codex takes minutes and must not look like a hang)."""
    from asm.services import ledger

    show = sys.stderr.isatty()

    def _progress(source: str, done: int, total: int) -> None:
        if show:
            print(f"\r  indexing {source} history {done:,}/{total:,} (one-time)",
                  end="", file=sys.stderr, flush=True)

    changed = ledger.update_claude(_progress) + ledger.update_codex(_progress)
    if show and changed:
        print(file=sys.stderr)


def cmd_cost(args) -> int:
    from asm.services import claude_data, codex_data, pricing

    rates_note = pricing.load_live_rates()
    _prewarm_ledger()
    mods = {"claude": claude_data, "codex": codex_data}
    result = {}
    for src in _sources(args.source):
        mod = mods[src]
        usage = mod.get_usage_data()
        periods = mod.get_period_usage(args.period)
        result[src] = {
            "total_cost": usage["total_cost"],
            "first_use": usage.get("first_use", ""),
            "model_totals": usage["model_totals"],
            "top_projects": usage["project_costs"][:TOP_PROJECT_CHART_ROWS],
            args.period: periods[: args.limit] if args.limit else periods,
        }

    if args.json:
        _out_json({"rates_source": rates_note, **result})
        return 0

    if sys.stdout.isatty():
        from rich.console import Console

        console = Console()
        console.print(f"[dim]rates: {rates_note}[/]")
        num = ("left", "right", "right", "right", "right")
        for src, data in result.items():
            since = f"  [dim]since {data['first_use'][:10]}[/]" if data["first_use"] else ""
            console.print(f"\n[bold]{src}[/] — [bold green]${data['total_cost']:,.2f}[/]{since}")
            _print_table(
                None, ["model", "cost", "input", "output"],
                [(m, f"${mt['costUSD']:,.2f}", f"{mt['inputTokens']:,}", f"{mt['outputTokens']:,}")
                 for m, mt in sorted(data["model_totals"].items(), key=lambda x: -x[1]["costUSD"])],
                justify=num,
            )
            if codex_data.UNKNOWN_MODEL in data["model_totals"]:
                console.print(
                    f"[dim]{codex_data.UNKNOWN_MODEL} = session file records no model id; "
                    f"priced at the default GPT tier[/]"
                )
            _print_table(
                args.period, ["period", "cost", "input", "output", "msgs"],
                [(r["period"], f"${r['total_cost']:,.2f}", f"{r['total_input']:,}",
                  f"{r['total_output']:,}", f"{r['total_messages']:,}") for r in data[args.period]],
                justify=num,
            )
        return 0

    print(f"rates: {rates_note}")
    for src, data in result.items():
        print(f"[{src}] total ${data['total_cost']:.2f}"
              + (f"  (since {data['first_use'][:10]})" if data["first_use"] else ""))
        for model, mt in sorted(data["model_totals"].items(), key=lambda x: -x[1]["costUSD"]):
            print(f"  {model:<40} ${mt['costUSD']:>9.2f}  in {mt['inputTokens']:>12,}  out {mt['outputTokens']:>10,}")
        print(f"  -- {args.period} --")
        for row in data[args.period]:
            print(f"  {row['period']:<10} ${row['total_cost']:>9.2f}  in {row['total_input']:>12,}  out {row['total_output']:>10,}  msgs {row['total_messages']}")
        print()
    return 0


# ── projects / sessions / preview ───────────────────────────────────────


def cmd_projects(args) -> int:
    from asm.models import PROJECTS_DIR, encode_path
    from asm.services import claude_data, codex_data

    srcs = _sources(args.source)
    merged: dict[str, dict] = {}
    if "claude" in srcs:
        for p in claude_data.get_projects():
            d = PROJECTS_DIR / encode_path(p.path)
            count = len(list(d.glob("*.jsonl"))) if d.exists() else 0
            merged[p.path] = {"path": p.path, "exists": p.exists, "claude_sessions": count, "codex": False}
    if "codex" in srcs:
        for p in codex_data.get_projects():
            entry = merged.setdefault(
                p.path, {"path": p.path, "exists": p.exists, "claude_sessions": 0, "codex": False}
            )
            entry["codex"] = True

    rows = sorted(merged.values(), key=lambda r: r["path"].casefold())
    if args.json:
        _out_json(rows)
        return 0
    if sys.stdout.isatty():
        from rich.text import Text

        _print_table(
            f"{len(rows)} projects", ["", "project", "src", "sessions"],
            [(
                Text("●", style="green") if r["exists"] else Text("✗", style="red"),
                r["path"],
                Text("C", style="#D97757") + Text("X", style="#10A37F") if r["claude_sessions"] and r["codex"]
                else _src_cell("claude") if r["claude_sessions"] else _src_cell("codex") if r["codex"] else Text("-"),
                str(r["claude_sessions"] or ""),
            ) for r in rows],
            justify=("center", "left", "left", "right"),
        )
        return 0

    for r in rows:
        mark = ("C" if r["claude_sessions"] else "") + ("X" if r["codex"] else "")
        status = "O" if r["exists"] else "X"
        print(f"{status} {r['path']}  [{mark or '-'}]  {r['claude_sessions']} sessions")
    print(f"\n{len(rows)} projects")
    return 0


def cmd_sessions(args) -> int:
    rows = _tagged_sessions(args.source, args.project)
    if args.search:
        q = args.search.casefold()
        rows = [r for r in rows if q in (r[0].summary or "").casefold()]
    if args.limit:
        rows = rows[: args.limit]

    if args.json:
        _out_json([
            {
                "session_id": s.session_id,
                "source": src,
                "project": path,
                "title": s.summary,
                "modified": _fmt_ts(s.last_modified),
                "size_bytes": s.file_size,
            }
            for s, src, path in rows
        ])
        return 0
    if sys.stdout.isatty():
        # project before title: a long title would otherwise fold and push the
        # project column (needed to resume) off-screen. Resume needs the cwd —
        # `claude -r <id>` only finds a session in its own project directory.
        _print_table(
            f"{len(rows)} sessions", ["src", "modified", "project", "session id", "title"],
            [(
                _src_cell(src), _fmt_ts(s.last_modified), path, s.session_id,
                (s.summary or "").replace("\n", " ")[:_TITLE_WIDTH],
            ) for s, src, path in rows],
        )
        from rich.console import Console
        Console().print(
            "[dim]Resume any session: [/][cyan]asm resume <session id>[/]"
            "[dim]  (cds into its project automatically; Claude and Codex)[/]"
        )
        return 0

    for s, src, path in rows:
        title = (s.summary or "").replace("\n", " ")[:_TITLE_WIDTH]
        mark = "C" if src == "claude" else "X"
        print(f"{mark} {_fmt_ts(s.last_modified)}  {s.session_id}  {title}  ({path})")
    print(f"\n{len(rows)} sessions")
    return 0


def cmd_preview(args) -> int:
    from asm.services import claude_data, codex_data

    # Resolve the owning project dir up front so the direct JSONL path is used
    # (the SDK lookup returns [] for ids it can't find instead of raising).
    project = args.project or _find_claude_project_dir(args.session_id)

    messages = claude_data.get_session_messages(args.session_id, project, args.limit)
    if not messages and codex_data.is_available():
        messages = codex_data.get_session_messages(args.session_id, limit=args.limit)
    if not messages:
        print(f"session not found: {args.session_id}", file=sys.stderr)
        return 1
    if args.json:
        _out_json(messages)
        return 0
    if sys.stdout.isatty():
        from rich.console import Console

        console = Console()
        styles = {"user": "bold cyan", "assistant": "bold magenta"}
        for m in messages:
            role = _role(m)
            console.rule(f"[{styles.get(role, 'dim')}]{role}[/]", align="left", style="dim")
            console.print(m.get("content", ""), markup=False, highlight=False)
        return 0
    for m in messages:
        role = _role(m)
        print(f"--- {role} ---")
        print(m.get("content", ""))
    return 0


# ── artifacts ───────────────────────────────────────────────────────────


def cmd_artifacts(args) -> int:
    from asm.models import decode_path_hint
    from asm.services.artifacts import list_artifacts

    items = list_artifacts()
    if args.limit:
        items = items[: args.limit]
    if args.json:
        _out_json([vars(a) for a in items])
        return 0
    if sys.stdout.isatty():
        _print_table(
            f"{len(items)} artifacts", ["published", "title", "project", "url"],
            [(
                _fmt_ts(a.published),
                f"{a.favicon} {a.title}".strip(),
                decode_path_hint(a.project_dir) if a.project_dir else "",
                a.url,
            ) for a in items],
        )
        return 0
    for a in items:
        print(f"{_fmt_ts(a.published)}  {a.url}  {a.title}")
    print(f"\n{len(items)} artifacts")
    return 0


# ── resume ──────────────────────────────────────────────────────────────


def _read_claude_session_cwd(jsonl: Path) -> str | None:
    """The working dir recorded inside a Claude session JSONL, if any."""
    try:
        with open(jsonl) as f:
            for line in f:
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(o, dict) and o.get("cwd"):
                    return o["cwd"]
    except OSError:
        pass
    return None


def _exec_resume(argv: list[str], cwd: str | None, dry_run: bool) -> int:
    """chdir into ``cwd`` and replace this process with ``argv`` (resume)."""
    import os
    import shutil

    if shutil.which(argv[0]) is None:
        print(f"{argv[0]} not found in PATH", file=sys.stderr)
        return 1
    if cwd:
        if not os.path.isdir(cwd):
            print(f"project dir not found: {cwd}", file=sys.stderr)
            return 1
        os.chdir(cwd)
    print(f"↻ (cwd: {cwd or os.getcwd()})  {' '.join(argv)}", file=sys.stderr)
    if dry_run:
        return 0
    os.execvp(argv[0], argv)  # replaces this process; never returns on success
    return 1  # only reached if execvp fails


def cmd_resume(args) -> int:
    """Find a session by id, cd into its project, and resume Claude/Codex.

    `claude -r <id>` only finds a session in its own project dir, so we resolve
    the recorded cwd first. Codex resumes by id (cwd restored from its meta).
    """
    from asm.models import PROJECTS_DIR, decode_path_hint

    sid = args.session_id

    enc = _find_claude_project_dir(sid)
    if enc:
        cwd = _read_claude_session_cwd(PROJECTS_DIR / enc / f"{sid}.jsonl") or decode_path_hint(enc)
        return _exec_resume(["claude", "-r", sid], cwd, args.dry_run)

    from asm.services import codex_data
    if codex_data.is_available():
        s = codex_data.find_session(sid)
        if s:
            return _exec_resume(["codex", "resume", sid], s.cwd or None, args.dry_run)

    print(f"session not found: {sid}", file=sys.stderr)
    return 1


# ── clean ───────────────────────────────────────────────────────────────


def cmd_clean(args) -> int:
    from asm.services import claude_data, cleaner

    if args.target == "empty":
        items = claude_data.find_empty_sessions()
        if args.dry_run or not items:
            _print_clean_plan(items, "empty session(s)", args)
            return 0
        if not _confirm(f"Trash {len(items)} empty session(s)?", args.yes):
            return 1
        ok = fail = 0
        for e in items:
            if cleaner.trash_single_session_file(e["project_dir"], e["session_id"]):
                ok += 1
            else:
                fail += 1
        print(f"trashed {ok}, failed {fail}")
        return 0 if fail == 0 else 1

    if args.target == "orphaned":
        names = [s.dir_name for s in claude_data.get_sessions() if s.is_orphaned]
        if args.dry_run or not names:
            _print_clean_plan(names, "orphaned session dir(s)", args)
            return 0
        if not _confirm(f"Trash {len(names)} orphaned session dir(s)?", args.yes):
            return 1
        ok, fail = cleaner.trash_sessions(names)
        print(f"trashed {ok}, failed {fail}")
        return 0 if fail == 0 else 1

    if args.target in ("debug", "todos"):
        if args.target == "debug":
            names = cleaner.list_empty_debug_files()
            label, prune = "empty debug file(s)", cleaner.prune_empty_debug_files
        else:
            names = cleaner.list_empty_todo_entries()
            label, prune = "empty todo entry(ies)", cleaner.prune_empty_todo_files
        if args.dry_run or not names:
            _print_clean_plan(names, label, args)
            return 0
        if not _confirm(f"Trash {len(names)} {label}?", args.yes):
            return 1
        ok, fail = prune()
        print(f"trashed {ok}, failed {fail}")
        return 0 if fail == 0 else 1
    return 1


def _print_clean_plan(items, label: str, args) -> None:
    if args.json:
        _out_json(items)
        return
    for it in items:
        print(it if isinstance(it, str) else f"{it['session_id']}  ({it['project_dir']})  {it.get('title', '')}")
    print(f"{len(items)} {label}" + (" — dry run, nothing deleted" if args.dry_run else ""))


# ── backup / recovery ───────────────────────────────────────────────────

_BACKUP_CREATORS = {
    "config": "create_config_backup",
    "full": "create_full_backup",
    "settings": "create_settings_backup",
    "plugins": "create_plugins_backup",
    "sessions": "create_sessions_backup",
    "codex": "create_codex_backup",
}

def _backup_source_dirs_full():
    from asm.models import CLAUDE_DIR
    return [CLAUDE_DIR]


def _backup_source_dirs_sessions():
    from asm.models import PROJECTS_DIR
    return [PROJECTS_DIR]


def _backup_source_dirs_plugins():
    from asm.models import PLUGINS_DIR, SKILLS_DIR
    return [PLUGINS_DIR, SKILLS_DIR]


def _backup_source_dirs_codex():
    from asm.models import CODEX_SESSIONS_DIR
    return [CODEX_SESSIONS_DIR]


# Directory-based backup types copy whole trees (possibly GBs) — the create
# path sizes the source up front and confirms. config/settings copy tiny files.
_BACKUP_SOURCE_DIRS = {
    "full": _backup_source_dirs_full,
    "sessions": _backup_source_dirs_sessions,
    "plugins": _backup_source_dirs_plugins,
    "codex": _backup_source_dirs_codex,
}

_BACKUP_RESTORERS = {
    "config": "restore_config_backup",
    "full": "restore_full_backup",
    "settings": "restore_settings_backup",
    "plugins": "restore_plugins_backup",
    "sessions": "restore_sessions_backup",
    "codex": "restore_codex_backup",
}


def cmd_backup(args) -> int:
    from asm.services import backup

    if args.action == "list":
        items = backup.list_backups()
        if args.json:
            _out_json([vars(b) for b in items])
            return 0
        if sys.stdout.isatty():
            _print_table(
                f"{len(items)} backups", ["created", "type", "size", "path"],
                [(_fmt_ts(b.created), b.backup_type, format_bytes(b.size_bytes), b.path) for b in items],
                justify=("left", "left", "right", "left"),
            )
            return 0
        for b in items:
            print(f"{_fmt_ts(b.created)}  {b.backup_type:<9} {format_bytes(b.size_bytes):>10}  {b.path}")
        print(f"\n{len(items)} backups")
        return 0

    if args.action == "create":
        source_dirs = _BACKUP_SOURCE_DIRS.get(args.type)
        if source_dirs:
            from asm.utils import dir_size

            total = sum(dir_size(d) for d in source_dirs() if d.exists())
            if not _confirm(
                f"Create {args.type} backup (~{format_bytes(total)} will be copied)?", args.yes
            ):
                return 1
        path = getattr(backup, _BACKUP_CREATORS[args.type])()
        if path:
            print(path)
            return 0
        print("backup failed", file=sys.stderr)
        return 1

    if args.action == "restore":
        info = next((b for b in backup.list_backups() if b.path == args.path or b.name == args.path), None)
        if not info:
            print(f"backup not found: {args.path}", file=sys.stderr)
            return 1
        if not _confirm(f"Restore {info.backup_type} backup '{info.name}'? Current data is replaced (rollback-safe)", args.yes):
            return 1
        result = getattr(backup, _BACKUP_RESTORERS[info.backup_type])(info.path)
        ok = result[0] if isinstance(result, tuple) else result
        print("restored" if ok else "restore failed")
        return 0 if ok else 1

    if args.action == "delete":
        if not _confirm(f"Delete backup {args.path}?", args.yes):
            return 1
        ok = backup.delete_backup(args.path)
        print("deleted" if ok else "delete failed")
        return 0 if ok else 1

    if args.action == "export":
        out = backup.export_backup(args.path, dest_dir=args.dest)
        print(out or "export failed")
        return 0 if out else 1

    if args.action == "import":
        out = backup.import_backup(args.path)
        print(out or "import failed")
        return 0 if out else 1
    return 1


def cmd_recovery(args) -> int:
    from asm.services import recovery

    if args.action == "list":
        items = recovery.list_recovery_items()
        if args.json:
            _out_json([vars(r) for r in items])
            return 0
        if sys.stdout.isatty():
            _print_table(
                f"{len(items)} recovery snapshots", ["created", "id", "category", "original path"],
                [(_fmt_ts(r.created), r.id, r.category, r.original_path) for r in items],
            )
            return 0
        for r in items:
            print(f"{_fmt_ts(r.created)}  {r.id}  [{r.category}]  {r.name}  -> {r.original_path}")
        print(f"\n{len(items)} recovery snapshots")
        return 0

    if args.action == "restore":
        if not _confirm(f"Restore snapshot {args.id}?", args.yes):
            return 1
        ok, msg = recovery.restore_recovery_item(args.id, overwrite=args.overwrite)
        print(msg)
        return 0 if ok else 1

    if args.action == "delete":
        if not _confirm(f"Delete snapshot {args.id}?", args.yes):
            return 1
        ok = recovery.delete_recovery_item(args.id)
        print("deleted" if ok else "delete failed")
        return 0 if ok else 1
    return 1


# ── trash / migrate ─────────────────────────────────────────────────────


def cmd_trash(args) -> int:
    from asm.services import cleaner, codex_data

    encoded_dir = _find_claude_project_dir(args.session_id)
    if encoded_dir:
        if not _confirm(f"Trash Claude session {args.session_id} ({encoded_dir})?", args.yes):
            return 1
        ok = cleaner.trash_single_session_file(encoded_dir, args.session_id)
        print("trashed" if ok else "trash failed")
        return 0 if ok else 1

    if codex_data.is_available():
        # Full-filename lookup, not the recent-N scan window — old sessions
        # must stay trashable by id.
        s = codex_data.find_session(args.session_id)
        if s:
            if not _confirm(f"Trash Codex session {args.session_id}?", args.yes):
                return 1
            ok = cleaner.trash_codex_session(s.project_dir)  # rollout path
            print("trashed" if ok else "trash failed")
            return 0 if ok else 1

    print(f"session not found: {args.session_id}", file=sys.stderr)
    return 1


def cmd_migrate(args) -> int:
    from asm.models import encode_path
    from asm.services import migrate

    if not _confirm(
        f"Migrate sessions {args.src} -> {args.dest}"
        + (f" ({len(args.sessions)} selected)" if args.sessions else " (all)") + "?",
        args.yes,
    ):
        return 1
    result = migrate.migrate_sessions(
        source_path=args.src,
        target_path=args.dest,
        source_encoded=encode_path(args.src),
        target_encoded=encode_path(args.dest),
        session_ids=args.sessions or None,
    )
    print(f"{'ok' if result.success else 'failed'}: {result.sessions_copied} session(s) copied"
          + (f" — {result.message}" if result.message else ""))
    return 0 if result.success else 1


# ── parser wiring ───────────────────────────────────────────────────────



def _import_direction(to: str) -> str:
    return "claude-to-codex" if to == "codex" else "codex-to-claude"


def _resolve_import_source(session_id: str):
    """Locate a session by id and report which way it can travel."""
    from pathlib import Path as _Path

    from asm.models import PROJECTS_DIR
    from asm.services import agent_import, codex_data

    encoded = _find_claude_project_dir(session_id)
    if encoded:
        return "claude", _Path(PROJECTS_DIR) / encoded / f"{session_id}.jsonl"
    rollout = agent_import.find_codex_rollout(session_id)
    if rollout:
        return "codex", rollout
    if codex_data.is_available():
        found = codex_data.find_session(session_id)
        if found:
            return "codex", _Path(found.project_dir)
    return None, None


def cmd_import(args) -> int:
    from asm.services import agent_import

    if args.action == "home":
        from asm.models import CODEX_DIR, CODEX_SESSIONS_DIR

        count = sum(1 for _ in CODEX_SESSIONS_DIR.rglob("rollout-*.jsonl")) if CODEX_SESSIONS_DIR.is_dir() else 0
        payload = {"path": str(CODEX_DIR), "exists": CODEX_DIR.is_dir(), "sessions": count}
        if args.json:
            print(json.dumps({"codex_home": payload}, ensure_ascii=False, indent=2))
        else:
            suffix = "" if payload["exists"] else "  (missing)"
            print(f"Codex home: {payload['path']}  {payload['sessions']} sessions{suffix}")
        return 0

    if args.action == "mcp":
        plan = agent_import.plan_mcp(_import_direction(args.to))
        if args.json:
            print(json.dumps({
                "new": [s.name for s in plan.new],
                "already_present": plan.already_present,
                "unsupported": [{"name": n, "reason": r} for n, r in plan.unsupported],
            }, ensure_ascii=False, indent=2))
        else:
            print(f"{plan.source} -> {plan.target}: {len(plan.new)} importable, "
                  f"{len(plan.already_present)} already there")
            for server in plan.new:
                print(f"  + {server.name} ({server.transport})")
            for name, reason in plan.unsupported:
                print(f"  - {name}: {reason}")
        if args.dry_run or not plan.new:
            return 0
        if not _confirm(f"Import {len(plan.new)} MCP server(s) into {plan.target}?", args.yes):
            return 1
        result = agent_import.apply_mcp(plan)
        print(f"imported {len(result.imported)}, failed {len(result.failed)}")
        for name, reason in result.failed:
            print(f"  ! {name}: {reason}", file=sys.stderr)
        return 1 if result.failed else 0

    if args.action == "list":
        plan = (agent_import.plan_sessions_to_codex() if args.to == "codex"
                else agent_import.plan_sessions_to_claude())
        rows = [{"path": c.path, "title": c.title, "turns": c.turns, "cwd": c.cwd,
                 "last_active": datetime.fromtimestamp(c.modified_at / 1e9).isoformat(" ", "minutes")}
                for c in plan.new][: args.limit or None]
        if args.json:
            print(json.dumps({"new": rows, "already_imported": len(plan.already_imported),
                              "truncated": plan.truncated}, ensure_ascii=False, indent=2))
            return 0
        print(f"{len(plan.new)} importable, {len(plan.already_imported)} already imported"
              + (f", {plan.truncated} older not listed" if plan.truncated else "")
              + "  (newest activity first)")
        for row in rows:
            print(f"  {_session_id_of(row['path'])}  {row['last_active']}  "
                  f"{row['turns']:>5} turns  {row['title'][:52]}")
        return 0

    source, path = _resolve_import_source(args.session_id)
    if not source:
        print(f"session not found: {args.session_id}", file=sys.stderr)
        return 1

    if source == "claude":
        plan = agent_import.plan_sessions_to_codex([path])
        target = "codex"
    else:
        plan = agent_import.plan_sessions_to_claude([path])
        target = "claude"

    if plan.skipped:
        for _p, reason in plan.skipped:
            print(f"skipped: {reason}", file=sys.stderr)
        return 1
    if plan.already_imported and not plan.new:
        print(f"already imported into {target}; nothing to do")
        return 0

    candidate = plan.new[0]
    origin = f"  [{path.parents[4].name}]" if source == "codex" and len(path.parents) > 4 else ""
    print(f"{source} -> {target}: {candidate.turns} turns  cwd={candidate.cwd}{origin}")
    print(f"  title: {candidate.title[:70]}")
    if args.dry_run:
        return 0
    if not _confirm(f"Import this session into {target}?", args.yes):
        return 1

    result = (agent_import.apply_sessions_to_codex(plan) if target == "codex"
              else agent_import.apply_sessions_to_claude(plan))
    for _src, new_id in result.imported:
        print(f"imported as {new_id}")
    for name, reason in result.failed:
        print(f"  ! {name}: {reason}", file=sys.stderr)
    if result.backup_path:
        print(f"backup: {result.backup_path}")
    return 1 if result.failed else 0


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _session_id_of(path: str) -> str:
    """Session id in a transcript filename (Claude `<uuid>`, Codex `rollout-<ts>-<uuid>`)."""
    stem = Path(path).stem
    found = _UUID_RE.search(stem)
    return found.group(0) if found else stem


def add_cli_subparsers(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="command", metavar="command")

    def _common(p, json_flag=True):
        if json_flag:
            p.add_argument("--json", action="store_true", help="machine-readable JSON output")

    p = sub.add_parser("cost", help="cost/token usage stats")
    p.add_argument("--period", choices=["daily", "weekly", "monthly"], default="daily")
    p.add_argument("--source", choices=["all", "claude", "codex"], default="all")
    p.add_argument("--limit", type=int, default=14, help="max period rows (default 14, 0 = all)")
    _common(p)
    p.set_defaults(func=cmd_cost)

    p = sub.add_parser("projects", help="list projects")
    p.add_argument("--source", choices=["all", "claude", "codex"], default="all")
    _common(p)
    p.set_defaults(func=cmd_projects)

    p = sub.add_parser("sessions", help="list/search sessions by title")
    p.add_argument("--project", help="limit to one project path")
    p.add_argument("--search", help="substring match on session title")
    p.add_argument("--source", choices=["all", "claude", "codex"], default="all")
    p.add_argument("--limit", type=int, default=50, help="max rows (default 50, 0 = all)")
    _common(p)
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("preview", help="print a session's conversation")
    p.add_argument("session_id")
    p.add_argument("--project", help="project path hint (faster lookup)")
    p.add_argument("--limit", type=int, default=50)
    _common(p)
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("artifacts", help="list artifacts published from Claude Code sessions")
    p.add_argument("--limit", type=int, default=50, help="max rows (default 50, 0 = all)")
    _common(p)
    p.set_defaults(func=cmd_artifacts)

    p = sub.add_parser("resume", help="cd into a session's project and resume it (Claude/Codex)")
    p.add_argument("session_id")
    p.add_argument("--dry-run", action="store_true", help="print the command + cwd instead of running it")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("clean", help="clean up stale data (trash + recovery snapshot)")
    p.add_argument("target", choices=["empty", "orphaned", "debug", "todos"])
    p.add_argument("--dry-run", action="store_true", help="list only, delete nothing")
    p.add_argument("--yes", action="store_true", help="skip confirmation")
    _common(p)
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("backup", help="manage backups")
    p.add_argument("action", choices=["list", "create", "restore", "delete", "export", "import"])
    p.add_argument("path", nargs="?", help="backup path/name (restore/delete/export) or archive (import)")
    p.add_argument("--type", choices=sorted(_BACKUP_CREATORS), default="config", help="backup type for create")
    p.add_argument("--dest", help="destination dir for export")
    p.add_argument("--yes", action="store_true", help="skip confirmation")
    _common(p)
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("recovery", help="recovery snapshots (created before deletions)")
    p.add_argument("action", choices=["list", "restore", "delete"])
    p.add_argument("id", nargs="?", help="snapshot id (restore/delete)")
    p.add_argument("--overwrite", action="store_true", help="overwrite existing data on restore")
    p.add_argument("--yes", action="store_true", help="skip confirmation")
    _common(p)
    p.set_defaults(func=cmd_recovery)

    p = sub.add_parser("trash", help="trash one session by id (Claude or Codex)")
    p.add_argument("session_id")
    p.add_argument("--yes", action="store_true", help="skip confirmation")
    p.set_defaults(func=cmd_trash)

    p = sub.add_parser(
        "import",
        help="copy MCP servers or sessions between Claude Code and Codex",
        description=(
            "Copy MCP servers or sessions between Claude Code and Codex.\n"
            "\n"
            "Sessions are listed newest-first by LAST ACTIVITY (file mtime), which is not\n"
            "the timestamp in a Codex rollout filename -- that one is when the session\n"
            "started. A session begun 08-13 and continued until 08-19 therefore sorts\n"
            "above one that started 08-19. The `list` output prints that activity time so\n"
            "the ordering is visible.\n"
            "\n"
            "Only the newest 200 transcripts are examined per run (hashing every one of\n"
            "25k+ rollouts is slow); the count left out is reported, never hidden.\n"
            "\n"
            "Imported copies get a NEW id and are recorded so re-running skips them.\n"
            "They carry zero token usage, so a copied session is never priced twice.\n"
            "The destination folder is chosen from the session's own cwd.\n"
            "\n"
            "Codex App, CLI and companion agents share one CODEX_HOME (default ~/.codex).\n"
            "asm never auto-discovers sibling ~/.codex-* directories. To inspect legacy\n"
            "split-home data for one run, start asm with CODEX_HOME=/path/to/legacy-home.\n"
            "`asm import home` prints the active data home.\n"
            "\n"
            "Examples:\n"
            "  asm import home                        # active Codex data home\n"
            "  asm import list --to claude            # what could move Codex -> Claude\n"
            "  asm import session <id> --dry-run      # direction inferred from the id\n"
            "  asm import session <id> --yes\n"
            "  asm import mcp --to codex              # MCP servers Claude -> Codex\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("action", choices=["session", "mcp", "list", "home"],
                   help="session: copy one session by id; mcp: copy MCP servers; "
                        "list: importable sessions; home: active Codex data home")
    p.add_argument("session_id", nargs="?", help="session id (action=session)")
    p.add_argument("--to", choices=["codex", "claude"], default="claude",
                   help="destination agent for mcp/list (default: claude); session infers it from the id")
    p.add_argument("--limit", type=int, default=50, help="max rows for list (default 50, 0 = all)")
    p.add_argument("--dry-run", action="store_true", help="show what would happen, write nothing")
    p.add_argument("--yes", action="store_true", help="skip confirmation")
    _common(p)
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("migrate", help="copy sessions to another project path")
    p.add_argument("src", help="source project path")
    p.add_argument("dest", help="target project path")
    p.add_argument("--sessions", nargs="*", help="session ids (default: all)")
    p.add_argument("--yes", action="store_true", help="skip confirmation")
    p.set_defaults(func=cmd_migrate)


def run_cli(args) -> int:
    """Dispatch a parsed subcommand; returns the process exit code."""
    # Validate positional combos argparse can't express.
    if args.command == "backup" and args.action in ("restore", "delete", "export", "import") and not args.path:
        print(f"backup {args.action} requires a path", file=sys.stderr)
        return 2
    if args.command == "recovery" and args.action in ("restore", "delete") and not args.id:
        print(f"recovery {args.action} requires an id", file=sys.stderr)
        return 2
    if args.command == "import" and args.action == "session" and not args.session_id:
        print("import session requires a session id", file=sys.stderr)
        return 2
    return args.func(args)
