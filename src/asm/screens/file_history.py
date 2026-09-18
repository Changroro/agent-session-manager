"""File history management screen."""

import re

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Input, Static

from asm.models import decode_path_hint, encode_path
from asm.screens.confirm import ConfirmScreen
from asm.services.claude_data import get_file_history, get_project_paths, get_session_to_project_map
from asm.i18n import t
from asm.services.cleaner import trash_file_history, trash_file_histories
from asm.widgets.action_bar import ActionBar

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _display_name(dir_name: str, encoded_to_path: dict) -> str:
    """Convert dir_name to a readable display name."""
    actual = encoded_to_path.get(dir_name)
    if actual:
        if isinstance(actual, list):
            return f"[ambiguous] {' | '.join(actual)}"
        return actual
    if _UUID_RE.match(dir_name):
        return f"[dim]Session:[/] {dir_name[:8]}..."
    return decode_path_hint(dir_name)


class FileHistoryPane(Container):
    """View and manage file history entries."""

    BINDINGS = [
        ("space", "toggle_select", "Select"),
        ("d", "trash_selected", "Trash"),
        ("D", "trash_orphaned", "Trash Orphaned"),
    ]

    CSS = """
    FileHistoryPane {
        height: 1fr;
        padding: 1;
    }
    #fh-info {
        height: auto;
        margin-bottom: 1;
        color: $text-muted;
    }
    #fh-layout {
        height: 1fr;
    }
    #fh-filter {
        margin-bottom: 1;
    }
    #fh-list-panel {
        width: 1fr;
        height: 1fr;
    }
    #fh-actions {
        height: 1;
        margin-top: 1;
    }
    #fh-detail-panel {
        width: 1fr;
        height: 1fr;
        max-width: 50%;
        border-left: tall $primary;
        padding: 0 1;
    }
    #fh-detail {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(
            t("fh.info"),
            id="fh-info",
        )
        yield Input(placeholder=t("fh.filter_placeholder"), id="fh-filter")
        with Horizontal(id="fh-layout"):
            with Vertical(id="fh-list-panel"):
                yield DataTable(id="fh-table")
                yield ActionBar(id="fh-actions")
            with Vertical(id="fh-detail-panel"):
                yield VerticalScroll(
                    Static(t("fh.select_hint"), id="fh-detail-body"),
                    id="fh-detail",
                )

    def on_mount(self) -> None:
        self._entries: dict = {}
        table = self.query_one("#fh-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.styles.height = "1fr"
        table.add_columns("Project / Session", "Status")
        self._orphaned_fh_names = []
        self._selected: set[str] = set()
        self._row_display: dict[str, str] = {}
        self._group_items: dict[str, list[str]] = {}
        self._filter_query = ""
        self._sort_mode = "project"
        self._all_entries = []
        self._encoded_to_path = {}
        self._session_to_project = {}
        self.refresh_data()

    def refresh_data(self) -> None:
        self.run_worker(self._load, thread=True)

    def _load(self) -> None:
        entries = get_file_history()
        project_paths = get_project_paths()
        encoded_to_path: dict[str, list[str]] = {}
        for path in project_paths:
            encoded_to_path.setdefault(encode_path(path), []).append(path)
        if self.app.target_path:
            target_enc = self.app.target_encoded
            entries = [e for e in entries if e.dir_name == target_enc]
        session_to_project = get_session_to_project_map()
        self.app.call_from_thread(self._set_entries, entries, encoded_to_path, session_to_project)

    def _set_entries(self, entries, encoded_to_path: dict, session_to_project: dict = None) -> None:
        self._all_entries = list(entries)
        self._encoded_to_path = encoded_to_path
        self._session_to_project = session_to_project or {}
        self._render_table()

    def _render_table(self) -> None:
        entries = list(self._all_entries)
        query = self._filter_query.casefold()
        if query:
            filtered = []
            for entry in entries:
                project = self._session_to_project.get(entry.dir_name, "")
                display = _display_name(entry.dir_name, self._encoded_to_path)
                haystack = " ".join((entry.dir_name, project, display)).casefold()
                if query in haystack:
                    filtered.append(entry)
            entries = filtered

        if self._sort_mode == "session":
            entries.sort(key=lambda e: _display_name(e.dir_name, self._encoded_to_path).casefold())
        elif self._sort_mode == "status":
            entries.sort(key=lambda e: (not e.is_orphaned, _display_name(e.dir_name, self._encoded_to_path).casefold()))
        else:
            entries.sort(
                key=lambda e: (
                    self._session_to_project.get(e.dir_name, _display_name(e.dir_name, self._encoded_to_path)).casefold(),
                    _display_name(e.dir_name, self._encoded_to_path).casefold(),
                )
            )

        table = self.query_one("#fh-table", DataTable)
        table.clear()
        self._entries = {e.dir_name: e for e in entries}
        self._orphaned_fh_names = [e.dir_name for e in entries if e.is_orphaned]
        self._selected.clear()
        self._row_display = {}
        self._group_items = {}

        # Group by project
        from collections import defaultdict
        groups: dict[str, list] = defaultdict(list)
        orphaned = []
        for e in entries:
            if e.is_orphaned:
                orphaned.append(e)
            else:
                project = self._session_to_project.get(e.dir_name)
                display = project if project else _display_name(e.dir_name, self._encoded_to_path)
                groups[display].append(e)

        if orphaned:
            hdr = "__hdr_orphaned__"
            table.add_row(
                f"[bold yellow]── Orphaned ({len(orphaned)})[/]", "",
                key=hdr,
            )
            self._group_items[hdr] = [e.dir_name for e in orphaned]
            for i, e in enumerate(orphaned, 1):
                display = f"[yellow]Orphan {i}[/]"
                self._row_display[e.dir_name] = display
                table.add_row(f"  {display}", t("common.orphaned"), key=e.dir_name)

        for project in sorted(groups.keys()):
            items = groups[project]
            hdr = f"__hdr_{project}__"
            table.add_row(
                f"[bold cyan]── {project} ({len(items)})[/]", "",
                key=hdr,
            )
            self._group_items[hdr] = [e.dir_name for e in items]
            for i, e in enumerate(items, 1):
                display = f"Session {i}"
                self._row_display[e.dir_name] = display
                table.add_row(f"  {display}", t("common.active"), key=e.dir_name)
        self._render_actions()

    def action_toggle_select(self) -> None:
        """Toggle selection on the current row (spacebar)."""
        table = self.query_one("#fh-table", DataTable)
        if table.cursor_row is None or table.row_count == 0:
            return
        row_key = list(table.rows.keys())[table.cursor_row]
        name = row_key.value
        if name.startswith("__hdr_") or name not in self._row_display:
            return
        name_col = list(table.columns.keys())[0]
        if name in self._selected:
            self._selected.discard(name)
            table.update_cell(row_key, name_col, f"  {self._row_display[name]}")
        else:
            self._selected.add(name)
            table.update_cell(row_key, name_col, f"  [bold green]●[/] {self._row_display[name]}")
        self._render_actions()

    def _render_actions(self) -> None:
        """Render action bar with clickable buttons."""
        sel = len(self._selected)
        trash_label = t("fh.btn_trash") + (f" ({sel})" if sel else "")
        sort_labels = {
            "project": t("fh.sort_project"),
            "status": t("fh.sort_status"),
            "session": t("fh.sort_session"),
        }
        actions = [
            ("sort-fh", t("fh.btn_sort", mode=sort_labels[self._sort_mode]), "#555555"),
            ("trash-selected", trash_label, "#e8890c"),
        ]
        count = len(getattr(self, "_orphaned_fh_names", []))
        if count > 0:
            actions.append(("trash-orphaned", t("fh.btn_trash_orphaned", count=count), "#ba3c5b"))
        bar = self.query_one("#fh-actions", ActionBar)
        bar.set_actions(actions, on_action=self._handle_action)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not event.row_key or event.row_key.value.startswith("__hdr_"):
            return
        if event.row_key.value in self._entries:
            e = self._entries[event.row_key.value]
            body = self.query_one("#fh-detail-body", Static)

            project = self._session_to_project.get(e.dir_name)

            if e.is_orphaned:
                status_msg = t("fh.status_orphaned_desc")
            else:
                status_msg = t("fh.status_active_desc")

            detail = f"[bold]Session ID:[/]\n  [dim]{e.dir_name}[/]\n\n"
            if project:
                detail += f"[bold]Project:[/]\n  {project}\n\n"
            detail += f"[bold]Status:[/] {status_msg}"
            body.update(detail)

    def _handle_action(self, action_id: str) -> None:
        """Handle action bar button clicks."""
        if action_id == "sort-fh":
            self._sort_mode = {
                "project": "status",
                "status": "session",
                "session": "project",
            }[self._sort_mode]
            self._render_table()
        elif action_id == "trash-selected":
            self._click_trash_selected()
        elif action_id == "trash-orphaned":
            self._click_trash_orphaned()

    def _click_trash_selected(self) -> None:
        # Multi-select mode
        if self._selected:
            names = list(self._selected)
            self.app.push_screen(
                ConfirmScreen(t("confirm.bulk_delete", count=len(names), type="file history")),
                callback=lambda ok, ns=names: self._do_trash_bulk(ns) if ok else None,
            )
            return
        table = self.query_one("#fh-table", DataTable)
        if table.cursor_row is not None and table.row_count > 0:
            row_key = list(table.rows.keys())[table.cursor_row]
            name = row_key.value
            # Group delete
            if name.startswith("__hdr_"):
                items = self._group_items.get(name, [])
                if items:
                    self.app.push_screen(
                        ConfirmScreen(t("confirm.group_delete", count=len(items), type="file history")),
                        callback=lambda ok, ns=items: self._do_trash_bulk(ns) if ok else None,
                    )
                return
            display = _display_name(name, self._encoded_to_path)
            self.app.push_screen(
                ConfirmScreen(
                    t("fh.confirm_trash", name=display)
                ),
                callback=lambda ok, n=name: self._do_trash(n) if ok else None,
            )

    def _click_trash_orphaned(self) -> None:
        names = getattr(self, "_orphaned_fh_names", [])
        if not names:
            self.app.notify(t("common.no_items"))
            return
        self.app.push_screen(
            ConfirmScreen(t("fh.confirm_trash_orphaned", count=len(names))),
            callback=lambda ok: self._do_trash_orphaned() if ok else None,
        )

    def _do_trash(self, name: str) -> None:
        def _work():
            if trash_file_history(name):
                self.app.call_from_thread(self.app.notify, t("common.trashed", name=name))
            else:
                self.app.call_from_thread(self.app.notify, t("common.failed"), severity="error")
            self.app.call_from_thread(self.refresh_data)
        self.run_worker(_work, thread=True)

    def action_trash_selected(self) -> None:
        self._click_trash_selected()

    def action_trash_orphaned(self) -> None:
        self._click_trash_orphaned()

    def _do_trash_bulk(self, names: list[str]) -> None:
        def _work():
            ok, fail = trash_file_histories(names)
            self.app.call_from_thread(self._on_bulk_done, ok, fail)
        self.run_worker(_work, thread=True)

    def _on_bulk_done(self, ok: int, fail: int) -> None:
        self._selected.clear()
        self.app.notify(t("common.trash_bulk_ok", ok=ok, fail=fail))
        self.refresh_data()

    def _do_trash_orphaned(self) -> None:
        names = list(self._orphaned_fh_names)
        def _work():
            ok, fail = trash_file_histories(names)
            self.app.call_from_thread(self.app.notify, t("common.trash_bulk_ok", ok=ok, fail=fail))
            self.app.call_from_thread(self.refresh_data)
        self.run_worker(_work, thread=True)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "fh-filter":
            return
        self._filter_query = event.value.strip()
        self._render_table()
