<div align="center">

# agent-session-manager

**One terminal dashboard for everything Claude Code and Codex leave behind.**
See cost, browse sessions, and clean up `~/.claude` and `~/.codex` — side by side, with a Claude / Codex filter.

[![PyPI](https://img.shields.io/pypi/v/agent-session-manager?style=flat-square&color=blue)](https://pypi.org/project/agent-session-manager/)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Built with Textual](https://img.shields.io/badge/Built%20with-Textual-5A2CA0?style=flat-square)](https://github.com/Textualize/textual)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20Windows-orange?style=flat-square)](#)
[![Status](https://img.shields.io/badge/Status-Personal--use-lightgrey?style=flat-square)](#)

`asm` · **[한국어](README.ko.md)**

</div>

---

> **⚠️ Heads up**
> This is a personal tool that reads and edits the **internal data of Claude Code and OpenAI Codex** (`~/.claude`, `~/.codex`). Every delete goes to the OS trash and is logged, but you should still back up before bulk operations. Not affiliated with Anthropic or OpenAI.

---

## What it is

If you live in **Claude Code** and **Codex**, the two of them quietly fill `~/.claude` and `~/.codex` with session transcripts, cost logs, debug files, task lists, and snapshots. After a few weeks it's impossible to tell which projects burned the most tokens, or which files are orphaned and safe to delete.

**agent-session-manager** (`asm`) puts all of it in one terminal dashboard. Both agents show up together, and a single keystroke filters the view to **All / Claude / Codex** so you can compare spend or drill into one tool.

### 💡 Why it exists

- **Problem:** cost and session data for two different coding agents live in two opaque directories with no shared view.
- **Solution:** one TUI that reads both, prices every model accurately, and lets you clean up safely — to the trash, with recovery snapshots.

---

## Demo

The combined dashboard (cost across both agents, source-tagged rows), then the
same view filtered to **Claude** and to **Codex**:

<img src="docs/demo.gif" alt="asm demo" width="820"/>

---

## ✨ Features

### Combined dashboard
- **Cost across both agents** with a source filter — **click** `All / Claude / Codex` or press `s`
- Per-model token & cost breakdown (Opus / Sonnet / Haiku / GPT-5.x), each row tagged by source
- Daily / weekly / monthly usage tables (one scan, all periods) and a Top-10 project cost chart
- Accurate pricing fetched **live from LiteLLM** (15-min cache, offline fallback to the bundled table) — new models are priced correctly without waiting for a release, and the dashboard always shows which rate source was used
- **Persistent usage ledger** (`~/.asm/usage.db`): sessions are parsed once and re-scanned only when they change, each one valued at the rates in effect when it was scanned (old sessions are never re-priced), and **cost history survives** even after Claude Code prunes old transcripts — Codex history is indexed in full, no recent-N cap

### Unified sessions (Claude + Codex)
- One tree: Claude projects and Codex working directories together, each session tagged **C** / **X**
- Expand a project to preview conversations from either agent; trash individual sessions
- **Edit instructions:** per project, view/edit/create `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, `AGENTS.local.md` in a built-in editor (Ctrl+S to save)
- **Move a Codex session** to another working directory (rewrites its `cwd`, which is how `codex resume --cd` associates sessions)
- **Orphan cleanup:** detect and bulk-clean Claude sessions, file-history, debug, and task entries with no matching project
- **Duplicate sessions:** find the same session copied across projects and delete individual copies
- **Empty sessions:** clean stub sessions that hold only a title/metadata and no conversation (can't be resumed)
- **Move / Copy:** move sessions to another project (same id; originals to trash with a recovery snapshot) or copy them under new ids (originals stay). Subagent/tool-result folders included, paths auto-rewritten. `relocate` moves the project folder itself too and lists files that still mention the old path. Sessions a running Claude Code / Codex is still using are refused

### Artifacts
- Browse every page you've published with Claude Code's **Artifact tool** — scanned straight out of your session history, newest first
- Open in the browser (`Enter`/`o`) or copy the URL (`c`) without leaving the terminal; also available as `asm artifacts` (`--json` for scripts)

### Claude ↔ Codex (`F8` tab)
- **MCP servers, both ways:** compares `~/.codex/config.toml` with `~/.claude.json`, lists what only one side has, and adds it through that agent's own `mcp add` CLI (so file format and comments stay intact). Auth headers and env vars are carried over verbatim; `codex mcp add` can't express HTTP headers, so those are appended as `[mcp_servers.<name>.http_headers]`
- **Session copies, both ways:** Claude transcripts become Codex rollout threads and vice versa — verified end-to-end by resuming the result in the real CLI. Claude→Codex writes the same `content_sha256` ledger the official importer uses, so it won't duplicate what Codex already imported, and unlike `/import` you pick the sessions (no 30-day / 50-session cap)
- Imported copies carry **zeroed token usage**, so a copied session is never priced twice
- Everything is planned before it's written: a preview lists importable / skipped / unsupported items, and a backup snapshot is taken before the first write
- **Nothing is preselected.** A plan covers the newest 200 transcripts (hashing every one of 25k+ rollouts would be slow) and states how many older ones it left out, so a truncated list never reads as "that's all of them"

### Safe by default
- Every delete goes to the **OS trash** and is recorded in an audit log
- **Recovery snapshots** are taken before trashing (Claude and Codex), with a size/age cap so they don't pile up
- Backups **and restore** for Claude (config / settings / plugins / sessions / full) and Codex (sessions, excluding huge caches); restore is rollback-safe (the live dir is moved aside and put back if the copy fails), and credential-bearing backups are written owner-only (0600)

---

## How it works

```
   ~/.claude  ┐
              ├──►  asm  ──►  one dashboard  ──►  filter: All / Claude / Codex
   ~/.codex   ┘              (cost · sessions · cleanup · backup)
```

`asm` reads both data directories directly — no daemon, no config. Claude data is grouped by project; Codex sessions are grouped by working directory. Pricing is computed from each session's recorded token usage.

| Path | What's there |
|:---|:---|
| `~/.claude.json` · `~/.claude/projects/` | Claude projects, costs, session JSONL |
| `~/.claude/file-history/` · `debug/` · `tasks/` | Snapshots, debug logs, per-session task lists |
| `~/.codex/sessions/` | Codex rollout session files |
| `~/.asm/backups/` · `trash-log.jsonl` · `usage.db` | Backups (auto-migrated from old `~/.cc-tui`), the deletion audit log, and the usage ledger |

---

## 🛠️ Tech Stack

- **TUI:** [Textual](https://github.com/Textualize/textual) + [Rich](https://github.com/Textualize/rich)
- **Safety:** [send2trash](https://github.com/arsenetar/send2trash) (OS trash, not `rm`)
- **Sessions:** built-in JSONL parser — no heavy deps; optional [claude-agent-sdk](https://pypi.org/project/claude-agent-sdk/) via `pip install 'agent-session-manager[sdk]'`
- **Python:** 3.11+

---

## 🚀 Getting started

### Install (recommended)

```bash
# uv
uv tool install agent-session-manager

# pip
pip install agent-session-manager
```

Both install a single `asm` command. On first run, data from the previous `~/.cc-tui` location is migrated to `~/.asm` automatically.

<details>
<summary><strong>Run from source</strong></summary>

```bash
git clone https://github.com/Changroro/agent-session-manager.git
cd agent-session-manager
uv sync && uv run asm
```

</details>

### Usage

```bash
asm                       # Launch — shows Claude + Codex together
asm --source codex        # Start with the dashboard filtered to Codex
asm --path /your/project  # Limit to one Claude project
asm --lang ko             # Korean UI  (or set ASM_LANG=ko)
asm --no-update-check     # Skip the startup update check
```

Both sources are always available; `--source` only sets the dashboard's initial filter, which you change anytime with `s`.

### Headless CLI

Data operations have headless subcommands for scripts and AI agents. Instruction editing, Codex cwd moves, and Artifact browser/clipboard actions remain TUI actions. In a terminal you get rich tables; piped output is plain text, and `--json` gives machine-readable output.

```bash
# Read-only
asm cost --period weekly          # Cost/token stats per model & period
asm projects                      # All projects (Claude + Codex)
asm find "firewall"               # Find sessions by title + content (--only-title / --only-content)
asm preview <session-id>          # Print a conversation
asm resume <session-id>           # cd into its project & resume (Claude/Codex; --project if the id is duplicated)
asm artifacts                     # Pages published via the Artifact tool
asm import list --to claude       # Sessions that could be copied Codex -> Claude
asm backup list / asm recovery list

# Destructive — always confirms first (skip with --yes); everything goes
# through the OS trash + recovery snapshots, same as the TUI.
asm clean empty --dry-run         # empty | orphaned | debug | todos
asm trash <session-id>
asm backup create --type full     # config|full|settings|plugins|sessions|codex
asm backup restore <path> / asm recovery restore <id>
asm move /old/project /new/project                # move all its sessions (Claude + Codex); originals to trash
asm move /old/project /new/project --sessions 3f2a1b9c   # only these (ids or unique prefixes)
asm copy /old/project /new/project --sessions 3f2a1b9c   # duplicate under new session ids; originals stay
asm relocate /old/project /new/project --dry-run          # move the folder itself + sessions + ~/.claude.json / Codex config keys
asm import session <session-id>   # Copy one session; direction inferred from the id
asm import mcp --to codex         # Copy MCP servers between agents
```

Codex App, CLI, and companion agents share one session store — see
[Codex data home](docs/codex-data-home.md).

`asm import list` sorts by **last activity**, not by the timestamp in a Codex
rollout filename (that one is when the session *started*), and prints that
activity time so the ordering is visible. `asm import --help` spells out the
rest — the 200-transcript scan window, the new id given to each copy, and how
the destination folder is picked from the session's own `cwd`.

### Keyboard

| Key | Action |
|:---:|:---|
| `F1`–`F8` | Switch tabs |
| `s` / click | Dashboard source filter (All / Claude / Codex) |
| `Tab` / `Shift+Tab` · `1` `2` `3` | Dashboard period (Daily / Weekly / Monthly) |
| `d` / `D` | Trash selected / all orphaned |
| `Space` · `Enter` | Toggle selection · Preview conversation |
| `r` · `q` | Refresh all · Quit |

### Staying up to date

When a newer release is on PyPI, `asm` offers a `y/N` upgrade prompt on startup (via `uv tool` or `pip`). It's skipped in non-interactive shells and when offline.

---

## 🗺️ Roadmap

- [ ] Per-source disk-usage in the data overview
- [ ] Import hooks and subagents (the surfaces neither agent maps automatically)
- [ ] ruff + mypy in CI

---

## ⚠️ Status & scope

- **Personal / pre-release**, under active development.
- Operates directly on Claude Code and Codex internal data — **back up before bulk deletes**.
- Deletes go to the OS trash with recovery snapshots; nothing is `rm`'d in place.
- No warranty. Not affiliated with Anthropic or OpenAI.

---

## 📄 License

[MIT](LICENSE)

<div align="center">
<br/>
Made with <b>Claude Code</b> · and now <b>Codex</b> too
</div>
