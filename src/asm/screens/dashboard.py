"""Dashboard screen - usage stats and overview."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.widgets import DataTable, Static, TabPane, TabbedContent

from asm.i18n import t
from asm.utils import RECENT_DAYS_LIMIT, TOP_PROJECT_CHART_ROWS, TOP_PROJECT_LIMIT, format_bytes


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


# Source tags used to distinguish Claude vs Codex rows in the combined view.
_SRC_TAG = {"claude": "[#D97757]C[/]", "codex": "[#10A37F]X[/]"}


def _merge_usage(usages: dict[str, dict]) -> dict:
    """Merge per-source get_usage_data() dicts into one combined view.

    Each project entry is tagged with its source so the combined table stays
    distinguishable. Model keys are already disjoint (claude-* vs gpt-*).
    """
    total_cost = 0.0
    model_totals: dict[str, dict] = {}
    project_costs: list[dict] = []
    sessions_by_day: dict[str, int] = {}
    total_sessions = 0
    for source, u in usages.items():
        if not u:
            continue
        total_cost += u.get("total_cost", 0.0)
        for model, mt in u.get("model_totals", {}).items():
            model_totals[model] = {**mt, "source": source}
        for pc in u.get("project_costs", []):
            project_costs.append({**pc, "source": source})
        for day, n in u.get("sessions_by_day", {}).items():
            sessions_by_day[day] = sessions_by_day.get(day, 0) + n
        total_sessions += u.get("total_sessions_ever", 0)
    project_costs.sort(key=lambda x: x["cost"], reverse=True)
    claude_u = usages.get("claude") or {}
    return {
        "total_cost": total_cost,
        "model_totals": model_totals,
        "project_costs": project_costs[:TOP_PROJECT_LIMIT],
        "sessions_by_day": dict(sorted(sessions_by_day.items(), reverse=True)[:RECENT_DAYS_LIMIT]),
        "total_sessions_ever": total_sessions,
        "first_use": claude_u.get("first_use", ""),
        "num_startups": claude_u.get("num_startups", 0),
    }


def _merge_periods(period_lists: dict[str, list]) -> list:
    """Merge per-source get_period_usage() lists, summing by period key."""
    from collections import defaultdict
    agg: dict[str, dict] = defaultdict(lambda: {
        "total_cost": 0.0, "total_input": 0, "total_output": 0,
        "total_cache": 0, "total_messages": 0, "models": {},
    })
    for data in period_lists.values():
        for row in data or []:
            e = agg[row["period"]]
            e["total_cost"] += row.get("total_cost", 0.0)
            e["total_input"] += row.get("total_input", 0)
            e["total_output"] += row.get("total_output", 0)
            e["total_cache"] += row.get("total_cache", 0)
            e["total_messages"] += row.get("total_messages", 0)
            e["models"].update(row.get("models", {}))
    return [{"period": k, **v} for k, v in sorted(agg.items(), reverse=True)]


class DashboardPane(Container):
    # Focusable so its key bindings (s / 1 / 2 / 3) work right after the tab opens
    # without the user having to click into the pane first.
    can_focus = True

    BINDINGS = [
        Binding("tab", "period_next", "Next Period", show=False, priority=True),
        Binding("shift+tab", "period_prev", "Previous Period", show=False, priority=True),
        Binding("1", "period('daily')", "Daily"),
        Binding("2", "period('weekly')", "Weekly"),
        Binding("3", "period('monthly')", "Monthly"),
        Binding("s", "source_cycle", "Source"),
    ]

    CSS = """
    DashboardPane {
        height: 1fr;
        padding: 1 2;
    }
    #dash-scroll {
        height: 1fr;
    }
    .dash-title {
        text-style: bold;
        color: $accent;
        margin: 1 0 0 0;
    }
    .dash-table {
        height: auto;
        margin: 0 0 0 2;
    }
    #period-tabs {
        height: auto;
        margin: 0;
    }
    .period-pane {
        padding: 0;
        height: auto;
    }
    .period-table {
        height: auto;
        margin: 0;
    }
    """

    CONTENT_IDS = (
        "dash-header", "dash-div-1", "dash-cost-title", "dash-model-table",
        "dash-div-2", "period-tabs",
        "dash-div-3", "dash-top-title", "dash-top-projects",
        "dash-div-4", "dash-overview-title", "dash-overview",
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._period = "daily"
        self._source = "all"  # all | claude | codex
        self._raw_stats: dict[str, object] = {}
        self._raw_usage: dict[str, dict] = {}
        self._raw_periods: dict[str, dict[str, list]] = {}  # period -> {source: list}
        self._loaded = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dash-scroll"):
            yield Static(f"[bold]asm[/]  {t('dash.loading')}", id="dash-loading")
            yield Static("", id="dash-header")
            yield Static("", id="dash-div-1")
            yield Static("", id="dash-cost-title", classes="dash-title")
            yield Static("", id="dash-model-table", classes="dash-table")
            yield Static("", id="dash-div-2")
            with TabbedContent(initial="period-daily", id="period-tabs"):
                with TabPane("Daily", id="period-daily", classes="period-pane"):
                    yield DataTable(id="period-table-daily", classes="period-table")
                with TabPane("Weekly", id="period-weekly", classes="period-pane"):
                    yield DataTable(id="period-table-weekly", classes="period-table")
                with TabPane("Monthly", id="period-monthly", classes="period-pane"):
                    yield DataTable(id="period-table-monthly", classes="period-table")
            yield Static("", id="dash-div-3")
            yield Static("", id="dash-top-title", classes="dash-title")
            yield Static("", id="dash-top-projects", classes="dash-table")
            yield Static("", id="dash-div-4")
            yield Static("", id="dash-overview-title", classes="dash-title")
            yield Static("", id="dash-overview", classes="dash-table")

    def on_mount(self) -> None:
        self._source = getattr(self.app, "source", "all") or "all"
        for wid in self.CONTENT_IDS:
            self.query_one(f"#{wid}").display = False
        for key in ("daily", "weekly", "monthly"):
            pt = self._get_period_table(key)
            pt.zebra_stripes = True
            pt.show_cursor = False
            pt.add_columns("Period", "Cost", "", "Messages", "Input", "Output", "Cache")
        if not getattr(self.app, "lazy_tabs", False):  # main app loads tabs on first view
            self.refresh_data()

    def refresh_data(self) -> None:
        self._raw_periods = {}
        self.run_worker(self._load, thread=True)

    def _show_scan_progress(self, text: str) -> None:
        loading = self.query_one("#dash-loading", Static)
        loading.display = True
        loading.update(f"[bold]asm[/]  {t('dash.scanning', progress=text)}")

    def _source_modules(self) -> dict:
        from asm.services import claude_data, codex_data
        mods = {"claude": claude_data}
        if codex_data.is_available():
            mods["codex"] = codex_data
        return mods

    def _load(self) -> None:
        from asm.services import ledger, pricing
        pricing.load_live_rates()  # worker thread — network wait can't block the UI

        def _progress(source: str, done: int, total: int) -> None:
            self.app.call_from_thread(
                self._show_scan_progress, f"{source} {done:,}/{total:,}"
            )

        # Pre-warm the ledger with progress — the first backfill over a large
        # ~/.codex can take minutes and must not look like a hang.
        ledger.update_claude(_progress)
        ledger.update_codex(_progress)
        mods = self._source_modules()
        stats = {s: m.get_stats() for s, m in mods.items()}
        usage = {s: m.get_usage_data() for s, m in mods.items()}
        # One scan per source yields all three periods (daily/weekly/monthly).
        by_source = {s: m.get_all_period_usage() for s, m in mods.items()}
        periods = {
            p: {s: by_source[s].get(p, []) for s in by_source}
            for p in ("daily", "weekly", "monthly")
        }
        self.app.call_from_thread(self._on_data_loaded, stats, usage, periods)

    def _on_data_loaded(self, stats: dict, usage: dict, periods: dict) -> None:
        self._raw_stats = stats
        self._raw_usage = usage
        self._raw_periods = periods
        self._loaded = True
        self.query_one("#dash-loading", Static).display = False
        for wid in self.CONTENT_IDS:
            self.query_one(f"#{wid}").display = True
        self._render_all()
        # Take focus so 's' / '1' / '2' / '3' work without a click.
        try:
            if self.app.query_one("#main-tabs", TabbedContent).active == "tab-dashboard":
                self.focus()
        except Exception:
            pass

    # ── Source filter (All / Claude / Codex) ──────────────────────────
    def _has_codex(self) -> bool:
        return "codex" in self._raw_usage

    def action_source_cycle(self) -> None:
        if not self._has_codex():
            return  # nothing to switch between
        order = ["all", "claude", "codex"]
        self._source = order[(order.index(self._source) + 1) % len(order)]
        if self._loaded:
            self._render_all()

    def _current_usage(self) -> dict:
        if self._source == "all":
            return _merge_usage(self._raw_usage)
        return self._raw_usage.get(self._source) or {}

    def _current_stats(self):
        # Data-overview is Claude-centric; fall back to whatever source exists.
        return self._raw_stats.get("claude") or next(iter(self._raw_stats.values()), None)

    def _current_period(self, key: str) -> list:
        raw = self._raw_periods.get(key, {})
        if self._source == "all":
            return _merge_periods(raw)
        return raw.get(self._source) or []

    def _render_all(self) -> None:
        if not self._loaded:
            return
        usage = self._current_usage()
        stats = self._current_stats()
        if not usage or not stats:
            return

        from asm.services import pricing
        # Claude's per-day history counts prompts; Codex counts sessions.
        unit = {"claude": "prompts", "codex": "sessions"}.get(self._source, "prompts+sessions")
        self.query_one("#dash-header", Static).update(
            f"{self._source_filter_line()}\n"
            f"[dim]Since {usage['first_use'][:10] if usage['first_use'] else 'N/A'}  |  "
            f"{usage['num_startups']} startups  |  "
            f"{usage['total_sessions_ever']} total {unit}  |  "
            f"rates: {pricing.rates_source()}[/]"
        )
        self.query_one("#dash-div-1", Static).update("─" * 70)

        self.query_one("#dash-cost-title", Static).update(
            f"[bold]  {t('dash.total_cost')}  [green]${usage['total_cost']:.2f}[/][/]"
        )
        show_tag = self._source == "all" and self._has_codex()
        model_lines = []
        for model in sorted(usage["model_totals"].keys()):
            mt = usage["model_totals"][model]
            short = model.replace("claude-", "").split("-2025")[0].split("-2026")[0]
            tag = f"{_SRC_TAG.get(mt.get('source', ''), ' ')} " if show_tag else ""
            model_lines.append(
                f"  {tag}[cyan]{short:18s}[/]  "
                f"${mt['costUSD']:>8.2f}  "
                f"In:{_fmt_tokens(mt['inputTokens']):>6s}  "
                f"Out:{_fmt_tokens(mt['outputTokens']):>6s}  "
                f"Cache:{_fmt_tokens(mt['cacheReadInputTokens']):>7s}"
            )
        from asm.services.codex_data import UNKNOWN_MODEL
        if UNKNOWN_MODEL in usage["model_totals"]:
            model_lines.append(
                f"  [dim]{UNKNOWN_MODEL} = session file records no model id; "
                f"priced at the default GPT tier[/]"
            )
        self.query_one("#dash-model-table", Static).update("\n".join(model_lines))
        self.query_one("#dash-div-2", Static).update("─" * 70)

        self._render_period_section()

        self.query_one("#dash-div-3", Static).update("─" * 70)

        self.query_one("#dash-top-title", Static).update(
            f"[bold]  {t('dash.top_projects')}[/]  [dim]({t('dash.top10')})[/]"
        )
        costs = usage["project_costs"]
        cost_lines = []
        for i, pc in enumerate(costs[:TOP_PROJECT_CHART_ROWS], 1):
            bar_len = int(pc["cost"] / costs[0]["cost"] * 20) if costs and costs[0]["cost"] else 0
            bar = "▓" * bar_len
            tag = f"{_SRC_TAG.get(pc.get('source', ''), ' ')} " if show_tag else ""
            cost_lines.append(f"  {i:2d}. {tag}${pc['cost']:>8.2f}  {bar}  {pc['name']}")
        self.query_one("#dash-top-projects", Static).update("\n".join(cost_lines))
        self.query_one("#dash-div-4", Static).update("─" * 70)

        self.query_one("#dash-overview-title", Static).update(f"[bold]  {t('dash.data_overview')}[/]")
        self.query_one("#dash-overview", Static).update(self._overview_text())

    def _source_filter_line(self) -> str:
        """Header line with the active source filter as clickable chips."""
        if not self._has_codex():
            return "[bold]asm[/]  Claude Code Session Manager  [dim](Codex: none)[/]"

        def chip(key, label):
            style = "reverse" if self._source == key else "dim"
            # Clickable (Textual @click markup) and keyboard-toggleable with 's'.
            return f"[@click=app.dash_set_source('{key}')][{style}] {label} [/][/]"

        return (
            "[bold]asm[/]   "
            f"{chip('all', 'All')}{chip('claude', 'Claude')}{chip('codex', 'Codex')}"
            "   [dim]click or press [b]s[/][/]"
        )

    def set_source(self, name: str) -> None:
        """Set the dashboard source filter directly (from a click or key)."""
        if name not in ("all", "claude", "codex"):
            return
        if not self._has_codex() and name != "claude":
            return
        if self._source != name and self._loaded:
            self._source = name
            self._render_all()

    def _overview_text(self) -> str:
        claude = self._raw_stats.get("claude")
        codex = self._raw_stats.get("codex")
        lines = []
        if self._source in ("all", "claude") and claude is not None:
            lines += [
                "  [#D97757]Claude[/]",
                f"  {'Projects':20s} {claude.total_projects:>6d}",
                f"  {'Session Dirs':20s} {claude.total_sessions:>6d}    [dim](orphaned: {claude.orphaned_sessions})[/]",
                f"  {'File History':20s} {claude.total_file_history:>6d}    [dim](orphaned: {claude.orphaned_file_history})[/]",
                f"  {'Debug Files':20s} {claude.total_debug:>6d}    [dim](orphaned: {claude.orphaned_debug})[/]",
                f"  {'Todos':20s} {claude.total_todos:>6d}    [dim](orphaned: {claude.orphaned_todos})[/]",
                f"  {'Disk: .claude/':20s} {format_bytes(claude.claude_dir_size):>10s}",
            ]
        if self._source in ("all", "codex") and codex is not None:
            if lines:
                lines.append("")
            lines += [
                "  [#10A37F]Codex[/]",
                f"  {'Projects (cwd)':20s} {codex.total_projects:>6d}",
                f"  {'Sessions':20s} {codex.total_sessions:>6d}",
            ]
        return "\n".join(lines)

    def _render_period_section(self) -> None:
        """Render the period table from cache."""
        self.query_one("#period-tabs", TabbedContent).active = f"period-{self._period}"

        period_loaded = self._period in self._raw_periods
        period_data = self._current_period(self._period)
        pt = self._get_period_table(self._period)
        pt.clear()

        if period_data:
            from rich.text import Text
            rows = period_data[:20]
            max_cost = max((r["total_cost"] for r in rows), default=0.0)
            for p in rows:
                bar = "▆" * round(p["total_cost"] / max_cost * 10) if max_cost else ""
                pt.add_row(
                    p["period"],
                    f"${p['total_cost']:.2f}",
                    Text(bar, style="green"),
                    str(p["total_messages"]),
                    _fmt_tokens(p["total_input"]),
                    _fmt_tokens(p["total_output"]),
                    _fmt_tokens(p["total_cache"]),
                )
        elif not period_loaded:
            pt.add_row(t("dash.loading_period"), "", "", "", "", "", "")
        else:
            pt.add_row(t("dash.no_data"), "", "", "", "", "", "")

        pt.move_cursor(row=0, column=0, animate=False, scroll=False)
        pt.scroll_home(animate=False, immediate=True, x_axis=False, y_axis=True)

    def _get_period_table(self, key: str) -> DataTable:
        return self.query_one(f"#period-table-{key}", DataTable)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Switch the displayed period when its sub-tab is clicked."""
        if event.tabbed_content.id != "period-tabs":
            return
        # Use the active *pane* id ("period-weekly"); event.tab.id is prefixed
        # with "--content-tab-" so matching on it never worked.
        active = event.tabbed_content.active or ""
        if active.startswith("period-"):
            period = active.replace("period-", "", 1)
            if period != self._period:
                self.action_period(period)

    def action_period(self, key: str) -> None:
        """Switch the displayed period (all three are preloaded in _load)."""
        self._period = key
        self._render_period_section()

    def action_period_next(self) -> None:
        """Cycle to the next dashboard period immediately."""
        order = ["daily", "weekly", "monthly"]
        idx = order.index(self._period)
        self.action_period(order[(idx + 1) % len(order)])

    def action_period_prev(self) -> None:
        """Cycle to the previous dashboard period immediately."""
        order = ["daily", "weekly", "monthly"]
        idx = order.index(self._period)
        self.action_period(order[(idx - 1) % len(order)])
