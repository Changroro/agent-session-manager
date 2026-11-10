"""Projects management screen - tree with inline sessions."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path, PurePath

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, Static, Tree

from rich.markup import escape

from asm.models import PROJECTS_DIR, ProjectInfo, decode_path_hint, encode_path
from asm.screens.confirm import ConfirmScreen
from asm.screens.file_editor import FileEditorScreen

# Per-project instruction files that can be viewed/edited from the detail panel.
INSTRUCTION_FILES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "AGENTS.local.md")
from asm.services.backup import create_config_backup
from asm.services.claude_data import (
    find_duplicate_sessions,
    find_empty_sessions,
    get_project_sessions,
    get_projects,
    get_session_messages,
    get_sessions,
    load_claude_json,
    remove_project_from_json,
    project_paths_for_encoded,
)
from asm.i18n import t
from asm.services.cleaner import trash_sessions, trash_single_session_file
from asm.utils import format_bytes
from asm.widgets.action_bar import ActionBar


def _fmt_cost(cost: float) -> str:
    return f"${cost / 1000:.1f}k" if cost >= 1000 else f"${cost:.2f}"


class ProjectsPane(Container):
    BINDINGS = [
        ("d", "trash_session", "Trash Session"),
        ("D", "trash_orphaned", "Trash Orphaned"),
        ("x", "remove_config", "Remove Config"),
        ("o", "resume_session", "Resume"),
        ("e", "export_session", "Export MD"),
    ]

    CSS = """
    ProjectsPane {
        height: 1fr;
        padding: 1;
    }
    #projects-info {
        height: auto;
        margin-bottom: 1;
        color: $text-muted;
    }
    #projects-layout {
        height: 1fr;
    }
    #projects-filter {
        margin-bottom: 1;
    }
    #projects-tree-panel {
        width: 1fr;
        height: 1fr;
        min-width: 40;
    }
    #project-tree {
        height: 1fr;
    }
    #tree-actions {
        height: 1;
        margin-top: 1;
    }
    #project-detail-panel {
        width: 1fr;
        height: 1fr;
        border-left: tall $primary;
        padding: 0 1;
    }
    #detail-scroll {
        height: 1fr;
    }
    #project-detail-header {
        height: auto;
        text-style: bold;
        margin-bottom: 1;
    }
    #detail-actions {
        height: 1;
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(
            t("proj.info"),
            id="projects-info",
        )
        yield Input(placeholder=t("proj.filter_placeholder"), id="projects-filter")
        with Horizontal(id="projects-layout"):
            with Vertical(id="projects-tree-panel"):
                yield Tree("Projects", id="project-tree")
                yield ActionBar(id="tree-actions")
            with Vertical(id="project-detail-panel"):
                yield Static("", id="project-detail-header")
                yield VerticalScroll(
                    Static(t("proj.select_hint"), id="project-detail-body"),
                    id="detail-scroll",
                )
                yield ActionBar(id="detail-actions")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._selected_project_path: str | None = None
        self._selected_session: tuple[str, str | None] | None = None
        self._selected_session_node = None
        self._project_map: dict[str, ProjectInfo] = {}
        self._all_projects: list[ProjectInfo] = []
        self._all_orphaned_sessions = []
        self._all_duplicates: dict[str, list[str]] = {}
        self._codex_paths: set[str] = set()
        self._project_costs: dict[str, float] = {}
        self._session_counts: dict[str, int] = {}  # encoded dir -> top-level .jsonl count
        self._empty_sessions: list[dict] = []
        self._filter_query = ""
        self._sort_mode = "path"
        # Session-title search: per-project (SessionDetail, source) cache built
        # lazily on first title query, and the current query's matches. The
        # generation counter discards cache builds that finish after a refresh.
        self._session_cache: dict[str, list] | None = None
        self._session_cache_building = False
        self._session_cache_gen = 0
        self._session_match_map: dict[str, list] = {}
        # Full-text body search results (None until the debounced scan lands):
        #  - Claude: session_ids whose JSONL body contains the query.
        #  - Codex: sessions loaded directly from matching rollout paths, grouped
        #    by cwd, loaded straight from their rollout paths.
        # The generation counter discards results of superseded queries.
        self._content_claude_ids: set[str] | None = None
        self._content_main_ids: set[str] = set()  # hit in the session's own transcript
        self._subagent_only: set[str] = set()  # shown with a (sub-agent) marker
        self._content_codex_by_cwd: dict[str, list] = {}
        self._content_search_gen = 0
        self._content_search_timer = None

    def on_mount(self) -> None:
        tree = self.query_one("#project-tree", Tree)
        tree.show_root = False
        if not getattr(self.app, "lazy_tabs", False):  # main app loads tabs on first view
            self.refresh_data()

    def refresh_data(self) -> None:
        self.run_worker(self._load_projects, thread=True)

    def _load_projects(self) -> None:
        from asm.services import codex_data
        from asm.services.claude_data import get_project_cost_map
        projects = get_projects()
        orphaned_sessions = [s for s in get_sessions() if s.is_orphaned]
        duplicates = find_duplicate_sessions()
        # Merge Codex working directories into the same project tree, so both
        # agents live in one view. Codex cwds not already known to Claude become
        # their own project nodes.
        codex_paths: set[str] = set()
        if codex_data.is_available():
            claude_paths = {p.path for p in projects}
            for cp in codex_data.get_projects():
                codex_paths.add(cp.path)
                if cp.path not in claude_paths:
                    projects.append(cp)
        empty_sessions = find_empty_sessions()
        session_counts = {
            d.name: sum(1 for _ in d.glob("*.jsonl"))
            for d in (PROJECTS_DIR.iterdir() if PROJECTS_DIR.exists() else [])
            if d.is_dir()
        }
        if self.app.target_path:
            projects = [p for p in projects if p.path == self.app.target_path]
            orphaned_sessions = []
            duplicates = {}
            codex_paths = {p for p in codex_paths if p == self.app.target_path}
            empty_sessions = []
        # Show the tree (and start the title index) before the slower cost pass.
        self.app.call_from_thread(
            self._set_project_data, projects, orphaned_sessions, duplicates, codex_paths,
            empty_sessions, {}, session_counts,
        )
        project_costs = get_project_cost_map()
        if codex_data.is_available():
            for cwd, cost in codex_data.get_project_cost_map().items():
                project_costs[cwd] = project_costs.get(cwd, 0.0) + cost
        self.app.call_from_thread(self._set_project_costs, project_costs)

    def _set_project_costs(self, project_costs: dict[str, float]) -> None:
        self._project_costs = project_costs
        self._build_tree_from_state()

    def _set_project_data(self, projects, orphaned_sessions, duplicates=None, codex_paths=None,
                          empty_sessions=None, project_costs=None, session_counts=None) -> None:
        self._all_projects = projects
        self._all_orphaned_sessions = orphaned_sessions or []
        self._all_duplicates = duplicates or {}
        self._codex_paths = codex_paths or set()
        self._empty_sessions = empty_sessions or []
        self._project_costs = project_costs or {}
        self._session_counts = session_counts or {}
        self._session_cache = None
        self._session_cache_gen += 1
        if self._content_search_timer is not None:
            self._content_search_timer.stop()
            self._content_search_timer = None
        self._content_search_gen += 1
        self._content_claude_ids = None
        self._content_main_ids = set()
        self._content_codex_by_cwd = {}
        if self._filter_query:
            self._schedule_content_search()
        self._build_tree_from_state()
        # Build the title index now, so the first keystroke already has matches.
        self._ensure_session_cache()

    def _ensure_session_cache(self) -> None:
        if self._session_cache is None and not self._session_cache_building:
            self._session_cache_building = True
            gen = self._session_cache_gen
            self.run_worker(lambda: self._build_session_cache(gen), thread=True)

    def _build_tree_from_state(self) -> None:
        projects = list(self._all_projects)
        orphaned_sessions = list(self._all_orphaned_sessions)
        query = self._filter_query.casefold()
        self._session_match_map = {}
        self._subagent_only = set()

        if query:
            session_matches = self._match_sessions_by_title(query)
            self._merge_codex_content_matches(session_matches)
            self._session_match_map = session_matches
            projects = [
                p for p in projects
                if query in p.path.casefold() or p.path in session_matches
            ]
            # Codex body hits may belong to a working dir not in the project list
            # (e.g. an old cwd beyond the recent scan). Add a node so they show.
            shown = {p.path for p in projects}
            for cwd in self._content_codex_by_cwd:
                if cwd in session_matches and cwd not in shown:
                    projects.append(
                        ProjectInfo(path=cwd, exists=Path(cwd).exists() if cwd else False)
                    )
                    shown.add(cwd)
            orphaned_sessions = [
                s for s in orphaned_sessions
                if query in s.dir_name.casefold() or query in PurePath(s.actual_path).name.casefold()
            ]

        if self._sort_mode == "cost":
            costs = self._project_costs
            projects.sort(key=lambda p: (-(costs.get(p.path) or p.last_cost or 0), p.path.casefold()))
        elif self._sort_mode == "status":
            projects.sort(key=lambda p: (p.exists, p.path.casefold()))
        else:
            projects.sort(key=lambda p: p.path.casefold())

        self._build_tree(projects, orphaned_sessions)

    def _match_sessions_by_title(self, query: str) -> dict[str, list]:
        """Map project path -> tagged sessions matching the filter.

        Matches a session when the query is in its title *or* (once the
        debounced full-text scan has landed) in its conversation body. The title
        cache is built lazily in a worker on the first query; until it is ready
        this returns no matches (path filtering still applies) and the tree is
        rebuilt once the cache lands.
        """
        if self._session_cache is None:
            self._ensure_session_cache()
            return {}
        # Codex body matches are folded in by _build_tree_from_state (they may
        # not be present in the title cache), so here we only match
        # titles plus Claude body hits over the title cache.
        from asm.services.search import normalize

        nq = normalize(query)  # whitespace-insensitive, same rule as body search
        claude_ids = self._content_claude_ids
        matches: dict[str, list] = {}
        for path, tagged in self._session_cache.items():
            hit = []
            for ts in tagged:
                s, source = ts
                if nq and nq in normalize(s.summary or ""):
                    hit.append(ts)
                elif source == "claude" and claude_ids and s.session_id in claude_ids:
                    hit.append(ts)
                    if s.session_id not in self._content_main_ids:
                        self._subagent_only.add(s.session_id)
            if hit:
                matches[path] = hit
        return matches

    def _merge_codex_content_matches(self, matches: dict[str, list]) -> None:
        """Fold Codex body-search hits into ``matches`` (path -> tagged sessions).

        These are loaded straight from disk, so they may not be in the title
        cache; de-dupe by session id against whatever the title pass already
        added for the same working dir.
        """
        for cwd, tagged in self._content_codex_by_cwd.items():
            existing = matches.setdefault(cwd, [])
            seen = {t[0].session_id for t in existing}
            for ts in tagged:
                if ts[0].session_id not in seen:
                    existing.append(ts)
                    seen.add(ts[0].session_id)
            if not existing:
                del matches[cwd]

    def _build_session_cache(self, gen: int) -> None:
        cache: dict[str, list] = {}
        for p in self._all_projects:
            tagged = self._tagged_sessions(p.path)
            if tagged:
                cache[p.path] = tagged
        self.app.call_from_thread(self._on_session_cache_ready, cache, gen)

    def _on_session_cache_ready(self, cache: dict[str, list], gen: int) -> None:
        self._session_cache_building = False
        if gen != self._session_cache_gen:
            # Data was refreshed while scanning: this cache reflects the old
            # project list. Drop it and scan the new one.
            self._ensure_session_cache()
            return
        self._session_cache = cache
        if self._filter_query:
            self._build_tree_from_state()

    def _schedule_content_search(self) -> None:
        """(Re)arm the debounced full-text body scan for the current query.

        Title matching stays instant; the heavier body scan runs in a worker a
        short moment after typing stops, so fast typing doesn't spawn a scan per
        keystroke. A blank query just cancels any pending scan.
        """
        if self._content_search_timer is not None:
            self._content_search_timer.stop()
            self._content_search_timer = None
        query = self._filter_query
        self._content_search_gen += 1  # also cancels a scan still running for the old query
        if not query:
            return
        gen = self._content_search_gen
        self._content_search_timer = self.set_timer(
            0.15,
            lambda: self.run_worker(lambda: self._run_content_search(query, gen), thread=True),
        )

    def _run_content_search(self, query: str, gen: int) -> None:
        """Stream body hits into the tree as they are found (~10 updates/s).

        A newer query bumps the generation, which makes the iterators kill
        their ripgrep process instead of finishing a stale scan.
        """
        import time

        from asm.services import codex_data, search

        def cancelled() -> bool:
            return gen != self._content_search_gen

        claude_hits: set[tuple[str, bool]] = set()
        codex_paths: list[str] = []
        last = time.monotonic()

        def flush(force: bool = False) -> None:
            nonlocal last
            if not (claude_hits or codex_paths) or cancelled():
                return
            if not force and time.monotonic() - last < 0.1:
                return
            codex_by_cwd: dict[str, list] = {}
            if codex_paths:
                for s in codex_data.get_sessions_by_paths(codex_paths):
                    codex_by_cwd.setdefault(s.cwd, []).append((s, "codex"))
            self.app.call_from_thread(self._on_content_batch, set(claude_hits), codex_by_cwd, gen)
            claude_hits.clear()
            codex_paths.clear()
            last = time.monotonic()

        try:
            for hit in search.iter_claude_hits(query, cancelled):
                claude_hits.add(hit)
                flush()
            # Codex rollouts are global; matches load straight from disk so hits
            # appear even for long-dead working dirs.
            for path in search.iter_codex_rollout_paths(query, cancelled):
                codex_paths.append(path)
                flush()
            flush(force=True)
            self.app.call_from_thread(self._on_content_batch, set(), {}, gen)
        except (search.SearchError, codex_data.CodexDataError, ValueError) as exc:
            if not cancelled():
                self.app.call_from_thread(self.app.notify, str(exc), severity="error")

    def _on_content_batch(self, claude_hits: set[tuple[str, bool]], codex_by_cwd: dict[str, list], gen: int) -> None:
        if gen != self._content_search_gen:
            return  # a newer query superseded this scan
        merged = self._content_claude_ids or set()
        if not claude_hits and not codex_by_cwd and self._content_claude_ids is not None:
            return  # end-of-scan marker with nothing new
        self._content_claude_ids = merged | {sid for sid, _sub in claude_hits}
        self._content_main_ids |= {sid for sid, sub in claude_hits if not sub}
        for cwd, tagged in codex_by_cwd.items():
            self._content_codex_by_cwd.setdefault(cwd, []).extend(tagged)
        if self._filter_query:
            self._build_tree_from_state()

    def _build_tree(self, projects: list[ProjectInfo], orphaned_sessions=None) -> None:
        tree = self.query_one("#project-tree", Tree)
        tree.clear()
        self._project_map = {p.path: p for p in projects}
        all_paths = {p.path for p in projects}

        # Update orphaned sessions action bar
        self._orphaned_session_dirs = [s.dir_name for s in (orphaned_sessions or [])]
        self._render_tree_actions()

        # Build a proper tree structure by inserting each path into a nested dict
        root_nodes: dict = {}  # nested dict structure

        for p in projects:
            parts = PurePath(p.path).parts  # ('/', 'home', 'bch', 'Project', ...)
            current = root_nodes
            for part in parts:
                if part not in current:
                    current[part] = {}
                current = current[part]
            # Mark this node as a project leaf
            current["__project__"] = p

        # Render the tree, collapsing long chains of single-child dirs
        self._render_tree_nodes(tree.root, root_nodes, "", all_paths)

        # Duplicate sessions (same session id in 2+ project dirs) — listed for
        # manual review/deletion, not auto-cleaned.
        self._render_duplicates(tree.root)

    def _render_duplicates(self, root) -> None:
        """Add a top-level group listing duplicated sessions and their copies."""
        dups = self._all_duplicates
        if not dups:
            return
        group = root.add(
            f"[bold yellow]⚠ {t('proj.duplicates_title')} ({len(dups)})[/]",
            data=("dup_group", None),
            expand=False,
        )
        for sid in sorted(dups):
            dirs = dups[sid]
            sid_node = group.add(
                f"[yellow]{sid[:12]}…[/]  [dim]({len(dirs)} {t('proj.copies')})[/]",
                data=("dup_session", sid),
                expand=True,
            )
            for dir_name in sorted(dirs):
                jsonl = PROJECTS_DIR / dir_name / f"{sid}.jsonl"
                try:
                    size = jsonl.stat().st_size if jsonl.exists() else 0
                except OSError:
                    size = 0
                hint = decode_path_hint(dir_name)
                sid_node.add_leaf(
                    f"{hint}  [dim]({format_bytes(size)})[/]",
                    data=("dup_copy", sid, dir_name),
                )

    def _render_tree_actions(self) -> None:
        """Render orphaned sessions action bar below tree."""
        bar = self.query_one("#tree-actions", ActionBar)
        sort_labels = {
            "path": t("proj.sort_path"),
            "cost": t("proj.sort_cost"),
            "status": t("proj.sort_status"),
        }
        actions = [("sort-projects", t("proj.btn_sort", mode=sort_labels[self._sort_mode]), "#555555")]
        count = len(self._orphaned_session_dirs)
        if count > 0:
            actions.append(("trash-orphaned-sessions", t("proj.btn_trash_orphaned", count=count), "#ba3c5b"))
        empty = len(getattr(self, "_empty_sessions", []))
        if empty > 0:
            actions.append(("trash-empty-sessions", t("proj.btn_trash_empty", count=empty), "#ba3c5b"))
        bar.set_actions(actions, on_action=self._handle_action)

    def _refresh_detail_actions(self) -> None:
        """Re-render detail actions for the selected project (e.g. after a save)."""
        path = getattr(self, "_selected_project_path", None)
        if path:
            encoded = encode_path(path)
            if len(project_paths_for_encoded(encoded)) > 1:
                self._render_detail_actions()
                return
            sd = PROJECTS_DIR / encoded
            session_count = len(list(sd.glob("*.jsonl"))) if sd.exists() else 0
            self._render_detail_actions(
                show_remove_config=(session_count == 0),
                instr_path=path if Path(path).is_dir() else None,
            )

    def _render_detail_actions(self, show_trash_session=False, show_remove_config=False, instr_path=None, show_move=False) -> None:
        """Render detail panel action bar."""
        actions = []
        if show_trash_session:
            actions.append(("trash-session", t("proj.btn_trash_session"), "#e8890c"))
        if show_move:  # Codex: re-assign session to another working dir
            actions.append(("move-codex-session", t("proj.btn_move_codex"), "#8B5CF6"))
            actions.append(("copy-codex-session", t("proj.btn_copy_codex"), "#0178d4"))
        # Per-project instruction files (edit existing, "+" to create).
        if instr_path:
            for fname in INSTRUCTION_FILES:
                exists = (Path(instr_path) / fname).exists()
                label = fname if exists else f"+ {fname}"
                color = "#0178d4" if exists else "#555555"
                actions.append((f"edit::{fname}", label, color))
        if show_remove_config:
            actions.append(("remove-config", t("proj.btn_remove_config"), "#ba3c5b"))
        bar = self.query_one("#detail-actions", ActionBar)
        bar.set_actions(actions, on_action=self._handle_action)

    def _render_tree_nodes(self, parent_node, node_dict: dict, current_path: str, all_paths: set):
        """Recursively render tree, collapsing single-child intermediate directories."""
        for key, children in node_dict.items():
            if key == "__project__":
                continue

            new_path = f"{current_path}/{key}" if current_path else key
            if new_path == "/":
                new_path = "/"

            is_project = "__project__" in children
            child_dirs = {k: v for k, v in children.items() if k != "__project__"}

            if is_project:
                p = children["__project__"]
                self._add_project_node(parent_node, p, key, child_dirs, all_paths)
            elif len(child_dirs) == 1 and not is_project:
                # Single child dir - collapse into parent label
                child_key = list(child_dirs.keys())[0]
                collapsed_label = f"{key}/{child_key}"
                collapsed_children = child_dirs[child_key]
                collapsed_path = f"{new_path}/{child_key}"

                # Keep collapsing while single child
                while (
                    "__project__" not in collapsed_children
                    and len({k for k in collapsed_children if k != "__project__"}) == 1
                ):
                    next_key = [k for k in collapsed_children if k != "__project__"][0]
                    collapsed_label = f"{collapsed_label}/{next_key}"
                    collapsed_children = collapsed_children[next_key]
                    collapsed_path = f"{collapsed_path}/{next_key}"

                is_collapsed_project = "__project__" in collapsed_children
                collapsed_child_dirs = {k: v for k, v in collapsed_children.items() if k != "__project__"}

                if is_collapsed_project:
                    p = collapsed_children["__project__"]
                    self._add_project_node(parent_node, p, collapsed_label, collapsed_child_dirs, all_paths)
                elif collapsed_child_dirs:
                    count = self._count_projects(collapsed_children)
                    group = parent_node.add(
                        f"[bold]{collapsed_label}/[/]  ({count})",
                        data=("group", collapsed_path),
                        expand=bool(self._filter_query),
                    )
                    self._render_tree_nodes(group, collapsed_children, collapsed_path, all_paths)
            else:
                # Multiple children - create group node
                count = self._count_projects(children)
                group = parent_node.add(
                    f"[bold]{key}/[/]  ({count})",
                    data=("group", new_path),
                    expand=bool(self._filter_query),
                )
                self._render_tree_nodes(group, children, new_path, all_paths)

    def _count_projects(self, node_dict: dict) -> int:
        """Count total project leaves in a subtree."""
        count = 1 if "__project__" in node_dict else 0
        for k, v in node_dict.items():
            if k != "__project__" and isinstance(v, dict):
                count += self._count_projects(v)
        return count

    def _add_project_node(self, parent, p: ProjectInfo, display_name: str, child_dirs: dict, all_paths: set):
        """Add a project node that can be expanded to show sessions."""
        status = "[green]O[/]" if p.exists else "[red]X[/]"
        encoded = encode_path(p.path)
        ambiguous = project_paths_for_encoded(encoded)
        if len(ambiguous) > 1:
            label = f"[yellow]ambiguous[/] {display_name}"
            parent.add_leaf(label, data=("project", p.path))
            return
        session_count = self._session_counts.get(encoded, 0)
        matched_sessions = self._session_match_map.get(p.path)
        # A full-text body search can surface Codex sessions for a working dir
        # that isn't in the recent-scan project list, so treat any matched-but-
        # Codex hit as evidence this node has Codex sessions to expand.
        has_codex = p.path in self._codex_paths or (
            bool(matched_sessions) and any(src == "codex" for _, src in matched_sessions)
        )
        # Source markers: C if it has Claude sessions, X if it has Codex sessions.
        marks = ""
        if session_count > 0:
            marks += "[#D97757]C[/]"
        if has_codex:
            marks += "[#10A37F]X[/]"
        marks_str = f"  {marks}" if marks else ""
        session_str = f"  [dim]{session_count} sessions[/]" if session_count > 0 else ""
        cost = self._project_costs.get(p.path, 0.0)
        cost_str = f"  [green]{_fmt_cost(cost)}[/]" if cost >= 0.005 else ""

        label = f"{status} {display_name}{marks_str}{session_str}{cost_str}"
        expandable = session_count > 0 or has_codex or bool(matched_sessions)
        if expandable or child_dirs:
            node = parent.add(label, data=("project", p.path), expand=bool(matched_sessions))
            if matched_sessions:
                # Title search hit: show only the matching sessions, pre-loaded.
                self._populate_sessions(node, matched_sessions)
            elif expandable:
                node.add_leaf("[dim]...[/]", data=("placeholder", None))
            if child_dirs:
                self._render_tree_nodes(node, {k: v for k, v in child_dirs.items()}, p.path, all_paths)
        else:
            parent.add_leaf(label, data=("project", p.path))

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        node_data = event.node.data
        if not node_data:
            return
        kind, value = node_data[0], node_data[1] if len(node_data) > 1 else None
        if kind == "project" and value:
            children = list(event.node.children)
            if any(c.data and c.data[0] == "placeholder" for c in children):
                self.run_worker(lambda: self._load_sessions(event.node, value), thread=True)

    def _tagged_sessions(self, project_path: str) -> list:
        """Claude + Codex sessions for one working directory, newest first."""
        tagged = [(s, "claude") for s in get_project_sessions(project_path)]
        if project_path in self._codex_paths:
            from asm.services import codex_data
            tagged += [(s, "codex") for s in codex_data.get_project_sessions(project_path)]
        tagged.sort(key=lambda t: t[0].last_modified, reverse=True)
        return tagged

    def _load_sessions(self, node, project_path: str) -> None:
        # Claude sessions + Codex sessions for the same working directory.
        tagged = self._tagged_sessions(project_path)
        self.app.call_from_thread(self._populate_sessions, node, tagged)

    def _populate_sessions(self, node, tagged) -> None:
        # Remove placeholder
        for child in list(node.children):
            if child.data and child.data[0] == "placeholder":
                child.remove()
        for s, source in tagged:
            ts = s.last_modified / 1000 if s.last_modified > 1e12 else s.last_modified
            dt = datetime.fromtimestamp(ts).strftime("%m/%d %H:%M") if ts > 0 else "?"
            size_kb = s.file_size / 1024
            summary = s.summary.replace("\n", " ")[:50] if s.summary else s.session_id[:12]
            tag = "[#D97757]C[/]" if source == "claude" else "[#10A37F]X[/]"
            sub = "  [magenta](sub-agent)[/]" if source == "claude" and s.session_id in self._subagent_only else ""
            label = f"{tag} [dim]{dt}[/]  [cyan]{escape(summary)}[/]{sub}  [dim]({size_kb:.0f}KB)[/]"
            node.add_leaf(label, data=("session", s.session_id, s.project_dir, source))

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        node_data = event.node.data
        if not node_data:
            return
        kind = node_data[0]

        if kind == "project":
            path = node_data[1]
            self._selected_project_path = path
            self._selected_session = None
            self._selected_session_node = None
            encoded = encode_path(path)
            if len(project_paths_for_encoded(encoded)) > 1:
                self._render_detail_actions()
                p = self._project_map.get(path)
                if p:
                    self._show_project_detail(p, 0)
                return
            session_dir = PROJECTS_DIR / encoded
            session_count = len(list(session_dir.glob("*.jsonl"))) if session_dir.exists() else 0
            self._render_detail_actions(
                show_trash_session=False,
                show_remove_config=(session_count == 0),
                instr_path=path if Path(path).is_dir() else None,
            )
            p = self._project_map.get(path)
            if p:
                self._show_project_detail(p, session_count)
        elif kind == "session":
            session_id = node_data[1]
            project_dir = node_data[2] if len(node_data) > 2 else None
            source = node_data[3] if len(node_data) > 3 else "claude"
            self._selected_session = (session_id, project_dir, source)
            self._selected_session_node = event.node
            # The owning project node carries the real working dir, which Claude
            # needs to resume (`claude -r <id>` only looks in its own project).
            parent = event.node.parent
            project_path = (
                parent.data[1] if parent and parent.data and parent.data[0] == "project" else None
            )
            self._render_detail_actions(
                show_trash_session=True,
                show_remove_config=False,
                show_move=(source == "codex"),
            )
            self.run_worker(
                lambda: self._load_messages(session_id, project_dir, source, project_path),
                thread=True,
            )
        elif kind == "dup_copy":
            session_id = node_data[1]
            project_dir = node_data[2]
            self._selected_session = (session_id, project_dir, "claude")
            self._selected_session_node = event.node
            self._selected_project_path = None
            self._render_detail_actions(
                show_trash_session=True,
                show_remove_config=False,
            )
            self._show_duplicate_detail(session_id, project_dir)
            self.run_worker(lambda: self._load_messages(session_id, project_dir, "claude"), thread=True)

    def _show_duplicate_detail(self, session_id: str, project_dir: str) -> None:
        """Show which project dirs hold copies of a duplicated session."""
        header = self.query_one("#project-detail-header", Static)
        body = self.query_one("#project-detail-body", Static)
        header.update(f"[bold yellow]{t('proj.duplicate_session')}[/] {session_id[:16]}…")
        dirs = self._all_duplicates.get(session_id, [])
        lines = [t("proj.duplicate_hint"), ""]
        for dir_name in sorted(dirs):
            jsonl = PROJECTS_DIR / dir_name / f"{session_id}.jsonl"
            try:
                size = jsonl.stat().st_size if jsonl.exists() else 0
            except OSError:
                size = 0
            mark = "[green]▸[/] " if dir_name == project_dir else "  "
            lines.append(f"{mark}{decode_path_hint(dir_name)}  [dim]({format_bytes(size)})[/]")
        body.update("\n".join(lines))

    def _show_project_detail(self, p: ProjectInfo, session_count: int = 0) -> None:
        header = self.query_one("#project-detail-header", Static)
        body = self.query_one("#project-detail-body", Static)
        status = t("proj.status_found") if p.exists else t("proj.status_missing")
        header.update(f"[bold]{PurePath(p.path).name}[/]")

        detail = (
            f"[bold]Path:[/] {p.path}\n"
            f"[bold]Status:[/] {status}\n"
            f"[bold]Sessions:[/] {session_count}\n"
        )

        # Instruction files (edit via the buttons below)
        if Path(p.path).is_dir():
            marks = []
            for fname in INSTRUCTION_FILES:
                present = (Path(p.path) / fname).exists()
                marks.append(f"[green]✓[/] {fname}" if present else f"[red]✗[/] [dim]{fname}[/]")
            detail += "\n[bold]Instructions:[/]\n  " + "   ".join(marks) + "\n"

        # Config 내용 표시
        config_data = load_claude_json().get("projects", {}).get(p.path, {})
        if config_data:
            detail += "\n[bold]Config:[/]\n"
            cost = config_data.get("lastCost")
            if cost is not None:
                detail += f"  Last Cost: ${cost:.4f}\n"
            duration = config_data.get("lastDuration")
            if duration:
                detail += f"  Last Duration: {duration / 1000:.1f}s\n"
            last_sid = config_data.get("lastSessionId", "")
            if last_sid:
                detail += f"  Last Session: {last_sid[:12]}...\n"
            mcp = config_data.get("mcpServers", {})
            if mcp:
                detail += f"  MCP Servers: {', '.join(mcp.keys())}\n"
            tools = config_data.get("allowedTools", [])
            if tools:
                detail += f"  Allowed Tools: {len(tools)}\n"
            model_usage = config_data.get("lastModelUsage", {})
            if model_usage:
                for model, usage in model_usage.items():
                    short = model.replace("claude-", "").split("-2025")[0].split("-2026")[0]
                    detail += f"  Model: {short} (${usage.get('costUSD', 0):.2f})\n"

        if session_count > 0:
            detail += f"\n{t('proj.sessions_hint')}"
        else:
            detail += f"\n{t('proj.no_sessions_hint')}"
        body.update(detail)

    def _load_messages(self, session_id: str, project_dir: str | None, source: str = "claude",
                        project_path: str | None = None) -> None:
        if source == "codex":
            from asm.services import codex_data
            messages = codex_data.get_session_messages(session_id, project_dir, limit=50)
            cwd = None  # Codex resumes by id, no working dir needed
        else:
            messages = get_session_messages(session_id, project_dir=project_dir, limit=50)
            # Prefer the real project path; fall back to decoding the dir name.
            cwd = project_path or (decode_path_hint(project_dir) if project_dir else None)
        self.app.call_from_thread(self._show_messages, session_id, messages, source, cwd, project_dir)

    def action_export_session(self) -> None:
        """Write the previewed session's full conversation to a markdown file."""
        target = getattr(self, "_preview_export", None)
        if not target:
            self.app.notify(t("proj.export_select_first"), severity="warning")
            return
        self.run_worker(lambda: self._do_export(*target), thread=True)

    def _do_export(self, session_id: str, source: str, project_dir: str | None) -> None:
        from datetime import datetime as _dt

        if source == "codex":
            from asm.services import codex_data
            messages = codex_data.get_session_messages(session_id, project_dir, limit=0)
        else:
            messages = get_session_messages(session_id, project_dir=project_dir, limit=0)
        if not messages:
            self.app.call_from_thread(self.app.notify, t("proj.no_messages"), severity="warning")
            return
        out_dir = Path.home() / "Desktop"
        if not out_dir.exists():
            out_dir = Path.home()
        stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
        out = out_dir / f"asm-session-{session_id[:8]}-{stamp}.md"
        counter = 1
        while out.exists():
            out = out_dir / f"asm-session-{session_id[:8]}-{stamp}_{counter}.md"
            counter += 1
        lines = [f"# Session {session_id}", f"_source: {source} · exported by asm_", ""]
        role_names = {"user": "User", "assistant": "Assistant", "meta": "Meta"}
        for m in messages:
            lines.append(f"## {role_names.get(m.get('type'), m.get('type', '?'))}")
            lines.append(m.get("content", ""))
            lines.append("")
        try:
            tmp = out.with_name(f".{out.name}.tmp")
            tmp.write_text("\n".join(lines), encoding="utf-8")
            tmp.replace(out)
        except OSError as exc:
            self.app.call_from_thread(self.app.notify, str(exc), severity="error")
            return
        self.app.call_from_thread(self.app.notify, t("proj.exported", path=str(out)))

    def action_resume_session(self) -> None:
        """Quit the TUI and resume the previewed session in its project dir."""
        target = getattr(self, "_preview_target", None)
        if not target:
            self.app.notify(t("proj.resume_select_first"), severity="warning")
            return
        self.app.resume_target = target
        self.app.exit()

    def _show_messages(self, session_id: str, messages: list[dict], source: str = "claude",
                       cwd: str | None = None, project_dir: str | None = None) -> None:
        header = self.query_one("#project-detail-header", Static)
        body = self.query_one("#project-detail-body", Static)
        header.update(f"[bold]Session:[/] {session_id[:16]}...")

        self._preview_target = (session_id, source, cwd)
        self._preview_export = (session_id, source, project_dir)
        resume_line = (
            f"[dim]↻ resume:[/] [cyan]asm resume {escape(session_id)}[/]"
            f"  [dim]({t('proj.resume_key_hint')})[/]"
        )

        if not messages:
            body.update(f"{resume_line}\n\n{t('proj.no_messages')}")
            return

        lines = [resume_line, ""]
        for m in messages:
            content = m["content"]
            if m["type"] == "user":
                lines.append(f"[bold green]User:[/]\n{escape(content)}\n")
            elif m["type"] == "meta":
                lines.append(f"[dim]{escape(content)}[/]")
            else:
                if len(content) > 500:
                    content = content[:500] + "..."
                lines.append(f"[bold cyan]Assistant:[/]\n{escape(content)}\n")
        body.update("\n".join(lines))

    def action_trash_session(self) -> None:
        self._handle_action("trash-session")

    def action_trash_orphaned(self) -> None:
        self._handle_action("trash-orphaned-sessions")

    def action_remove_config(self) -> None:
        self._handle_action("remove-config")

    def _handle_action(self, action_id: str) -> None:
        if action_id in ("move-codex-session", "copy-codex-session"):
            session = getattr(self, "_selected_session", None)
            if not session or len(session) < 3 or session[2] != "codex":
                return
            from asm.screens.input_dialog import InputDialog
            kind = "copy" if action_id.startswith("copy") else "move"
            self.app.push_screen(
                InputDialog(
                    t(f"proj.{kind}_codex_title"),
                    t(f"proj.{kind}_codex_label"),
                    placeholder="/home/you/project",
                ),
                callback=self._do_copy_codex if kind == "copy" else self._do_move_codex,
            )
            return
        if action_id.startswith("edit::"):
            fname = action_id.split("::", 1)[1]
            path = getattr(self, "_selected_project_path", None)
            if path:
                target = Path(path) / fname
                self.app.push_screen(
                    FileEditorScreen(target),
                    callback=lambda saved: self._refresh_detail_actions() if saved else None,
                )
            return
        if action_id == "sort-projects":
            self._sort_mode = {
                "path": "cost",
                "cost": "status",
                "status": "path",
            }[self._sort_mode]
            self._build_tree_from_state()
        elif action_id == "trash-session":
            session = getattr(self, "_selected_session", None)
            if not session:
                return
            session_id = session[0]
            self.app.push_screen(
                ConfirmScreen(
                    t("proj.confirm_trash_session", sid=f"{session_id[:16]}...")
                ),
                callback=lambda ok: self._do_trash_session() if ok else None,
            )
        elif action_id == "remove-config":
            path = getattr(self, "_selected_project_path", None)
            if not path:
                return
            self.app.push_screen(
                ConfirmScreen(
                    t("proj.confirm_remove_config", path=path)
                ),
                callback=lambda ok: self._do_remove_config(path) if ok else None,
            )
        elif action_id == "trash-orphaned-sessions":
            names = getattr(self, "_orphaned_session_dirs", [])
            if not names:
                self.app.notify(t("common.no_items"))
                return
            self.app.push_screen(
                ConfirmScreen(t("proj.confirm_trash_orphaned", count=len(names))),
                callback=lambda ok: self._do_trash_orphaned_sessions() if ok else None,
            )
        elif action_id == "trash-empty-sessions":
            empties = getattr(self, "_empty_sessions", [])
            if not empties:
                self.app.notify(t("common.no_items"))
                return
            self.app.push_screen(
                ConfirmScreen(t("proj.confirm_trash_empty", count=len(empties))),
                callback=lambda ok: self._do_trash_empty_sessions() if ok else None,
            )

    def _do_trash_empty_sessions(self) -> None:
        empties = list(getattr(self, "_empty_sessions", []))

        def _work():
            ok = fail = 0
            for e in empties:
                if trash_single_session_file(e["project_dir"], e["session_id"]):
                    ok += 1
                else:
                    fail += 1
            self.app.call_from_thread(self.app.notify, t("common.trash_bulk_ok", ok=ok, fail=fail))
            self.app.call_from_thread(self.refresh_data)
        self.run_worker(_work, thread=True)

    def _do_move_codex(self, new_cwd: str | None) -> None:
        new_cwd = (new_cwd or "").strip()
        session = getattr(self, "_selected_session", None)
        if not new_cwd or not session:
            return
        rollout_path = session[1]

        def _work():
            from asm.services import codex_data
            from asm.services.live import LiveSessionError
            try:
                ok = codex_data.move_session(rollout_path, new_cwd)
            except LiveSessionError as exc:
                self.app.call_from_thread(self.app.notify, str(exc), severity="error")
                return
            self.app.call_from_thread(self._on_codex_moved, ok)
        self.run_worker(_work, thread=True)

    def _do_copy_codex(self, new_cwd: str | None) -> None:
        new_cwd = (new_cwd or "").strip()
        session = getattr(self, "_selected_session", None)
        if not new_cwd or not session:
            return
        rollout_path = session[1]

        def _work():
            from asm.services import codex_data
            new_id = codex_data.copy_session(rollout_path, new_cwd)
            self.app.call_from_thread(self._on_codex_copied, new_id)
        self.run_worker(_work, thread=True)

    def _on_codex_copied(self, new_id: str | None) -> None:
        if new_id:
            self.app.notify(t("proj.copy_codex_ok", sid=new_id[:8]))
            self.refresh_data()
        else:
            self.app.notify(t("common.failed"), severity="error")

    def _on_codex_moved(self, ok: bool) -> None:
        if ok:
            self.app.notify(t("proj.move_codex_ok"))
            self._selected_session = None
            self.refresh_data()
        else:
            self.app.notify(t("common.failed"), severity="error")

    def _do_trash_session(self) -> None:
        session = getattr(self, "_selected_session", None)
        if not session:
            return
        session_id, project_dir = session[0], session[1]
        source = session[2] if len(session) > 2 else "claude"
        if not project_dir:
            self.app.notify(t("proj.no_dir_info"), severity="error")
            return
        node = getattr(self, "_selected_session_node", None)

        def _work():
            if source == "codex":
                from asm.services.cleaner import trash_codex_session
                ok = trash_codex_session(project_dir)  # project_dir holds the rollout path
            else:
                ok = trash_single_session_file(project_dir, session_id)
            self.app.call_from_thread(self._on_session_trashed, ok, session_id, node)
        self.run_worker(_work, thread=True)

    def _on_session_trashed(self, ok: bool, session_id: str, node) -> None:
        if ok:
            self.app.notify(t("proj.trash_ok", sid=f"{session_id[:12]}..."))
            self._selected_session = None
            if node:
                node.remove()
            self._render_detail_actions(show_trash_session=False, show_remove_config=False)
        else:
            self.app.notify(t("proj.trash_fail"), severity="error")

    def _do_remove_config(self, path: str) -> None:
        def _work():
            backup_path = create_config_backup()
            from asm.services import backup

            if backup.CLAUDE_JSON.exists() and not backup_path:
                self.app.call_from_thread(self.app.notify, t("proj.config_fail"), severity="error")
                return
            if remove_project_from_json(path):
                self.app.call_from_thread(self.app.notify, t("proj.config_removed", path=path))
                self.app.call_from_thread(self.refresh_data)
            else:
                self.app.call_from_thread(self.app.notify, t("proj.config_fail"), severity="error")
        self.run_worker(_work, thread=True)

    def _do_trash_orphaned_sessions(self) -> None:
        names = getattr(self, "_orphaned_session_dirs", [])
        def _work():
            ok, fail = trash_sessions(names)
            self.app.call_from_thread(self.app.notify, t("common.trash_bulk_ok", ok=ok, fail=fail))
            self.app.call_from_thread(self.refresh_data)
        self.run_worker(_work, thread=True)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "projects-filter":
            return
        self._filter_query = event.value.strip()
        # Drop stale body-scan results and re-arm the debounced full-text search.
        # Titles still match instantly; body matches fold in when the scan lands.
        self._content_claude_ids = None
        self._content_main_ids = set()
        self._content_codex_by_cwd = {}
        self._schedule_content_search()
        self._build_tree_from_state()
