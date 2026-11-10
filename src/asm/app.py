"""Main Textual application."""

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, TabbedContent, TabPane

from asm.i18n import t
from asm.screens.artifacts import ArtifactsPane
from asm.screens.backups import BackupsPane
from asm.screens.dashboard import DashboardPane
from asm.screens.debug_todos import DebugTodosPane
from asm.screens.file_history import FileHistoryPane
from asm.screens.agent_import import AgentImportPane
from asm.screens.migrate import MigratePane
from asm.screens.projects import ProjectsPane


class CCTuiApp(App):
    """asm — Claude Code & Codex session/data manager TUI."""

    TITLE = "asm"
    SUB_TITLE = "Claude Code & Codex session manager"

    CSS = """
    Screen {
        background: $surface;
    }
    TabbedContent {
        height: 1fr;
    }
    #main-tabs {
        height: 1fr;
    }
    .pane-container {
        height: 1fr;
        padding: 1;
    }
    """

    BINDINGS = [
        Binding("tab", "tab_nav_next", "Next", show=False, priority=True),
        Binding("shift+tab", "tab_nav_prev", "Previous", show=False, priority=True),
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("s", "dash_source", "Source", show=False),
        Binding("1", "dash_daily", show=False),
        Binding("2", "dash_weekly", show=False),
        Binding("3", "dash_monthly", show=False),
        Binding("f1", "tab('tab-dashboard')", "Dashboard"),
        Binding("f2", "tab('tab-projects')", "Projects"),
        Binding("f3", "tab('tab-file-history')", "File History"),
        Binding("f4", "tab('tab-debug-todos')", "Debug/Todos"),
        Binding("f5", "tab('tab-migrate')", "Move/Copy"),
        Binding("f6", "tab('tab-backups')", "Backups"),
        Binding("f7", "tab('tab-artifacts')", "Artifacts"),
        Binding("f8", "tab('tab-agent-import')", "Claude↔Codex"),
    ]

    def __init__(self, target_path: str | None = None, source: str = "all", **kwargs):
        super().__init__(**kwargs)
        if target_path:
            self.target_path = str(Path(target_path).resolve())
        else:
            self.target_path = None
        # Initial dashboard source filter (all | claude | codex). Both sources
        # are always loaded; this only sets the dashboard's starting view.
        self.source = source
        # Set by ProjectsPane's resume action just before exit; __main__ then
        # execs the resume command in the session's project dir.
        self.resume_target: tuple[str, str, str | None] | None = None
        # Panes load when their tab is first shown, so the visible tab doesn't
        # compete (GIL) with seven hidden ones at startup.
        self.lazy_tabs = True
        self._loaded_panes: set[int] = set()

    @property
    def target_encoded(self) -> str | None:
        """Encoded project dir name for the target path."""
        if self.target_path:
            from asm.models import encode_path
            return encode_path(self.target_path)
        return None

    def compose(self) -> ComposeResult:
        if self.target_path:
            self.sub_title = f"Project: {self.target_path}"
        yield Header()
        with TabbedContent(id="main-tabs"):
            with TabPane(t("tab.dashboard"), id="tab-dashboard"):
                yield DashboardPane()
            # Unified sessions: Claude projects + Codex working dirs in one tree.
            with TabPane(t("tab.projects"), id="tab-projects"):
                yield ProjectsPane()
            with TabPane(t("tab.file_history"), id="tab-file-history"):
                yield FileHistoryPane()
            with TabPane(t("tab.debug_todos"), id="tab-debug-todos"):
                yield DebugTodosPane()
            with TabPane(t("tab.migrate"), id="tab-migrate"):
                yield MigratePane()
            with TabPane(t("tab.backups"), id="tab-backups"):
                yield BackupsPane()
            with TabPane(t("tab.artifacts"), id="tab-artifacts"):
                yield ArtifactsPane()
            with TabPane(t("tab.agent_import"), id="tab-agent-import"):
                yield AgentImportPane()
        yield Footer()

    def action_tab(self, tab_id: str) -> None:
        """Switch to a specific tab by ID."""
        tabs = self.query_one("#main-tabs", TabbedContent)
        tabs.get_pane(tab_id)
        tabs.active = tab_id

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Load a pane on its first view; focus the dashboard so its shortcuts work."""
        if event.tabbed_content.id != "main-tabs":
            return
        for pane in event.pane.children:
            if hasattr(pane, "refresh_data") and id(pane) not in self._loaded_panes:
                self._loaded_panes.add(id(pane))
                pane.refresh_data()
        if event.tab.id == "tab-dashboard":
            self.call_after_refresh(self._focus_dashboard)

    def _focus_dashboard(self) -> None:
        self.query_one("DashboardPane").focus()

    def _dashboard_active(self) -> bool:
        return self.query_one("#main-tabs", TabbedContent).active == "tab-dashboard"

    def _dash(self, period: str | None) -> None:
        if self._dashboard_active():
            dash = self.query_one("DashboardPane")
            dash.action_source_cycle() if period is None else dash.action_period(period)

    def action_dash_source(self) -> None:
        self._dash(None)

    def action_dash_set_source(self, name: str) -> None:
        """Set a specific dashboard source filter (from a clicked chip)."""
        self.query_one("DashboardPane").set_source(name)

    def action_dash_daily(self) -> None:
        self._dash("daily")

    def action_dash_weekly(self) -> None:
        self._dash("weekly")

    def action_dash_monthly(self) -> None:
        self._dash("monthly")

    def action_tab_nav_next(self) -> None:
        """Handle Tab navigation, with dashboard period cycling override."""
        tabs = self.query_one("#main-tabs", TabbedContent)
        if tabs.active == "tab-dashboard":
            self.query_one("DashboardPane").action_period_next()
            return
        self.screen.focus_next()

    def action_tab_nav_prev(self) -> None:
        """Handle reverse Tab navigation, with dashboard period cycling override."""
        tabs = self.query_one("#main-tabs", TabbedContent)
        if tabs.active == "tab-dashboard":
            self.query_one("DashboardPane").action_period_prev()
            return
        self.screen.focus_previous()

    def action_refresh(self) -> None:
        """Refresh all panes that have refresh_data."""
        from asm.services import claude_data, codex_data
        codex_data.refresh()
        claude_data.refresh_usage_cache()
        for pane in self.query(
            "DashboardPane, ProjectsPane, FileHistoryPane, "
            "DebugTodosPane, MigratePane, BackupsPane, ArtifactsPane"
        ):
            if hasattr(pane, "refresh_data") and id(pane) in self._loaded_panes:
                pane.refresh_data()
        self.notify(t("app.refreshed"))
