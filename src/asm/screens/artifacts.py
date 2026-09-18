"""Artifacts screen — browse artifacts published from Claude Code sessions."""

from __future__ import annotations

from datetime import datetime

from textual.app import ComposeResult
from textual.containers import Container, Vertical
from textual.widgets import DataTable, Static

from asm.i18n import t
from asm.models import decode_path_hint
from asm.services.artifacts import ArtifactInfo, list_artifacts


class ArtifactsPane(Container):
    BINDINGS = [
        ("o", "open_artifact", "Open in Browser"),
        ("c", "copy_url", "Copy URL"),
    ]

    CSS = """
    ArtifactsPane {
        height: 1fr;
        padding: 1;
    }
    #artifacts-info {
        height: auto;
        margin: 0 0 1 0;
    }
    #artifacts-table {
        height: 1fr;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._artifacts: list[ArtifactInfo] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(t("art.loading"), id="artifacts-info")
            yield DataTable(id="artifacts-table")

    def on_mount(self) -> None:
        table = self.query_one("#artifacts-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Published", "Title", "Project", "URL")
        self.refresh_data()

    def refresh_data(self) -> None:
        self.run_worker(self._load, thread=True)

    def _load(self) -> None:
        artifacts = list_artifacts()
        self.app.call_from_thread(self._on_loaded, artifacts)

    def _on_loaded(self, artifacts: list[ArtifactInfo]) -> None:
        self._artifacts = artifacts
        table = self.query_one("#artifacts-table", DataTable)
        table.clear()
        info = self.query_one("#artifacts-info", Static)
        if not artifacts:
            info.update(t("art.none"))
            return
        info.update(t("art.header", count=len(artifacts)))
        for i, a in enumerate(artifacts):
            published = (
                datetime.fromtimestamp(a.published).strftime("%Y-%m-%d %H:%M")
                if a.published
                else "?"
            )
            title = f"{a.favicon} {a.title}" if a.favicon else a.title
            project = decode_path_hint(a.project_dir) if a.project_dir else ""
            table.add_row(published, title, project, a.url, key=str(i))

    def _selected(self) -> ArtifactInfo | None:
        table = self.query_one("#artifacts-table", DataTable)
        if not self._artifacts or table.cursor_row is None or table.cursor_row < 0:
            return None
        if table.cursor_row >= len(self._artifacts):
            return None
        return self._artifacts[table.cursor_row]

    def action_open_artifact(self) -> None:
        artifact = self._selected()
        if artifact is None:
            return
        import webbrowser

        try:
            opened = webbrowser.open(artifact.url)
        except OSError as exc:
            self.app.notify(f"Open failed: {exc}", severity="error")
            return
        if not opened:
            self.app.notify("Browser did not accept the artifact URL", severity="error")
            return
        self.app.notify(t("art.opened", url=artifact.url))

    def action_copy_url(self) -> None:
        artifact = self._selected()
        if artifact is None:
            return
        try:
            self.app.copy_to_clipboard(artifact.url)
        except (OSError, RuntimeError) as exc:
            self.app.notify(f"Copy failed: {exc}", severity="error")
            return
        self.app.notify(t("art.copied", url=artifact.url))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_open_artifact()
