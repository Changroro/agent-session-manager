"""Backups management screen."""

from datetime import datetime

from textual.app import ComposeResult
from textual.containers import Container, Vertical
from textual.widgets import DataTable, Input, Static

from asm.i18n import t
from asm.models import CLAUDE_DIR, CODEX_DIR, CODEX_SESSIONS_DIR, PLUGINS_DIR, PROJECTS_DIR, SKILLS_DIR
from asm.utils import dir_size
from asm.screens.confirm import ConfirmScreen
from asm.services.backup import (
    create_codex_backup,
    create_config_backup,
    create_full_backup,
    create_plugins_backup,
    create_sessions_backup,
    create_settings_backup,
    delete_backup,
    detect_symlinks,
    export_backup,
    import_backup,
    list_backups,
    restore_codex_backup,
    restore_config_backup,
    restore_full_backup,
    restore_plugins_backup,
    restore_sessions_backup,
    restore_settings_backup,
)
from asm.services.recovery import (
    delete_recovery_item,
    list_recovery_items,
    restore_recovery_item,
)
from asm.utils import format_bytes
from asm.widgets.action_bar import ActionBar


class BackupsPane(Container):
    """View, create, restore, and manage backups."""

    BINDINGS = [
        ("space", "toggle_select", "Select"),
        ("c", "config_backup", "Config Backup"),
        ("b", "full_backup", "Full Backup"),
        ("s", "settings_backup", "Settings Backup"),
        ("p", "plugins_backup", "Plugins Backup"),
        ("S", "sessions_backup", "Sessions Backup"),
        ("R", "restore_backup", "Restore"),
        ("d", "delete_backup", "Delete"),
        ("e", "export_backup", "Export"),
        ("i", "import_backup", "Import"),
    ]

    CSS = """
    BackupsPane {
        height: 1fr;
        padding: 1;
    }
    #backups-info {
        height: auto;
        margin-bottom: 1;
        color: $text-muted;
    }
    #backups-filter {
        margin-bottom: 1;
    }
    #backup-create-actions {
        height: 1;
        margin-bottom: 0;
    }
    #backup-manage-actions {
        height: 1;
        margin-bottom: 1;
    }
    #backups-table {
        height: 1fr;
        min-height: 8;
    }
    #recovery-section {
        height: 1fr;
        margin-top: 1;
        border-top: tall $primary;
        padding-top: 1;
    }
    #recovery-title {
        height: auto;
        text-style: bold;
    }
    #recovery-info {
        height: auto;
        color: $text-muted;
        margin-bottom: 1;
    }
    #recovery-actions {
        height: 1;
        margin-bottom: 1;
    }
    #recovery-table {
        height: 1fr;
        min-height: 8;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(t("bak.info"), id="backups-info")
        yield Input(placeholder=t("bak.filter_placeholder"), id="backups-filter")
        yield ActionBar(id="backup-create-actions")
        yield ActionBar(id="backup-manage-actions")
        yield DataTable(id="backups-table")
        with Vertical(id="recovery-section"):
            yield Static(t("bak.recovery_title"), id="recovery-title")
            yield Static(t("bak.recovery_info"), id="recovery-info")
            yield ActionBar(id="recovery-actions")
            yield DataTable(id="recovery-table")

    def on_mount(self) -> None:
        self._backups: dict[str, object] = {}
        self._selected: set[str] = set()
        self._row_display: dict[str, str] = {}
        self._recovery_items: dict[str, object] = {}
        self._filter_query = ""
        self._backup_sort_mode = "newest"
        self._recovery_sort_mode = "newest"
        self._all_backups = []
        self._all_recovery_items = []

        table = self.query_one("#backups-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Name", "Created", "Size", "Path")

        recovery_table = self.query_one("#recovery-table", DataTable)
        recovery_table.cursor_type = "row"
        recovery_table.zebra_stripes = True
        recovery_table.add_columns("Name", "Created", "Type", "Status", "Original Path")

        self._render_actions()
        self.refresh_data()

    # ── Actions bar ──────────────────────────────────────────

    def _render_actions(self) -> None:
        create_actions = [
            ("config-backup", t("bak.btn_config"), "#0178d4"),
            ("settings-backup", t("bak.btn_settings"), "#0178d4"),
            ("plugins-backup", t("bak.btn_plugins"), "#0178d4"),
            ("sessions-backup", t("bak.btn_sessions"), "#0178d4"),
            ("full-backup", t("bak.btn_full"), "#0178d4"),
        ]
        from asm.services import codex_data
        if codex_data.is_available():
            create_actions.append(("codex-backup", t("bak.btn_codex"), "#10A37F"))
        self.query_one("#backup-create-actions", ActionBar).set_actions(
            create_actions, on_action=self._handle_action
        )

        backup_sort_labels = {
            "newest": t("bak.sort_newest"),
            "largest": t("bak.sort_largest"),
            "type": t("bak.sort_type"),
        }
        sel = len(self._selected)
        del_label = t("bak.btn_delete") + (f" ({sel})" if sel else "")
        manage_actions = [
            ("sort-backups", t("bak.btn_sort", mode=backup_sort_labels[self._backup_sort_mode]), "#555555"),
            ("restore", t("bak.btn_restore"), "#4EBF71"),
            ("delete", del_label, "#ba3c5b"),
            ("export", t("bak.btn_export"), "#8B5CF6"),
            ("import", t("bak.btn_import"), "#8B5CF6"),
        ]
        self.query_one("#backup-manage-actions", ActionBar).set_actions(
            manage_actions, on_action=self._handle_action
        )

        recovery_sort_labels = {
            "newest": t("bak.sort_newest"),
            "largest": t("bak.sort_largest"),
            "type": t("bak.sort_type"),
        }
        recovery_actions = [
            ("sort-recovery", t("bak.btn_sort", mode=recovery_sort_labels[self._recovery_sort_mode]), "#555555"),
            ("restore-recovery", t("bak.btn_recovery_restore"), "#4EBF71"),
            ("restore-recovery-overwrite", t("bak.btn_recovery_overwrite"), "#e8890c"),
            ("delete-recovery", t("bak.btn_recovery_delete"), "#ba3c5b"),
        ]
        self.query_one("#recovery-actions", ActionBar).set_actions(
            recovery_actions, on_action=self._handle_action
        )

    # ── Data loading ─────────────────────────────────────────

    def refresh_data(self) -> None:
        self.run_worker(self._load, thread=True)

    def _load(self) -> None:
        backups = list_backups()
        recovery_items = list_recovery_items()
        self.app.call_from_thread(self._set_data, backups, recovery_items)

    def _set_data(self, backups, recovery_items) -> None:
        self._all_backups = list(backups)
        self._all_recovery_items = list(recovery_items)
        self._selected.clear()
        self._render_tables()

    def _render_tables(self) -> None:
        self._render_backups_table()
        self._render_recovery_table()
        self._render_actions()

    def _render_backups_table(self) -> None:
        backups = self._filtered_backups()
        table = self.query_one("#backups-table", DataTable)
        table.clear()
        self._backups = {b.name: b for b in backups}
        self._row_display = {}
        for b in backups:
            created = (
                datetime.fromtimestamp(b.created).strftime("%Y-%m-%d %H:%M:%S")
                if b.created
                else "N/A"
            )
            display = b.name
            self._row_display[b.name] = display
            table.add_row(display, created, format_bytes(b.size_bytes), b.path, key=b.name)

    def _filtered_backups(self):
        query = self._filter_query.casefold()
        backups = [
            b for b in self._all_backups
            if not query
            or query in " ".join((b.name, b.backup_type, b.path)).casefold()
        ]
        if self._backup_sort_mode == "largest":
            backups.sort(key=lambda b: (-b.size_bytes, -b.created, b.name.casefold()))
        elif self._backup_sort_mode == "type":
            backups.sort(key=lambda b: (b.backup_type, -b.created, b.name.casefold()))
        else:
            backups.sort(key=lambda b: (-b.created, b.name.casefold()))
        return backups

    def _render_recovery_table(self) -> None:
        items = self._filtered_recovery_items()
        table = self.query_one("#recovery-table", DataTable)
        table.clear()
        self._recovery_items = {item.id: item for item in items}
        if not items:
            table.add_row(t("bak.recovery_none"), "", "", "", "", key="__none__")
            return

        for item in items:
            created = (
                datetime.fromtimestamp(item.created).strftime("%Y-%m-%d %H:%M:%S")
                if item.created
                else "N/A"
            )
            status = (
                t("bak.recovery_status_exists")
                if item.original_exists
                else t("bak.recovery_status_ready")
            )
            table.add_row(
                f"{item.name} [{format_bytes(item.size_bytes)}]",
                created,
                item.category,
                status,
                item.original_path,
                key=item.id,
            )

    def _filtered_recovery_items(self):
        query = self._filter_query.casefold()
        items = [
            item for item in self._all_recovery_items
            if not query
            or query in " ".join((item.name, item.category, item.original_path)).casefold()
        ]
        if self._recovery_sort_mode == "largest":
            items.sort(key=lambda item: (-item.size_bytes, -item.created, item.name.casefold()))
        elif self._recovery_sort_mode == "type":
            items.sort(key=lambda item: (item.category, -item.created, item.name.casefold()))
        else:
            items.sort(key=lambda item: (-item.created, item.name.casefold()))
        return items

    # ── Selection ────────────────────────────────────────────

    def _get_selected_backup(self):
        table = self.query_one("#backups-table", DataTable)
        if table.cursor_row is not None and table.row_count > 0:
            row_key = list(table.rows.keys())[table.cursor_row]
            return self._backups.get(row_key.value)
        return None

    def _get_selected_recovery(self):
        table = self.query_one("#recovery-table", DataTable)
        if table.cursor_row is not None and table.row_count > 0:
            row_key = list(table.rows.keys())[table.cursor_row]
            return self._recovery_items.get(row_key.value)
        return None

    def action_toggle_select(self) -> None:
        """Toggle selection on the current row (spacebar)."""
        if getattr(self.app.focused, "id", "") != "backups-table":
            return
        table = self.query_one("#backups-table", DataTable)
        if table.cursor_row is None or table.row_count == 0:
            return
        row_key = list(table.rows.keys())[table.cursor_row]
        name = row_key.value
        if name not in self._row_display:
            return
        name_col = list(table.columns.keys())[0]
        if name in self._selected:
            self._selected.discard(name)
            table.update_cell(row_key, name_col, self._row_display[name])
        else:
            self._selected.add(name)
            table.update_cell(
                row_key, name_col, f"[bold green]●[/] {self._row_display[name]}"
            )
        self._render_actions()

    # ── Keybinding actions ───────────────────────────────────

    def action_config_backup(self) -> None:
        self._handle_action("config-backup")

    def action_full_backup(self) -> None:
        self._handle_action("full-backup")

    def action_settings_backup(self) -> None:
        self._handle_action("settings-backup")

    def action_plugins_backup(self) -> None:
        self._handle_action("plugins-backup")

    def action_sessions_backup(self) -> None:
        self._handle_action("sessions-backup")

    def action_restore_backup(self) -> None:
        self._handle_action("restore")

    def action_delete_backup(self) -> None:
        self._handle_action("delete")

    def action_export_backup(self) -> None:
        self._handle_action("export")

    def action_import_backup(self) -> None:
        self._handle_action("import")

    # ── Action dispatcher ────────────────────────────────────

    def _handle_action(self, action_id: str) -> None:
        if action_id == "sort-backups":
            self._backup_sort_mode = {
                "newest": "largest",
                "largest": "type",
                "type": "newest",
            }[self._backup_sort_mode]
            self._render_tables()
        elif action_id == "sort-recovery":
            self._recovery_sort_mode = {
                "newest": "largest",
                "largest": "type",
                "type": "newest",
            }[self._recovery_sort_mode]
            self._render_tables()
        elif action_id == "config-backup":
            self.run_worker(self._do_config_backup, thread=True)
        elif action_id == "full-backup":
            self.app.push_screen(
                ConfirmScreen(t("bak.confirm_full") + self._size_note(CLAUDE_DIR)),
                callback=lambda ok: self.run_worker(self._do_full_backup, thread=True)
                if ok
                else None,
            )
        elif action_id == "settings-backup":
            self.app.push_screen(
                ConfirmScreen(t("bak.confirm_settings")),
                callback=lambda ok: self.run_worker(self._do_settings_backup, thread=True)
                if ok
                else None,
            )
        elif action_id == "plugins-backup":
            self.app.push_screen(
                ConfirmScreen(t("bak.confirm_plugins") + self._size_note(PLUGINS_DIR, SKILLS_DIR)),
                callback=lambda ok: self.run_worker(self._do_plugins_backup, thread=True)
                if ok
                else None,
            )
        elif action_id == "sessions-backup":
            self.app.push_screen(
                ConfirmScreen(t("bak.confirm_sessions") + self._size_note(PROJECTS_DIR)),
                callback=lambda ok: self.run_worker(self._do_sessions_backup, thread=True)
                if ok
                else None,
            )
        elif action_id == "codex-backup":
            self.app.push_screen(
                ConfirmScreen(t("bak.confirm_codex") + self._size_note(
                    CODEX_SESSIONS_DIR,
                    CODEX_DIR / "session_index.jsonl",
                    CODEX_DIR / "history.jsonl",
                    CODEX_DIR / "config.toml",
                )),
                callback=lambda ok: self.run_worker(self._do_codex_backup, thread=True)
                if ok
                else None,
            )
        elif action_id == "restore":
            self._click_restore()
        elif action_id == "delete":
            self._click_delete()
        elif action_id == "export":
            self._click_export()
        elif action_id == "import":
            self._click_import()
        elif action_id == "restore-recovery":
            self._click_restore_recovery(overwrite=False)
        elif action_id == "restore-recovery-overwrite":
            self._click_restore_recovery(overwrite=True)
        elif action_id == "delete-recovery":
            self._click_delete_recovery()

    def _size_note(self, *paths) -> str:
        total = sum(p.stat().st_size if p.is_file() else dir_size(p) for p in paths if p.exists())
        return f"\n\n~{format_bytes(total)} will be copied."

    # ── Create workers ───────────────────────────────────────

    def _do_config_backup(self) -> None:
        path = create_config_backup()
        if path:
            self.app.call_from_thread(self.app.notify, t("bak.config_created", path=path))
        else:
            self.app.call_from_thread(self.app.notify, t("bak.no_source"), severity="warning")
        self.app.call_from_thread(self.refresh_data)

    def _do_full_backup(self) -> None:
        self.app.call_from_thread(self.app.notify, t("bak.full_creating"))
        path = create_full_backup()
        if path:
            self.app.call_from_thread(self.app.notify, t("bak.full_created", path=path))
        else:
            self.app.call_from_thread(
                self.app.notify, t("bak.backup_failed"), severity="error"
            )
        self.app.call_from_thread(self.refresh_data)

    def _do_codex_backup(self) -> None:
        self.app.call_from_thread(self.app.notify, t("bak.codex_creating"))
        path = create_codex_backup()
        if path:
            self.app.call_from_thread(self.app.notify, t("bak.codex_created", path=path))
        else:
            self.app.call_from_thread(self.app.notify, t("bak.no_source"), severity="warning")
        self.app.call_from_thread(self.refresh_data)

    def _do_settings_backup(self) -> None:
        path = create_settings_backup()
        if path:
            self.app.call_from_thread(
                self.app.notify, t("bak.settings_created", path=path)
            )
        else:
            self.app.call_from_thread(self.app.notify, t("bak.no_source"), severity="warning")
        self.app.call_from_thread(self.refresh_data)

    def _do_plugins_backup(self) -> None:
        self.app.call_from_thread(self.app.notify, t("bak.plugins_creating"))
        path = create_plugins_backup()
        if path:
            self.app.call_from_thread(
                self.app.notify, t("bak.plugins_created", path=path)
            )
        else:
            self.app.call_from_thread(self.app.notify, t("bak.no_source"), severity="warning")
        self.app.call_from_thread(self.refresh_data)

    def _do_sessions_backup(self) -> None:
        self.app.call_from_thread(self.app.notify, t("bak.sessions_creating"))
        path = create_sessions_backup()
        if path:
            self.app.call_from_thread(
                self.app.notify, t("bak.sessions_created", path=path)
            )
        else:
            self.app.call_from_thread(self.app.notify, t("bak.no_source"), severity="warning")
        self.app.call_from_thread(self.refresh_data)

    # ── Restore backups ──────────────────────────────────────

    def _click_restore(self) -> None:
        backup = self._get_selected_backup()
        if not backup:
            return

        extra = ""
        if backup.backup_type == "plugins":
            from pathlib import Path as P

            syms = detect_symlinks(P(backup.path))
            if syms:
                items_str = "\n".join(f"  - {s}" for s in syms[:10])
                if len(syms) > 10:
                    items_str += f"\n  ... +{len(syms) - 10} more"
                extra = f"\n\n[yellow]Symlinks detected ({len(syms)}):[/]\n{items_str}\n[dim]Broken symlink targets will be warned after restore.[/]"

        msg = t("bak.confirm_restore", name=backup.name) + extra
        self.app.push_screen(
            ConfirmScreen(msg),
            callback=lambda ok, b=backup: self._do_restore(b) if ok else None,
        )

    def _do_restore(self, backup) -> None:
        def _work():
            btype = backup.backup_type
            if btype == "config":
                ok = restore_config_backup(backup.path)
                self._notify_restore(ok, backup.name)
            elif btype == "full":
                ok = restore_full_backup(backup.path)
                self._notify_restore(ok, backup.name)
            elif btype == "settings":
                ok = restore_settings_backup(backup.path)
                self._notify_restore(ok, backup.name)
            elif btype == "plugins":
                ok, broken = restore_plugins_backup(backup.path)
                if ok and broken:
                    items = "\n".join(f"  {s}" for s in broken[:10])
                    if len(broken) > 10:
                        items += f"\n  ... +{len(broken) - 10} more"
                    self.app.call_from_thread(
                        self.app.notify,
                        t(
                            "bak.symlink_warning",
                            count=len(broken),
                            items=items,
                        ),
                        severity="warning",
                        timeout=15,
                    )
                self._notify_restore(ok, backup.name)
            elif btype == "sessions":
                ok = restore_sessions_backup(backup.path)
                self._notify_restore(ok, backup.name)
            elif btype == "codex":
                ok = restore_codex_backup(backup.path)
                self._notify_restore(ok, backup.name)
            else:
                ok = restore_full_backup(backup.path) if "[full]" in backup.name else restore_config_backup(backup.path)
                self._notify_restore(ok, backup.name)
            self.app.call_from_thread(self.refresh_data)

        self.run_worker(_work, thread=True)

    def _notify_restore(self, ok: bool, name: str) -> None:
        if ok:
            self.app.call_from_thread(self.app.notify, t("bak.restored", name=name))
        else:
            self.app.call_from_thread(
                self.app.notify, t("bak.restore_failed"), severity="error"
            )

    # ── Recovery snapshots ───────────────────────────────────

    def _click_restore_recovery(self, overwrite: bool) -> None:
        item = self._get_selected_recovery()
        if not item:
            return
        key = "bak.confirm_recovery_overwrite" if overwrite else "bak.confirm_recovery_restore"
        message = (
            t(key, name=item.name, path=item.original_path)
            if not overwrite
            else t(key, name=item.name)
        )
        self.app.push_screen(
            ConfirmScreen(message),
            callback=lambda ok, item_id=item.id, ow=overwrite: self._do_restore_recovery(item_id, ow) if ok else None,
        )

    def _do_restore_recovery(self, item_id: str, overwrite: bool) -> None:
        def _work():
            ok, info = restore_recovery_item(item_id, overwrite=overwrite)
            if ok:
                self.app.call_from_thread(self.app.notify, t("bak.recovery_restored", path=info))
            else:
                self.app.call_from_thread(
                    self.app.notify,
                    t("bak.recovery_restore_failed", reason=info),
                    severity="error",
                )
            self.app.call_from_thread(self.refresh_data)

        self.run_worker(_work, thread=True)

    def _click_delete_recovery(self) -> None:
        item = self._get_selected_recovery()
        if not item:
            return
        self.app.push_screen(
            ConfirmScreen(t("bak.confirm_recovery_delete", name=item.name)),
            callback=lambda ok, item_id=item.id, name=item.name: self._do_delete_recovery(item_id, name) if ok else None,
        )

    def _do_delete_recovery(self, item_id: str, name: str) -> None:
        def _work():
            if delete_recovery_item(item_id):
                self.app.call_from_thread(self.app.notify, t("bak.recovery_deleted", name=name))
            else:
                self.app.call_from_thread(
                    self.app.notify,
                    t("bak.recovery_delete_failed"),
                    severity="error",
                )
            self.app.call_from_thread(self.refresh_data)

        self.run_worker(_work, thread=True)

    # ── Delete backups ───────────────────────────────────────

    def _click_delete(self) -> None:
        if self._selected:
            self.app.push_screen(
                ConfirmScreen(t("bak.confirm_bulk_delete", count=len(self._selected))),
                callback=lambda ok: self._do_bulk_delete() if ok else None,
            )
            return
        backup = self._get_selected_backup()
        if backup:
            self.app.push_screen(
                ConfirmScreen(t("bak.confirm_delete", name=backup.name)),
                callback=lambda ok, b=backup: self._do_delete(b) if ok else None,
            )

    def _do_delete(self, backup) -> None:
        def _work():
            if delete_backup(backup.path):
                self.app.call_from_thread(
                    self.app.notify, t("bak.deleted", name=backup.name)
                )
            else:
                self.app.call_from_thread(
                    self.app.notify, t("bak.delete_failed"), severity="error"
                )
            self.app.call_from_thread(self.refresh_data)

        self.run_worker(_work, thread=True)

    def _do_bulk_delete(self) -> None:
        names = list(self._selected)

        def _work():
            ok_count = 0
            fail_count = 0
            for name in names:
                backup = self._backups.get(name)
                if backup and delete_backup(backup.path):
                    ok_count += 1
                else:
                    fail_count += 1
            self.app.call_from_thread(
                self.app.notify,
                t("bak.bulk_deleted", ok=ok_count, fail=fail_count),
            )
            self.app.call_from_thread(self.refresh_data)

        self.run_worker(_work, thread=True)

    # ── Export / Import ──────────────────────────────────────

    def _click_export(self) -> None:
        backup = self._get_selected_backup()
        if not backup:
            return
        self.run_worker(lambda: self._do_export(backup), thread=True)

    def _do_export(self, backup) -> None:
        path = export_backup(backup.path)
        if path:
            self.app.call_from_thread(self.app.notify, t("bak.exported", path=path))
        else:
            self.app.call_from_thread(
                self.app.notify, t("bak.export_failed"), severity="error"
            )

    def _click_import(self) -> None:
        from asm.screens.input_dialog import InputDialog

        self.app.push_screen(
            InputDialog(
                "Import Backup",
                "Enter path to .tar.gz file:",
            ),
            callback=self._on_import_path,
        )

    def _on_import_path(self, path: str | None) -> None:
        if not path or not path.strip():
            return
        self.run_worker(lambda: self._do_import(path.strip()), thread=True)

    def _do_import(self, archive_path: str) -> None:
        result = import_backup(archive_path)
        if result:
            self.app.call_from_thread(
                self.app.notify, t("bak.imported", name=result)
            )
        else:
            self.app.call_from_thread(
                self.app.notify, t("bak.import_failed"), severity="error"
            )
        self.app.call_from_thread(self.refresh_data)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "backups-filter":
            return
        self._filter_query = event.value.strip()
        self._render_tables()
