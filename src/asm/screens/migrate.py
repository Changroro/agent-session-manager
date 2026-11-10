"""Session migration screen - two-panel folder selection with session picking."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path, PurePosixPath

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, DirectoryTree, Static, Tree

from asm.models import PROJECTS_DIR, encode_path
from asm.screens.confirm import ConfirmScreen
from asm.i18n import t
from rich.markup import escape
from asm.services.claude_data import get_session_messages
from asm.services.migrate import get_available_projects, migrate_sessions
from asm.widgets.action_bar import ActionBar


class MigratePane(Container):
    """Session migration with two-panel project selection and session picking."""

    CSS = """
    MigratePane {
        height: 1fr;
        padding: 1;
    }
    #migrate-info {
        height: auto;
        margin-bottom: 1;
        color: $text-muted;
    }
    #migrate-panels {
        height: auto;
        max-height: 40%;
    }
    .migrate-panel {
        width: 1fr;
        height: 1fr;
        border: tall $primary;
        padding: 0 1;
    }
    .panel-title {
        height: auto;
        text-style: bold;
        margin-bottom: 1;
    }
    .panel-selected {
        height: auto;
        margin-bottom: 1;
        color: $accent;
    }
    .migrate-tree {
        height: 1fr;
    }
    #session-panel {
        height: 1fr;
        border-top: tall $primary;
        padding: 0 1;
    }
    #session-header {
        height: auto;
        margin: 1 0 0 0;
    }
    #session-table {
        height: auto;
        max-height: 9;
    }
    #session-preview {
        height: 1fr;
        min-height: 5;
        border-top: dashed $accent;
        padding: 0 1;
        display: none;
    }
    #migrate-actions {
        height: 1;
        margin-top: 1;
    }
    #migrate-result {
        height: auto;
        margin-top: 1;
        padding: 1;
        border: round $success;
        display: none;
    }
    """

    BINDINGS = [
        ("space", "toggle_session", "Toggle"),
        ("a", "toggle_all", "All/None"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._source_hint: str | None = None
        self._source_encoded: str | None = None
        self._target_hint: str | None = None
        self._target_encoded: str | None = None
        self._selected_sessions: set[str] = set()
        self._session_rows: dict = {}  # session_id -> RowKey
        self._mode: str = "append"

    def compose(self) -> ComposeResult:
        yield Static(
            t("mig.info"),
            id="migrate-info",
        )
        with Horizontal(id="migrate-panels"):
            with Vertical(classes="migrate-panel"):
                yield Static(t("mig.source_title"), classes="panel-title")
                yield Static(t("mig.not_selected"), classes="panel-selected", id="source-selected")
                yield Tree("Projects", id="source-tree", classes="migrate-tree")
            with Vertical(classes="migrate-panel"):
                yield Static(t("mig.target_title"), classes="panel-title")
                yield Static(t("mig.not_selected"), classes="panel-selected", id="target-selected")
                yield DirectoryTree(str(Path.home()), id="target-tree", classes="migrate-tree")
        with Vertical(id="session-panel"):
            yield Static("[dim]Select a source project to see sessions[/]", id="session-header")
            yield DataTable(id="session-table")
            with VerticalScroll(id="session-preview"):
                yield Static("", id="session-preview-body")
        yield ActionBar(id="migrate-actions")
        yield Static("", id="migrate-result")

    def on_mount(self) -> None:
        self.query_one("#source-tree", Tree).show_root = False
        st = self.query_one("#session-table", DataTable)
        st.cursor_type = "row"
        st.zebra_stripes = True
        st.add_columns("", "Date", "Session", "Size")
        self._render_actions()
        if not getattr(self.app, "lazy_tabs", False):  # main app loads tabs on first view
            self.run_worker(self._load_projects, thread=True)

    def _render_actions(self) -> None:
        bar = self.query_one("#migrate-actions", ActionBar)
        mode_label = "Append" if self._mode == "append" else "Overwrite"
        bar.set_actions(
            [
                ("toggle_mode", f"Move mode: {mode_label}", "#555555"),
                ("migrate", "Move", "#ba3c5b"),
                ("copy", "Copy", "#0178d4"),
            ],
            on_action=self._on_action,
        )

    def _on_action(self, action_id: str) -> None:
        if action_id == "toggle_mode":
            self._mode = "overwrite" if self._mode == "append" else "append"
            self._render_actions()
        elif action_id == "migrate":
            self._start_migrate()
        elif action_id == "copy":
            self._start_migrate(copy=True)

    def _load_projects(self) -> None:
        projects = get_available_projects()
        project_sessions = {}
        for encoded, hint in projects:
            session_dir = PROJECTS_DIR / encoded
            count = len(list(session_dir.glob("*.jsonl"))) if session_dir.exists() else 0
            project_sessions[encoded] = count
        self.app.call_from_thread(self._build_trees, projects, project_sessions)

    def _build_trees(self, projects: list[tuple[str, str]], project_sessions: dict[str, int]) -> None:
        source_tree = self.query_one("#source-tree", Tree)
        source_tree.clear()
        source_projects = [(e, h) for e, h in projects if project_sessions.get(e, 0) > 0]
        self._populate_tree(source_tree, source_projects, project_sessions)

    def _populate_tree(self, tree: Tree, projects: list[tuple[str, str]], session_counts: dict[str, int]) -> None:
        root_nodes: dict = {}
        for encoded, hint in projects:
            parts = PurePosixPath(hint).parts
            current = root_nodes
            for part in parts:
                if part not in current:
                    current[part] = {}
                current = current[part]
            current["__leaf__"] = (encoded, hint, session_counts.get(encoded, 0))

        self._render_nodes(tree.root, root_nodes, "")

    def _render_nodes(self, parent, node_dict: dict, current_path: str) -> None:
        for key, children in sorted(node_dict.items()):
            if key == "__leaf__":
                continue

            new_path = f"{current_path}/{key}" if current_path else key
            is_leaf = "__leaf__" in children
            child_dirs = {k: v for k, v in children.items() if k != "__leaf__"}

            if is_leaf:
                encoded, hint, sessions = children["__leaf__"]
                name = PurePosixPath(hint).name or hint
                session_str = f"  [dim]{sessions} sessions[/]" if sessions > 0 else "  [dim]empty[/]"
                label = f"\U0001f4c2 {name}{session_str}"
                if child_dirs:
                    node = parent.add(label, data=("project", hint, encoded), expand=False)
                    self._render_nodes(node, child_dirs, new_path)
                else:
                    parent.add_leaf(label, data=("project", hint, encoded))
            elif len(child_dirs) == 1:
                child_key = list(child_dirs.keys())[0]
                collapsed = f"{key}/{child_key}"
                collapsed_children = child_dirs[child_key]

                while (
                    "__leaf__" not in collapsed_children
                    and len({k for k in collapsed_children if k != "__leaf__"}) == 1
                ):
                    next_key = [k for k in collapsed_children if k != "__leaf__"][0]
                    collapsed = f"{collapsed}/{next_key}"
                    collapsed_children = collapsed_children[next_key]

                is_collapsed_leaf = "__leaf__" in collapsed_children
                collapsed_child_dirs = {k: v for k, v in collapsed_children.items() if k != "__leaf__"}

                if is_collapsed_leaf:
                    encoded, hint, sessions = collapsed_children["__leaf__"]
                    session_str = f"  [dim]{sessions} sessions[/]" if sessions > 0 else "  [dim]empty[/]"
                    label = f"\U0001f4c2 {collapsed}{session_str}"
                    if collapsed_child_dirs:
                        node = parent.add(label, data=("project", hint, encoded), expand=False)
                        self._render_nodes(node, collapsed_child_dirs, f"{current_path}/{collapsed}")
                    else:
                        parent.add_leaf(label, data=("project", hint, encoded))
                elif collapsed_child_dirs:
                    count = self._count_leaves(collapsed_children)
                    group = parent.add(
                        f"\U0001f4c1 [bold]{collapsed}/[/]  ({count})",
                        data=("group", None, None),
                        expand=False,
                    )
                    self._render_nodes(group, collapsed_children, f"{current_path}/{collapsed}")
            else:
                count = self._count_leaves(children)
                group = parent.add(
                    f"\U0001f4c1 [bold]{key}/[/]  ({count})",
                    data=("group", None, None),
                    expand=False,
                )
                self._render_nodes(group, children, new_path)

    def _count_leaves(self, node_dict: dict) -> int:
        count = 1 if "__leaf__" in node_dict else 0
        for k, v in node_dict.items():
            if k != "__leaf__" and isinstance(v, dict):
                count += self._count_leaves(v)
        return count

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        node_data = event.node.data
        if not node_data or node_data[0] != "project":
            return

        _, hint, encoded = node_data
        tree_id = event.node.tree.id

        if tree_id == "source-tree":
            self._source_hint = hint
            self._source_encoded = encoded
            self._selected_sessions.clear()
            self.query_one("#source-selected", Static).update(f"[bold cyan]{hint}[/]")
            self.run_worker(lambda e=encoded, h=hint: self._load_sessions(e, h), thread=True, group="session-load", exclusive=True)

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        target_path = str(event.path)
        self._target_hint = target_path
        self._target_encoded = encode_path(target_path)
        self.query_one("#target-selected", Static).update(f"[bold green]{target_path}[/]")

    def _load_sessions(self, encoded: str, hint: str) -> None:
        """Load sessions for the selected source project."""
        session_dir = PROJECTS_DIR / encoded
        if not session_dir.exists():
            self.app.call_from_thread(self._populate_session_table, hint, [])
            return

        all_jsonl = list(session_dir.glob("*.jsonl"))
        sessions = []
        for jsonl in sorted(all_jsonl, key=lambda f: f.stat().st_mtime, reverse=True):
            try:
                stat = jsonl.stat()
                first_prompt = ""
                with open(jsonl) as f:
                    for line in f:
                        try:
                            msg = json.loads(line)
                            if msg.get("type") == "user" and not msg.get("isMeta"):
                                raw = msg.get("message", {}).get("content", "")
                                if isinstance(raw, str) and not raw.startswith("<"):
                                    first_prompt = raw[:50]
                                    break
                                elif isinstance(raw, list):
                                    for block in raw:
                                        if isinstance(block, dict) and block.get("type") == "text":
                                            text = block.get("text", "")
                                            if not text.startswith("<"):
                                                first_prompt = text[:50]
                                                break
                                    if first_prompt:
                                        break
                        except (json.JSONDecodeError, KeyError):
                            continue
                dt_str = datetime.fromtimestamp(stat.st_mtime).strftime("%m/%d %H:%M")
                size_kb = stat.st_size / 1024
                sessions.append((jsonl.stem, dt_str, first_prompt or jsonl.stem[:12], f"{size_kb:.0f}KB"))
            except OSError:
                continue
        self.app.call_from_thread(self._populate_session_table, hint, sessions)

    def _populate_session_table(self, hint: str, sessions: list[tuple[str, str, str, str]]) -> None:
        """Fill the session DataTable with rows."""
        table = self.query_one("#session-table", DataTable)
        table.clear()
        self._session_rows = {}
        total = len(sessions)

        header = self.query_one("#session-header", Static)
        if total == 0:
            header.update(f"[bold]Source:[/] {hint}  [dim](no sessions)[/]")
            return

        for sid, dt, prompt, size in sessions:
            row_key = table.add_row("[dim]○[/]", dt, prompt, size, key=sid)
            self._session_rows[sid] = row_key
        self._selected_sessions.clear()
        self._update_header_count()

    def action_toggle_session(self) -> None:
        """Toggle selection of the focused session row."""
        table = self.query_one("#session-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return
        row_key = list(table.rows.keys())[table.cursor_row]
        sid = row_key.value
        check_col = list(table.columns.keys())[0]

        if sid in self._selected_sessions:
            self._selected_sessions.discard(sid)
            table.update_cell(row_key, check_col, "[dim]○[/]")
        else:
            self._selected_sessions.add(sid)
            table.update_cell(row_key, check_col, "[bold green]●[/]")

        self._update_header_count()

    def action_toggle_all(self) -> None:
        """Select every session, or clear the selection if all are selected."""
        table = self.query_one("#session-table", DataTable)
        if not self._session_rows:
            return
        check_col = list(table.columns.keys())[0]
        select_all = len(self._selected_sessions) < len(self._session_rows)
        for sid, row_key in self._session_rows.items():
            if select_all:
                self._selected_sessions.add(sid)
                table.update_cell(row_key, check_col, "[bold green]●[/]")
            else:
                self._selected_sessions.discard(sid)
                table.update_cell(row_key, check_col, "[dim]○[/]")
        self._update_header_count()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter or double-click on a session row → show conversation preview."""
        if event.control.id != "session-table":
            return
        sid = event.row_key.value
        if sid not in self._session_rows:
            return
        self.run_worker(lambda s=sid: self._load_session_preview(s), thread=True, group="session-preview", exclusive=True)

    def _load_session_preview(self, session_id: str) -> None:
        """Load messages for a session and show in preview."""
        messages = get_session_messages(session_id, project_dir=self._source_encoded, limit=20)
        self.app.call_from_thread(self._show_session_preview, session_id, messages)

    def _show_session_preview(self, session_id: str, messages: list[dict]) -> None:
        """Render session messages in the preview panel."""
        panel = self.query_one("#session-preview")
        body = self.query_one("#session-preview-body", Static)
        panel.display = True

        if not messages:
            body.update(f"[dim]Session {session_id[:12]}... - no messages[/]")
            return

        lines = [f"[bold]Session:[/] {session_id[:16]}...\n"]
        for m in messages[:15]:
            content = m.get("content", "")
            if len(content) > 120:
                content = content[:120] + "..."
            content = escape(content)
            if m.get("type") == "user":
                lines.append(f"[bold cyan]User:[/] {content}")
            else:
                lines.append(f"[bold green]Assistant:[/] {content}")
        body.update("\n".join(lines))

    def _update_header_count(self) -> None:
        """Update header to show selection count."""
        header = self.query_one("#session-header", Static)
        sel = len(self._selected_sessions)
        total = len(self._session_rows)
        if total and sel == total:
            note = f"[bold cyan]all {total} selected[/]"
        elif sel == 0:
            note = "[yellow]none selected — Space: pick, a: all[/]"
        else:
            note = f"[bold cyan]{sel}[/] of {total} selected"
        header.update(
            f"[bold]Source:[/] {self._source_hint}  ({total} sessions)  {note}  "
            f"[dim]Enter: preview[/]"
        )

    def _start_migrate(self, copy: bool = False) -> None:
        if not self._source_encoded:
            self.app.notify(t("mig.select_source"), severity="error")
            return
        if not self._target_encoded:
            self.app.notify(t("mig.select_target"), severity="error")
            return
        if self._source_encoded == self._target_encoded:
            self.app.notify(t("mig.same_error"), severity="error")
            return
        if not self._selected_sessions:
            self.app.notify(t("mig.no_sessions_selected"), severity="error")
            return

        sel = len(self._selected_sessions)
        total = len(self._session_rows)
        count_info = f"all {total}" if sel == total else f"{sel} of {total}"
        prompt = (
            t("mig.confirm_copy", src=self._source_hint, tgt=self._target_hint)
            if copy
            else t("mig.confirm", src=self._source_hint, tgt=self._target_hint, mode=self._mode)
        )
        self.app.push_screen(
            ConfirmScreen(prompt + f"\n({count_info} sessions selected)"),
            callback=lambda ok: self._do_migrate(copy) if ok else None,
        )

    def _do_migrate(self, copy: bool = False) -> None:
        src_hint = self._source_hint
        tgt_hint = self._target_hint
        src_enc = self._source_encoded
        tgt_enc = self._target_encoded
        # Always pass the explicit selection — implicit "empty means all"
        # semantics once bulk-copied a whole project by accident.
        selected = list(self._selected_sessions)
        mode = self._mode
        self.run_worker(
            lambda: self._execute_migrate(src_hint, tgt_hint, src_enc, tgt_enc, mode, selected, copy),
            thread=True,
        )

    def _execute_migrate(self, src_hint, tgt_hint, src_enc, tgt_enc, mode, session_ids, copy=False) -> None:
        result = migrate_sessions(
            copy=copy,
            source_path=src_hint,
            target_path=tgt_hint,
            mode=mode,
            source_encoded=src_enc,
            target_encoded=tgt_enc,
            session_ids=session_ids,
        )
        self.app.call_from_thread(self._show_result, result, copy)

    def refresh_data(self) -> None:
        """Reload project trees and the session list for the current source."""
        self._selected_sessions.clear()
        self._session_rows = {}
        table = self.query_one("#session-table", DataTable)
        table.clear()
        header = self.query_one("#session-header", Static)
        self.run_worker(self._load_projects, thread=True)
        if self._source_encoded:
            enc, hint = self._source_encoded, self._source_hint
            self.run_worker(lambda: self._load_sessions(enc, hint), thread=True,
                            group="session-load", exclusive=True)
        else:
            header.update("[dim]Select a source project to see sessions[/]")

    def _show_result(self, result, copy: bool = False) -> None:
        result_widget = self.query_one("#migrate-result", Static)
        if result.success:
            skipped = (
                f"  [dim](already in target: {result.sessions_skipped})[/]"
                if result.sessions_skipped else ""
            )
            result_widget.update(
                f"[green]{t('mig.complete')}[/]\n"
                f"Sessions {'copied' if copy else 'moved'}: {result.sessions_copied}{skipped}\n"
                f"Memory merged: {'Yes' if result.memory_copied else 'No'}\n"
                f"{result.message}"
            )
            self.app.notify(t("mig.complete"))
            self.refresh_data()
        else:
            result_widget.update(f"[red]{t('mig.failed')}:[/] {result.message}")
            self.app.notify(t("mig.failed"), severity="error")
        result_widget.display = True
