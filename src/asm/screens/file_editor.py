"""Modal editor for per-project instruction files (CLAUDE.md, AGENTS.md, …)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static, TextArea


class FileEditorScreen(ModalScreen[bool]):
    """Edit (or create) a text file. Ctrl+S saves, Esc cancels.

    Returns True if the file was saved.
    """

    CSS = """
    FileEditorScreen {
        align: center middle;
    }
    #editor-dialog {
        width: 90%;
        height: 85%;
        border: thick $primary;
        background: $surface;
        padding: 0 1;
    }
    #editor-title {
        height: auto;
        text-style: bold;
        padding: 0 1;
    }
    #editor-area {
        height: 1fr;
    }
    #editor-hint {
        height: auto;
        color: $text-muted;
        padding: 0 1;
    }
    """

    BINDINGS = [
        ("ctrl+s", "save", "Save"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, path: str | Path, **kwargs):
        super().__init__(**kwargs)
        self._path = Path(path)
        self._read_failed = False

    def compose(self) -> ComposeResult:
        try:
            if self._path.is_symlink():
                raise OSError("refusing to edit a symlink")
            text = self._path.read_text(encoding="utf-8") if self._path.exists() else ""
        except (OSError, UnicodeError) as e:
            self._read_failed = True
            text = f"(could not read file: {e})"
        exists = self._path.exists()
        verb = "Editing" if exists else "New file"
        with Vertical(id="editor-dialog"):
            yield Static(f"[bold]{verb}:[/] {self._path}", id="editor-title")
            yield TextArea(text, id="editor-area", read_only=self._read_failed)
            yield Static("[dim]Ctrl+S save · Esc cancel[/]", id="editor-hint")

    def on_mount(self) -> None:
        self.query_one("#editor-area", TextArea).focus()

    def action_save(self) -> None:
        if self._read_failed:
            self.app.notify("Save unavailable because the file could not be read", severity="error")
            return
        text = self.query_one("#editor-area", TextArea).text
        try:
            if self._path.is_symlink():
                raise OSError("refusing to edit a symlink")
            self._path.parent.mkdir(parents=True, exist_ok=True)
            mode = self._path.stat().st_mode & 0o777 if self._path.exists() else None
            fd, tmp_name = tempfile.mkstemp(dir=str(self._path.parent), prefix=f".{self._path.name}.")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
                if mode is not None:
                    os.chmod(tmp_name, mode)
                os.replace(tmp_name, self._path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            self.app.notify(f"Saved {self._path.name}")
            self.dismiss(True)
        except OSError as e:
            self.app.notify(f"Save failed: {e}", severity="error")

    def action_cancel(self) -> None:
        self.dismiss(False)
