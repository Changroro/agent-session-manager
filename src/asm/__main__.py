"""Entry point for asm."""

import argparse
import sys

from asm.cli import add_cli_subparsers, run_cli
from asm.i18n import init_lang
from asm.models import migrate_legacy_data_dir


_EXAMPLES = """\
examples:
  asm                          open the TUI (Claude + Codex together)
  asm cost --period weekly     cost & token stats per model
  asm sessions --search auth   find sessions by title
  asm resume <session-id>      cd into the session's project and resume it
  asm clean empty --dry-run    preview empty-session cleanup
  asm backup create --type full
  asm recovery list            snapshots taken before deletions
"""


def main():
    parser = argparse.ArgumentParser(
        prog="asm",
        description="asm: manage Claude Code & Codex sessions, cost and data "
        "(run without a subcommand to open the TUI)",
        epilog=_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--path",
        type=str,
        default=None,
        help="Specific project path to manage (default: global ~/.claude)",
    )
    parser.add_argument(
        "--lang",
        type=str,
        default=None,
        choices=["en", "ko"],
        help="UI language (default: en, or set ASM_LANG env var)",
    )
    parser.add_argument(
        "--source",
        type=str,
        default="all",
        choices=["all", "claude", "codex"],
        help="Initial dashboard source filter: all (default), claude, or codex. "
             "Both sources are always shown; toggle in-app with 's'.",
    )
    parser.add_argument(
        "--no-update-check",
        action="store_true",
        help="Skip the startup check for a newer release.",
    )
    add_cli_subparsers(parser)
    args = parser.parse_args()

    migrate_legacy_data_dir()

    if getattr(args, "command", None):
        sys.exit(run_cli(args))

    # TUI path — import lazily so CLI subcommands stay fast.
    from asm.app import CCTuiApp
    from asm.services.update import maybe_prompt_update

    init_lang(args.lang)
    if not args.no_update_check:
        maybe_prompt_update()

    app = CCTuiApp(target_path=args.path, source=args.source)
    app.run()

    # Hand off to `claude -r` / `codex resume` after the TUI exits via the
    # resume action ('o' on a previewed session).
    if app.resume_target:
        from asm.cli import _exec_resume

        session_id, source, cwd = app.resume_target
        argv = ["codex", "resume", session_id] if source == "codex" else ["claude", "-r", session_id]
        sys.exit(_exec_resume(argv, cwd, dry_run=False))


if __name__ == "__main__":
    main()
