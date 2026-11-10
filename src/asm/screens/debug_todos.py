"""Debug files and Todos management screen."""

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Input, Static

from asm.models import DEBUG_DIR
from asm.screens.confirm import ConfirmScreen
from asm.services.claude_data import get_debug_files, get_todos, get_session_to_project_map
from asm.i18n import t
from asm.services.cleaner import (
    count_empty_files,
    count_empty_todos,
    prune_empty_debug_files,
    prune_empty_todo_files,
    trash_debug_file,
    trash_debug_files,
    trash_todo_file,
    trash_todo_files,
)
from asm.utils import format_bytes
from asm.widgets.action_bar import ActionBar


class DebugTodosPane(Container):
    """View and manage debug files and todos."""

    BINDINGS = [
        ("space", "toggle_select", "Select"),
        ("d", "trash_focused", "Trash"),
        ("D", "trash_orphaned_all", "Trash Orphaned"),
        ("p", "prune_empty", "Prune Empty"),
    ]

    CSS = """
    DebugTodosPane {
        height: 1fr;
        padding: 1;
    }
    #dt-info {
        height: auto;
        margin-bottom: 1;
        color: $text-muted;
    }
    #dt-layout {
        height: 1fr;
    }
    #dt-filter {
        margin-bottom: 1;
    }
    #dt-tables-panel {
        width: 1fr;
        height: 1fr;
    }
    .dt-section {
        height: 1fr;
        margin-bottom: 1;
    }
    .section-title {
        text-style: bold;
        margin-bottom: 0;
    }
    .dt-action-bar {
        height: 1;
        margin-top: 1;
    }
    #dt-preview-panel {
        width: 1fr;
        height: 1fr;
        max-width: 50%;
        border-left: tall $primary;
        padding: 0 1;
    }
    #dt-preview-scroll {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(
            t("dt.info"),
            id="dt-info",
        )
        yield Input(placeholder=t("dt.filter_placeholder"), id="dt-filter")
        with Horizontal(id="dt-layout"):
            with Vertical(id="dt-tables-panel"):
                with Vertical(classes="dt-section"):
                    yield Static("", classes="section-title", id="debug-title")
                    yield DataTable(id="debug-table")
                    yield ActionBar(id="debug-actions", classes="dt-action-bar")
                with Vertical(classes="dt-section"):
                    yield Static("", classes="section-title", id="todo-title")
                    yield DataTable(id="todo-table")
                    yield ActionBar(id="todo-actions", classes="dt-action-bar")
            with Vertical(id="dt-preview-panel"):
                yield Static("[bold]Content Preview[/]", id="dt-preview-header")
                yield VerticalScroll(
                    Static(f"[dim]{t('dt.select_file')}[/]", id="dt-preview-body"),
                    id="dt-preview-scroll",
                )

    def on_mount(self) -> None:
        self._debug_entries: dict = {}
        self._todo_entries: dict = {}
        self._session_to_project: dict = {}
        dt = self.query_one("#debug-table", DataTable)
        dt.cursor_type = "row"
        dt.zebra_stripes = True
        dt.styles.height = "1fr"
        dt.add_columns("Name", "Size", "Status")

        tt = self.query_one("#todo-table", DataTable)
        tt.cursor_type = "row"
        tt.zebra_stripes = True
        tt.styles.height = "1fr"
        tt.add_columns("Name", "Size", "Status")

        self._orphaned_debug_names = []
        self._orphaned_todo_names = []
        self._empty_debug = 0
        self._empty_todo = 0
        self._selected_debug: set[str] = set()
        self._selected_todo: set[str] = set()
        self._debug_row_display: dict[str, str] = {}
        self._todo_row_display: dict[str, str] = {}
        self._debug_group_items: dict[str, list[str]] = {}
        self._todo_group_items: dict[str, list[str]] = {}
        self._filter_query = ""
        self._sort_mode = "project"
        self._all_debug = []
        self._all_todos = []
        if not getattr(self.app, "lazy_tabs", False):  # main app loads tabs on first view
            self.refresh_data()

    def refresh_data(self) -> None:
        self.run_worker(self._load, thread=True)

    def _load(self) -> None:
        debug = get_debug_files()
        todos = get_todos()
        empty_debug = count_empty_files(DEBUG_DIR)
        empty_todo = count_empty_todos()
        session_to_project = get_session_to_project_map()
        self.app.call_from_thread(self._set_data, debug, todos, empty_debug, empty_todo, session_to_project)

    def _set_data(self, debug, todos, empty_debug: int = 0, empty_todo: int = 0, session_to_project: dict = None) -> None:
        self._all_debug = list(debug)
        self._all_todos = list(todos)
        self._empty_debug = empty_debug
        self._empty_todo = empty_todo
        self._session_to_project = session_to_project or {}
        self._update()

    def _update(self) -> None:
        debug = list(self._all_debug)
        todos = list(self._all_todos)
        query = self._filter_query.casefold()
        if query:
            debug = [entry for entry in debug if self._matches_query(entry.name, entry)]
            todos = [entry for entry in todos if self._matches_query(entry.name, entry)]

        self._debug_entries = {d.name: d for d in debug}
        self._todo_entries = {td.name: td for td in todos}

        orphaned_debug = sum(1 for d in debug if d.is_orphaned)
        orphaned_todos = sum(1 for td in todos if td.is_orphaned)
        self._orphaned_debug_names = [d.name for d in debug if d.is_orphaned]
        self._orphaned_todo_names = [td.name for td in todos if td.is_orphaned]

        self.query_one("#debug-title", Static).update(
            f"[bold]Debug Files[/]  [dim]({len(debug)}, orphaned: [yellow]{orphaned_debug}[/])[/]"
        )
        self.query_one("#todo-title", Static).update(
            f"[bold]Todo Files[/]  [dim]({len(todos)}, orphaned: [yellow]{orphaned_todos}[/])[/]"
        )

        self._selected_debug.clear()
        self._selected_todo.clear()

        dt = self.query_one("#debug-table", DataTable)
        dt.clear()
        if debug:
            gi, rd = self._populate_grouped_table(
                dt, debug,
                key_fn=lambda d: d.name.split(".")[0],
            )
            self._debug_group_items = gi
            self._debug_row_display = rd
        else:
            dt.add_row(f"[dim]{t('dt.none')}[/]", "", "")
            self._debug_group_items = {}
            self._debug_row_display = {}

        tt = self.query_one("#todo-table", DataTable)
        tt.clear()
        if todos:
            gi, rd = self._populate_grouped_table(
                tt, todos,
                key_fn=lambda td: td.name.split("-agent-")[0] if "-agent-" in td.name else td.name.split(".")[0],
            )
            self._todo_group_items = gi
            self._todo_row_display = rd
        else:
            tt.add_row(f"[dim]{t('dt.none')}[/]", "", "")
            self._todo_group_items = {}
            self._todo_row_display = {}

        self._render_debug_actions()
        self._render_todo_actions()

    def _matches_query(self, name: str, entry) -> bool:
        session_id = name.split("-agent-")[0] if "-agent-" in name else name.split(".")[0]
        project = self._session_to_project.get(session_id, "")
        haystack = f"{name} {project}".casefold()
        return self._filter_query.casefold() in haystack

    def _populate_grouped_table(self, table: DataTable, entries, key_fn):
        """Populate a DataTable with entries grouped by project.

        Returns (group_items, row_display) for group delete and multi-select.
        """
        from collections import defaultdict
        groups: dict[str, list] = defaultdict(list)
        orphaned = []
        group_items: dict[str, list[str]] = {}
        row_display: dict[str, str] = {}

        for e in entries:
            if e.is_orphaned:
                orphaned.append(e)
            else:
                session_id = key_fn(e)
                project = self._session_to_project.get(session_id, session_id)
                groups[project].append(e)

        if self._sort_mode == "size":
            orphaned.sort(key=lambda e: (-e.size_bytes, e.name.casefold()))
        else:
            orphaned.sort(key=lambda e: e.name.casefold())

        # Orphaned group first
        if orphaned:
            hdr = "__hdr_orphaned__"
            table.add_row(
                f"[bold yellow]── Orphaned ({len(orphaned)})[/]", "", "",
                key=hdr,
            )
            group_items[hdr] = [e.name for e in orphaned]
            for i, e in enumerate(orphaned, 1):
                display = f"[yellow]Orphan {i}[/]"
                row_display[e.name] = display
                table.add_row(
                    f"  {display}",
                    format_bytes(e.size_bytes),
                    t("common.orphaned"),
                    key=e.name,
                )

        # Active groups by project
        if self._sort_mode == "size":
            group_names = sorted(
                groups.keys(),
                key=lambda project: (
                    -sum(entry.size_bytes for entry in groups[project]),
                    project.casefold(),
                ),
            )
        else:
            group_names = sorted(groups.keys(), key=str.casefold)

        for project in group_names:
            items = groups[project]
            if self._sort_mode == "size":
                items = sorted(items, key=lambda e: (-e.size_bytes, e.name.casefold()))
            elif self._sort_mode == "status":
                items = sorted(items, key=lambda e: (e.is_orphaned, e.name.casefold()))
            else:
                items = sorted(items, key=lambda e: e.name.casefold())
            total_size = sum(e.size_bytes for e in items)
            hdr = f"__hdr_{project}__"
            table.add_row(
                f"[bold cyan]── {project} ({len(items)})[/]",
                f"[dim]{format_bytes(total_size)}[/]", "",
                key=hdr,
            )
            group_items[hdr] = [e.name for e in items]
            for i, e in enumerate(items, 1):
                display = f"Session {i}"
                row_display[e.name] = display
                table.add_row(
                    f"  {display}",
                    format_bytes(e.size_bytes),
                    t("common.active"),
                    key=e.name,
                )

        return group_items, row_display

    def action_toggle_select(self) -> None:
        """Toggle selection on the current row (spacebar)."""
        focused = self.app.focused
        if focused is None:
            return
        table_id = getattr(focused, "id", "")
        if table_id == "debug-table":
            selected = self._selected_debug
            row_display = self._debug_row_display
        elif table_id == "todo-table":
            selected = self._selected_todo
            row_display = self._todo_row_display
        else:
            return
        table = focused
        if table.cursor_row is None or table.row_count == 0:
            return
        row_key = list(table.rows.keys())[table.cursor_row]
        name = row_key.value
        if name.startswith("__hdr_") or name not in row_display:
            return
        name_col = list(table.columns.keys())[0]
        if name in selected:
            selected.discard(name)
            table.update_cell(row_key, name_col, f"  {row_display[name]}")
        else:
            selected.add(name)
            table.update_cell(row_key, name_col, f"  [bold green]●[/] {row_display[name]}")
        self._render_debug_actions()
        self._render_todo_actions()

    def _get_focused_kind(self) -> str | None:
        """Return 'debug' or 'todo' based on focused table."""
        focused = self.app.focused
        table_id = getattr(focused, "id", "") if focused else ""
        if table_id == "debug-table":
            return "debug"
        if table_id == "todo-table":
            return "todo"
        return None

    def action_trash_focused(self) -> None:
        """Trash selected/cursor item (d key)."""
        kind = self._get_focused_kind()
        if kind == "debug":
            self._handle_action("trash-debug")
        elif kind == "todo":
            self._handle_action("trash-todo")

    def action_trash_orphaned_all(self) -> None:
        """Trash all orphaned items (D key)."""
        kind = self._get_focused_kind()
        if kind == "debug":
            self._handle_action("trash-orphaned-debug")
        elif kind == "todo":
            self._handle_action("trash-orphaned-todo")

    def action_prune_empty(self) -> None:
        """Prune empty files (p key)."""
        kind = self._get_focused_kind()
        if kind == "debug":
            self._handle_action("prune-debug")
        elif kind == "todo":
            self._handle_action("prune-todo")

    def _render_debug_actions(self) -> None:
        sel = len(self._selected_debug)
        trash_label = t("dt.btn_trash_debug") + (f" ({sel})" if sel else "")
        sort_labels = {
            "project": t("dt.sort_project"),
            "size": t("dt.sort_size"),
            "status": t("dt.sort_status"),
        }
        actions = [
            ("sort-dt", t("dt.btn_sort", mode=sort_labels[self._sort_mode]), "#555555"),
            ("trash-debug", trash_label, "#e8890c"),
        ]
        if self._empty_debug > 0:
            actions.append(("prune-debug", t("dt.btn_prune_debug", count=self._empty_debug), "#0178d4"))
        if len(self._orphaned_debug_names) > 0:
            actions.append(("trash-orphaned-debug", t("dt.btn_trash_orphaned_debug", count=len(self._orphaned_debug_names)), "#ba3c5b"))
        bar = self.query_one("#debug-actions", ActionBar)
        bar.set_actions(actions, on_action=self._handle_action)

    def _render_todo_actions(self) -> None:
        sel = len(self._selected_todo)
        trash_label = t("dt.btn_trash_todo") + (f" ({sel})" if sel else "")
        sort_labels = {
            "project": t("dt.sort_project"),
            "size": t("dt.sort_size"),
            "status": t("dt.sort_status"),
        }
        actions = [
            ("sort-dt", t("dt.btn_sort", mode=sort_labels[self._sort_mode]), "#555555"),
            ("trash-todo", trash_label, "#e8890c"),
        ]
        if self._empty_todo > 0:
            actions.append(("prune-todo", t("dt.btn_prune_todo", count=self._empty_todo), "#0178d4"))
        if len(self._orphaned_todo_names) > 0:
            actions.append(("trash-orphaned-todo", t("dt.btn_trash_orphaned_todo", count=len(self._orphaned_todo_names)), "#ba3c5b"))
        bar = self.query_one("#todo-actions", ActionBar)
        bar.set_actions(actions, on_action=self._handle_action)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Show file content preview when a row is highlighted."""
        if not event.row_key:
            return
        name = event.row_key.value
        entry = self._debug_entries.get(name) or self._todo_entries.get(name)
        if not entry:
            return
        self.run_worker(lambda: self._read_preview(entry.path, entry.name), thread=True)

    def _read_preview(self, file_path: str, name: str) -> None:
        """Read file content for preview."""
        try:
            p = Path(file_path)
            if p.is_file():
                content = p.read_text(errors="replace")[:2000]
            elif p.is_dir():
                content = self._preview_task_dir(p)
            else:
                content = "(not found)"
        except OSError as e:
            content = f"(read error: {e})"
        self.app.call_from_thread(self._show_preview, name, content)

    def _preview_task_dir(self, p: Path) -> str:
        """Render a tasks/<sessionId>/ directory as a checklist of task items."""
        import json

        task_files = sorted(
            (f for f in p.iterdir() if f.is_file() and f.suffix == ".json"),
            key=lambda f: int(f.stem) if f.stem.isdigit() else 0,
        )
        if not task_files:
            return "[]"
        status_mark = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}
        lines = [f"{len(task_files)} task(s):", ""]
        for f in task_files[:50]:
            try:
                data = json.loads(f.read_text(errors="replace"))
            except (json.JSONDecodeError, OSError):
                continue
            mark = status_mark.get(data.get("status", ""), "[ ]")
            subject = data.get("subject", "") or data.get("activeForm", "") or f.name
            lines.append(f"  {mark} {subject}")
        return "\n".join(lines)

    def _show_preview(self, name: str, content: str) -> None:
        from rich.text import Text
        body = self.query_one("#dt-preview-body", Static)
        text = Text()
        text.append(name, style="bold")
        text.append("\n" + "─" * 40 + "\n")
        stripped = content.strip()
        if stripped in ("[]", "{}", ""):
            empty_text = t("dt.empty_file")
            lines = empty_text.split("\n")
            text.append(lines[0] + "\n", style="dim")
            if len(lines) > 1:
                text.append(lines[1], style="dim italic")
        else:
            text.append(content)
        body.update(text)

    def _handle_action(self, action_id: str) -> None:
        if action_id == "sort-dt":
            self._sort_mode = {
                "project": "size",
                "size": "status",
                "status": "project",
            }[self._sort_mode]
            self._update()
        elif action_id == "trash-debug":
            # Multi-select mode
            if self._selected_debug:
                names = list(self._selected_debug)
                self.app.push_screen(
                    ConfirmScreen(t("confirm.bulk_delete", count=len(names), type="debug")),
                    callback=lambda ok, ns=names: self._do_trash_bulk("debug", ns) if ok else None,
                )
                return
            table = self.query_one("#debug-table", DataTable)
            if table.cursor_row is not None and table.row_count > 0:
                row_key = list(table.rows.keys())[table.cursor_row]
                name = row_key.value
                # Group delete
                if name.startswith("__hdr_"):
                    items = self._debug_group_items.get(name, [])
                    if items:
                        self.app.push_screen(
                            ConfirmScreen(t("confirm.group_delete", count=len(items), type="debug")),
                            callback=lambda ok, ns=items: self._do_trash_bulk("debug", ns) if ok else None,
                        )
                    return
                self.app.push_screen(
                    ConfirmScreen(t("dt.confirm_debug", name=name)),
                    callback=lambda ok, n=name: self._trash_debug(n) if ok else None,
                )
        elif action_id == "trash-todo":
            # Multi-select mode
            if self._selected_todo:
                names = list(self._selected_todo)
                self.app.push_screen(
                    ConfirmScreen(t("confirm.bulk_delete", count=len(names), type="todo")),
                    callback=lambda ok, ns=names: self._do_trash_bulk("todo", ns) if ok else None,
                )
                return
            table = self.query_one("#todo-table", DataTable)
            if table.cursor_row is not None and table.row_count > 0:
                row_key = list(table.rows.keys())[table.cursor_row]
                name = row_key.value
                # Group delete
                if name.startswith("__hdr_"):
                    items = self._todo_group_items.get(name, [])
                    if items:
                        self.app.push_screen(
                            ConfirmScreen(t("confirm.group_delete", count=len(items), type="todo")),
                            callback=lambda ok, ns=items: self._do_trash_bulk("todo", ns) if ok else None,
                        )
                    return
                self.app.push_screen(
                    ConfirmScreen(t("dt.confirm_todo", name=name)),
                    callback=lambda ok, n=name: self._trash_todo(n) if ok else None,
                )
        elif action_id == "prune-debug":
            count = self._empty_debug
            if count == 0:
                self.app.notify(t("common.no_items"))
                return
            self.app.push_screen(
                ConfirmScreen(t("dt.confirm_prune_debug", count=count)),
                callback=lambda ok: self._do_prune_debug() if ok else None,
            )
        elif action_id == "prune-todo":
            count = self._empty_todo
            if count == 0:
                self.app.notify(t("common.no_items"))
                return
            self.app.push_screen(
                ConfirmScreen(t("dt.confirm_prune_todo", count=count)),
                callback=lambda ok: self._do_prune_todo() if ok else None,
            )
        elif action_id == "trash-orphaned-debug":
            names = getattr(self, "_orphaned_debug_names", [])
            if not names:
                self.app.notify(t("common.no_items"))
                return
            self.app.push_screen(
                ConfirmScreen(t("dt.confirm_trash_orphaned_debug", count=len(names))),
                callback=lambda ok: self._do_trash_orphaned_debug() if ok else None,
            )
        elif action_id == "trash-orphaned-todo":
            names = getattr(self, "_orphaned_todo_names", [])
            if not names:
                self.app.notify(t("common.no_items"))
                return
            self.app.push_screen(
                ConfirmScreen(t("dt.confirm_trash_orphaned_todo", count=len(names))),
                callback=lambda ok: self._do_trash_orphaned_todo() if ok else None,
            )

    def _trash_debug(self, name: str) -> None:
        def _work():
            if trash_debug_file(name):
                self.app.call_from_thread(self.app.notify, t("common.trashed", name=name))
            else:
                self.app.call_from_thread(self.app.notify, t("common.failed"), severity="error")
            self.app.call_from_thread(self.refresh_data)
        self.run_worker(_work, thread=True)

    def _trash_todo(self, name: str) -> None:
        def _work():
            if trash_todo_file(name):
                self.app.call_from_thread(self.app.notify, t("common.trashed", name=name))
            else:
                self.app.call_from_thread(self.app.notify, t("common.failed"), severity="error")
            self.app.call_from_thread(self.refresh_data)
        self.run_worker(_work, thread=True)

    def _do_prune_debug(self) -> None:
        def _work():
            ok, fail = prune_empty_debug_files()
            self.app.call_from_thread(self.app.notify, t("dt.prune_ok", ok=ok, fail=fail))
            self.app.call_from_thread(self.refresh_data)
        self.run_worker(_work, thread=True)

    def _do_prune_todo(self) -> None:
        def _work():
            ok, fail = prune_empty_todo_files()
            self.app.call_from_thread(self.app.notify, t("dt.prune_ok", ok=ok, fail=fail))
            self.app.call_from_thread(self.refresh_data)
        self.run_worker(_work, thread=True)

    def _do_trash_bulk(self, kind: str, names: list[str]) -> None:
        """Bulk delete for multi-select and group delete."""
        def _work():
            if kind == "debug":
                ok, fail = trash_debug_files(names)
            else:
                ok, fail = trash_todo_files(names)
            self.app.call_from_thread(self._on_bulk_done, kind, ok, fail)
        self.run_worker(_work, thread=True)

    def _on_bulk_done(self, kind: str, ok: int, fail: int) -> None:
        if kind == "debug":
            self._selected_debug.clear()
        else:
            self._selected_todo.clear()
        self.app.notify(t("common.trash_bulk_ok", ok=ok, fail=fail))
        self.refresh_data()

    def _do_trash_orphaned_debug(self) -> None:
        names = list(self._orphaned_debug_names)
        def _work():
            ok, fail = trash_debug_files(names)
            self.app.call_from_thread(self.app.notify, t("common.trash_bulk_ok", ok=ok, fail=fail))
            self.app.call_from_thread(self.refresh_data)
        self.run_worker(_work, thread=True)

    def _do_trash_orphaned_todo(self) -> None:
        names = list(self._orphaned_todo_names)
        def _work():
            ok, fail = trash_todo_files(names)
            self.app.call_from_thread(self.app.notify, t("common.trash_bulk_ok", ok=ok, fail=fail))
            self.app.call_from_thread(self.refresh_data)
        self.run_worker(_work, thread=True)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "dt-filter":
            return
        self._filter_query = event.value.strip()
        self._update()
