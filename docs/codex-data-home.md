# Codex data home

Codex App, Codex CLI, and companion agents use **one shared `CODEX_HOME`**.
The default is `~/.codex`; asm reads exactly that one home and never scans
sibling `~/.codex-*` directories.

Account selection is an execution concern handled by the Codex app or its
configured provider/proxy. It does not create a separate project/session store
per account. Projects, rollouts, usage, and task history remain in the shared
home.

## Check the active home

```console
$ asm import home
Codex home: /home/you/.codex  25190 sessions
```

The standard `CODEX_HOME` environment variable selects a different home for one
process. This is intended for inspecting or importing **legacy/archive data**,
not for aggregating several active homes:

```bash
CODEX_HOME="$HOME/.codex-old" asm import list --to claude
CODEX_HOME="$HOME/.codex-old" asm import session <id> --dry-run
```

asm keeps that run isolated to the named home. It does not merge its dashboard,
costs, trash, backup, or recovery scope with the default home.

## Legacy split-home directories

A directory such as `~/.codex-hermes` may remain after an older launcher setup.
Its presence does not make it active. Do not infer activity from the directory
name; inspect the running app-server's `CODEX_HOME` or use `asm import home` in
the relevant process.

Previously ingested usage rows from a legacy home remain in the ledger because
asm deliberately preserves history after transcript deletion. Normal dashboard
queries filter those rows out by the active home path, so they are retained for
forensics without being included in current totals.
