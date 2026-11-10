"""Agent import screen — move MCP servers and sessions between Claude Code and Codex."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Container, Vertical
from rich.markup import escape
from textual.widgets import DataTable, Static

from asm.i18n import t
from asm.screens.confirm import ConfirmScreen
from asm.services import agent_import

DIRECTIONS = [
    ("mcp", "claude-to-codex"),
    ("mcp", "codex-to-claude"),
    ("sessions", "claude-to-codex"),
    ("sessions", "codex-to-claude"),
]


class AgentImportPane(Container):
    BINDINGS = [
        ("1", "pick(0)", "MCP: Claude→Codex"),
        ("2", "pick(1)", "MCP: Codex→Claude"),
        ("3", "pick(2)", "Sessions: Claude→Codex"),
        ("4", "pick(3)", "Sessions: Codex→Claude"),
        ("space", "toggle", "Toggle"),
        ("a", "toggle_all", "All/None"),
        ("i", "run_import", "Import"),
    ]

    CSS = """
    AgentImportPane {
        height: 1fr;
        padding: 1;
    }
    #import-info {
        height: auto;
        margin: 0 0 1 0;
    }
    #import-table {
        height: 1fr;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._index = 0
        self._rows: list[tuple[str, str, bool, object]] = []
        self._selected: set[str] = set()
        self._error: str | None = None
        self._truncated = 0

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(t("imp.loading"), id="import-info")
            yield DataTable(id="import-table")

    def on_mount(self) -> None:
        table = self.query_one("#import-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Sel", "Item", "Detail", "Status")
        if not getattr(self.app, "lazy_tabs", False):  # main app loads tabs on first view
            self.refresh_data()

    @property
    def _kind(self) -> str:
        return DIRECTIONS[self._index][0]

    @property
    def _direction(self) -> str:
        return DIRECTIONS[self._index][1]

    def refresh_data(self) -> None:
        index = self._index
        self.run_worker(lambda: self._load(index), thread=True)

    def _load(self, index: int) -> None:
        kind, direction = DIRECTIONS[index]
        error = None
        truncated = 0
        rows: list[tuple[str, str, bool, object]] = []
        try:
            if kind == "mcp":
                plan = agent_import.plan_mcp(direction)
                rows += [(s.name, s.transport, True, s) for s in plan.new]
                rows += [(name, "", False, None) for name in plan.already_present]
                rows += [(name, reason, False, None) for name, reason in plan.unsupported]
            else:
                plan = (
                    agent_import.plan_sessions_to_codex()
                    if direction == "claude-to-codex"
                    else agent_import.plan_sessions_to_claude()
                )
                truncated = plan.truncated
                rows += [(c.path, c.title, True, c) for c in plan.new]
                rows += [(c.path, c.title, False, None) for c in plan.already_imported]
        except agent_import.AgentImportError as exc:
            error = str(exc)
        self.app.call_from_thread(self._on_loaded, index, rows, error, truncated)

    def _on_loaded(self, index, rows, error, truncated) -> None:
        if index != self._index:
            return  # the user switched direction while this plan was loading
        self._rows = rows
        self._error = error
        self._truncated = truncated
        # Nothing is preselected: a plan can hold thousands of sessions, and
        # `i` must never fire an import the user did not pick.
        self._selected = set()
        self._refresh_view()

    def _refresh_view(self) -> None:
        table = self.query_one("#import-table", DataTable)
        table.clear()
        for key, detail, importable, _payload in self._rows:
            label = Path(key).name if self._kind == "sessions" else key
            status = t("imp.new") if importable else t("imp.skip")
            if not importable:
                mark = "-"
            else:
                mark = "*" if key in self._selected else " "
            table.add_row(mark, escape(label), escape(detail[:60]), status)

        info = self.query_one("#import-info", Static)
        if self._error:
            info.update(t("imp.error", error=self._error))
            return
        pending = sum(1 for key, _, importable, _ in self._rows if importable)
        header = t(
            "imp.header",
            mode=f"{self._kind} {self._direction}",
            pending=pending,
            selected=len(self._selected),
        )
        if self._truncated:
            header += "  " + t("imp.truncated", count=self._truncated)
        info.update(header)

    def action_pick(self, index: int) -> None:
        self._index = index
        self.refresh_data()

    def action_toggle(self) -> None:
        table = self.query_one("#import-table", DataTable)
        if not self._rows or table.cursor_row is None:
            return
        key, _detail, importable, _payload = self._rows[table.cursor_row]
        if not importable:
            return
        self._selected.symmetric_difference_update({key})
        self._refresh_view()

    def action_toggle_all(self) -> None:
        importable = {key for key, _, flag, _ in self._rows if flag}
        self._selected = set() if self._selected else importable
        self._refresh_view()

    def action_run_import(self) -> None:
        chosen = [
            payload
            for key, _detail, importable, payload in self._rows
            if importable and key in self._selected and payload is not None
        ]
        if not chosen:
            self.app.notify(t("imp.nothing"))
            return

        index = self._index  # the rows shown belong to this direction

        def on_confirm(confirmed: bool) -> None:
            if confirmed:
                self.run_worker(lambda: self._apply(chosen, index), thread=True)

        self.app.push_screen(
            ConfirmScreen(t("imp.confirm", count=len(chosen), mode=self._direction)),
            on_confirm,
        )

    def _apply(self, chosen, index: int) -> None:
        kind, direction = DIRECTIONS[index]
        try:
            if kind == "mcp":
                plan = agent_import.McpPlan(
                    source=direction.split("-")[0], target=direction.split("-")[-1], new=chosen
                )
                result = agent_import.apply_mcp(plan)
            else:
                plan = agent_import.SessionPlan(new=chosen)
                result = (
                    agent_import.apply_sessions_to_codex(plan)
                    if direction == "claude-to-codex"
                    else agent_import.apply_sessions_to_claude(plan)
                )
        except agent_import.AgentImportError as exc:
            self.app.call_from_thread(self.app.notify, t("imp.error", error=str(exc)))
            return
        self.app.call_from_thread(self._on_applied, result)

    def _on_applied(self, result) -> None:
        self.app.notify(
            t("imp.done", imported=len(result.imported), failed=len(result.failed))
        )
        for name, reason in result.failed:
            self.app.notify(t("imp.failed_item", item=Path(name).name, reason=reason[:120]))
        self.refresh_data()
