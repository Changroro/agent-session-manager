"""Internationalization support for asm."""

from __future__ import annotations

import os

_current_lang = "en"

TRANSLATIONS: dict[str, dict[str, str]] = {
    # ── App ──
    "app.refreshed": {"en": "Refreshed", "ko": "새로고침 완료"},
    # Tab names
    "tab.dashboard": {"en": "Dashboard", "ko": "대시보드"},
    "tab.projects": {"en": "Projects", "ko": "프로젝트"},
    "tab.file_history": {"en": "File History", "ko": "파일 히스토리"},
    "tab.debug_todos": {"en": "Debug/Todos", "ko": "디버그/Todos"},
    "tab.migrate": {"en": "Move/Copy", "ko": "이동/복사"},
    "tab.backups": {"en": "Backups", "ko": "백업"},
    "tab.artifacts": {"en": "Artifacts", "ko": "아티팩트"},
    "tab.agent_import": {"en": "Claude ↔ Codex", "ko": "Claude ↔ Codex"},
    "imp.loading": {
        "en": "Scanning Claude Code and Codex for movable sessions and MCP servers...",
        "ko": "Claude Code와 Codex에서 옮길 수 있는 세션·MCP 서버를 스캔 중...",
    },
    "imp.header": {
        "en": "{mode}  ·  {pending} copyable, {selected} selected  ·  1-4: direction  ·  space: pick  ·  a: all/none  ·  i: copy",
        "ko": "{mode}  ·  복사 가능 {pending}개, 선택 {selected}개  ·  1-4: 방향  ·  space: 선택  ·  a: 전체/해제  ·  i: 복사",
    },
    "imp.new": {"en": "copyable", "ko": "복사 가능"},
    "imp.skip": {"en": "skipped", "ko": "건너뜀"},
    "imp.nothing": {"en": "Nothing selected to import.", "ko": "가져올 항목이 선택되지 않았습니다."},
    "imp.confirm": {
        "en": "Copy {count} item(s) via {mode}? The source stays in place; a backup snapshot is taken first.",
        "ko": "{mode}(으)로 {count}개를 복사할까요? 원본은 유지되며 먼저 백업 스냅샷을 만듭니다.",
    },
    "imp.done": {
        "en": "Imported {imported}, failed {failed}",
        "ko": "가져오기 {imported}개, 실패 {failed}개",
    },
    "imp.failed_item": {"en": "{item}: {reason}", "ko": "{item}: {reason}"},
    "imp.error": {"en": "Import error: {error}", "ko": "가져오기 오류: {error}"},
    "imp.truncated": {
        "en": "({count} older ones not listed)",
        "ko": "(오래된 {count}개는 목록에 없음)",
    },
    "art.loading": {"en": "Scanning sessions for published artifacts...", "ko": "발행된 아티팩트를 세션에서 스캔 중..."},
    "art.none": {
        "en": "No artifacts published yet — pages published with Claude Code's Artifact tool will show up here.",
        "ko": "발행된 아티팩트가 없습니다 — Claude Code의 Artifact 도구로 발행한 페이지가 여기에 표시됩니다.",
    },
    "art.header": {
        "en": "{count} artifacts  ·  Enter/o: open in browser  ·  c: copy URL",
        "ko": "아티팩트 {count}개  ·  Enter/o: 브라우저로 열기  ·  c: URL 복사",
    },
    "art.opened": {"en": "Opened {url}", "ko": "{url} 열림"},
    "art.copied": {"en": "Copied {url}", "ko": "{url} 복사됨"},
    # Codex sessions
    # Confirm dialog
    "confirm.yes": {"en": "Confirm (y)", "ko": "확인 (y)"},
    "confirm.no": {"en": "Cancel (n)", "ko": "취소 (n)"},
    "confirm.bulk_delete": {
        "en": "Delete {count} selected {type}?",
        "ko": "선택한 {count}개 {type}를 삭제하시겠습니까?",
    },
    "confirm.group_delete": {
        "en": "Delete {count} {type} in this group?",
        "ko": "이 그룹의 {count}개 {type}를 삭제하시겠습니까?",
    },
    # ── Dashboard ──
    "dash.loading": {"en": "Loading dashboard data...", "ko": "대시보드 데이터 로딩 중..."},
    "dash.scanning": {
        "en": "Indexing session history {progress} (one-time)...",
        "ko": "세션 기록 인덱싱 {progress} (최초 1회)...",
    },
    "dash.loading_period": {"en": "Loading...", "ko": "로딩 중..."},
    "dash.total_cost": {"en": "Total Cost", "ko": "총 비용"},
    "dash.top_projects": {"en": "Top Projects by Cost", "ko": "비용 상위 프로젝트"},
    "dash.top10": {"en": "Top 10", "ko": "상위 10개"},
    "dash.data_overview": {"en": "Data Overview", "ko": "데이터 개요"},
    "dash.no_data": {"en": "(no data)", "ko": "(데이터 없음)"},
    # ── Projects ──
    "proj.info": {
        "en": (
            "[bold]Projects[/] - .claude.json projects + sessions\n"
            "[dim]Expand folder to see sessions. Click session to preview conversation.\n"
            "[green]O[/]=folder exists  [red]X[/]=folder deleted/moved (config only)[/]"
        ),
        "ko": (
            "[bold]Projects[/] - .claude.json 프로젝트 + 세션\n"
            "[dim]폴더를 펼치면 세션 목록이 보입니다. 세션을 클릭하면 대화 내용을 미리봅니다.\n"
            "[green]O[/]=폴더 존재  [red]X[/]=폴더 삭제/이동됨(설정만 남음)[/]"
        ),
    },
    "proj.select_hint": {
        "en": "Select a project or session",
        "ko": "프로젝트 또는 세션을 선택하세요",
    },
    "proj.filter_placeholder": {
        "en": "Filter by path or session title...",
        "ko": "경로 또는 세션 제목으로 필터...",
    },
    "proj.sort_path": {"en": "Path", "ko": "경로"},
    "proj.sort_cost": {"en": "Cost", "ko": "비용"},
    "proj.sort_status": {"en": "Status", "ko": "상태"},
    "proj.btn_sort": {"en": "Sort: {mode}", "ko": "정렬: {mode}"},
    "proj.btn_trash_session": {"en": "Delete Session", "ko": "세션 삭제"},
    "proj.btn_remove_config": {"en": "Remove from Config", "ko": "설정에서 제거"},
    "proj.status_found": {"en": "[green]Found[/]", "ko": "[green]존재[/]"},
    "proj.status_missing": {
        "en": "[red]Missing[/] (folder deleted/moved)",
        "ko": "[red]Missing[/] (폴더 삭제/이동됨)",
    },
    "proj.sessions_hint": {
        "en": "[dim]To delete sessions, expand the project and select individual sessions.[/]",
        "ko": "[dim]세션을 삭제하려면 프로젝트를 펼쳐서 개별 세션을 선택하세요.[/]",
    },
    "proj.no_sessions_hint": {
        "en": "[dim]No sessions. You can remove this from config.[/]",
        "ko": "[dim]세션이 없으므로 설정에서 제거할 수 있습니다.[/]",
    },
    "proj.confirm_trash_session": {
        "en": "Delete this session?\n\nSession: {sid}\n\n[bold]Only this 1 session will be deleted.[/]\n[dim]Moved to trash, recoverable[/]",
        "ko": "이 세션을 삭제하시겠습니까?\n\nSession: {sid}\n\n[bold]이 세션 1개만 삭제됩니다.[/]\n[dim]휴지통으로 이동되며 복구 가능합니다[/]",
    },
    "proj.confirm_remove_config": {
        "en": "Remove project from config?\n\nPath: {path}\n\n[dim]No sessions. Only removes from .claude.json.[/]",
        "ko": "설정에서 프로젝트를 제거하시겠습니까?\n\nPath: {path}\n\n[dim]세션이 없는 프로젝트입니다.\n.claude.json 목록에서만 제거됩니다.[/]",
    },
    "proj.trash_ok": {"en": "Session deleted: {sid}", "ko": "세션 삭제 완료: {sid}"},
    "proj.trash_fail": {"en": "Delete failed", "ko": "삭제 실패"},
    "proj.config_removed": {"en": "Removed from config: {path}", "ko": "설정에서 제거됨: {path}"},
    "proj.config_fail": {"en": "Remove failed", "ko": "제거 실패"},
    "proj.no_dir_info": {"en": "No project directory info", "ko": "프로젝트 디렉토리 정보 없음"},
    "proj.no_messages": {"en": "[dim]No conversation messages found[/]", "ko": "[dim]대화 메시지 없음[/]"},
    "proj.resume_key_hint": {"en": "press o to resume now", "ko": "o 키로 바로 이어가기"},
    "proj.exported": {"en": "Exported: {path}", "ko": "내보내기 완료: {path}"},
    "proj.export_select_first": {
        "en": "Select a session first (Enter to preview), then press e to export",
        "ko": "먼저 세션을 선택(Enter로 미리보기)한 뒤 e 키로 내보내세요",
    },
    "proj.resume_select_first": {
        "en": "Select a session first (Enter to preview), then press o to resume",
        "ko": "먼저 세션을 선택(Enter로 미리보기)한 뒤 o 키로 이어가세요",
    },
    "proj.btn_trash_orphaned": {"en": "Trash Orphaned Sessions ({count})", "ko": "Orphaned 세션 삭제 ({count})"},
    "proj.confirm_trash_orphaned": {
        "en": "Delete all orphaned session directories?\n\n{count} dirs with no matching project.\n[dim]Moved to trash, recoverable[/]",
        "ko": "Orphaned 세션 디렉토리를 모두 삭제?\n\n매칭 프로젝트 없는 {count}개.\n[dim]휴지통으로 이동, 복구 가능[/]",
    },
    "proj.btn_trash_empty": {"en": "Clean Empty Sessions ({count})", "ko": "빈 세션 정리 ({count})"},
    "proj.confirm_trash_empty": {
        "en": "Trash {count} empty session(s)?\n\nThese have only a title / metadata and no conversation (cannot be resumed).\n[dim]Moved to trash, recoverable[/]",
        "ko": "빈 세션 {count}개를 삭제할까요?\n\n제목/메타만 있고 대화가 없는(resume 불가) 세션입니다.\n[dim]휴지통으로 이동, 복구 가능[/]",
    },
    "proj.btn_move_codex": {"en": "Move [m]", "ko": "이동 [m]"},
    "proj.move_codex_title": {"en": "Move Codex session", "ko": "Codex 세션 이동"},
    "proj.move_codex_label": {
        "en": "New working directory (rewrites the session's cwd):",
        "ko": "새 작업 디렉토리 (세션의 cwd를 재작성):",
    },
    "proj.move_codex_ok": {"en": "Session moved (cwd updated)", "ko": "세션 이동됨 (cwd 갱신)"},
    "proj.btn_copy_codex": {"en": "Copy", "ko": "복사"},
    "proj.copy_codex_title": {"en": "Copy Codex session", "ko": "Codex 세션 복사"},
    "proj.copy_codex_label": {
        "en": "Working directory for the copy (gets a new session id):",
        "ko": "복사본의 작업 디렉토리 (새 세션 id로 생성):",
    },
    "proj.copy_codex_ok": {"en": "Session copied as {sid}", "ko": "세션 복사됨: {sid}"},
    "proj.duplicates_title": {"en": "Duplicate Sessions", "ko": "중복 세션"},
    "proj.copies": {"en": "copies", "ko": "곳"},
    "proj.duplicate_session": {"en": "Duplicate session", "ko": "중복 세션"},
    "proj.duplicate_hint": {
        "en": "[dim]Same session id exists in multiple project dirs. Select a copy and press [b]d[/] to delete just that one.[/]",
        "ko": "[dim]같은 세션 id가 여러 프로젝트 디렉토리에 존재합니다. 복사본을 선택하고 [b]d[/]를 눌러 해당 복사본만 삭제하세요.[/]",
    },
    # ── File History ── detail panel status descriptions
    "fh.status_orphaned_desc": {
        "en": (
            "[yellow bold]Orphaned[/]\n"
            "[dim]Session deleted, no matching project.\n"
            "Safe to delete.[/]"
        ),
        "ko": (
            "[yellow bold]Orphaned[/]\n"
            "[dim]세션이 삭제되어 매칭되지 않습니다.\n"
            "안전하게 삭제 가능합니다.[/]"
        ),
    },
    "fh.status_active_desc": {
        "en": (
            "[green bold]Active[/]\n"
            "[dim]Connected to a current project.\n"
            "Deleting will disable file rollback for this project.[/]"
        ),
        "ko": (
            "[green bold]Active[/]\n"
            "[dim]현재 프로젝트와 연결되어 있습니다.\n"
            "삭제 시 해당 프로젝트의 파일 되돌리기 기능을 사용할 수 없습니다.[/]"
        ),
    },
    # ── File History ──
    "fh.info": {
        "en": (
            "[bold]File History[/] - Version history/snapshots of files edited by Claude\n"
            "[dim]Stored in ~/.claude/file-history/. Used for file rollback; old entries can be cleaned.\n"
            "[yellow]Orphaned[/]=no matching project found[/]"
        ),
        "ko": (
            "[bold]File History[/] - Claude가 편집한 파일의 버전 히스토리/스냅샷\n"
            "[dim]~/.claude/file-history/ 에 저장됩니다. 파일 되돌리기용이므로 오래된 것은 정리해도 됩니다.\n"
            "[yellow]Orphaned[/]=프로젝트가 삭제/이동되어 매칭되지 않는 히스토리[/]"
        ),
    },
    "fh.select_hint": {
        "en": "Select a file history entry to see details",
        "ko": "파일 히스토리를 선택하면 상세 정보가 표시됩니다",
    },
    "fh.filter_placeholder": {
        "en": "Filter file history by project / session...",
        "ko": "프로젝트 / 세션으로 파일 히스토리 필터...",
    },
    "fh.sort_project": {"en": "Project", "ko": "프로젝트"},
    "fh.sort_status": {"en": "Status", "ko": "상태"},
    "fh.sort_session": {"en": "Session", "ko": "세션"},
    "fh.btn_sort": {"en": "Sort: {mode}", "ko": "정렬: {mode}"},
    "fh.btn_trash": {"en": "Trash Selected", "ko": "선택 항목 삭제"},
    "fh.confirm_trash": {
        "en": "Move file history to trash?\n\n{name}\n[dim]Moved to trash, recoverable[/]",
        "ko": "File history를 휴지통으로 이동?\n\n{name}\n[dim]휴지통으로 이동되며 복구 가능합니다[/]",
    },
    "fh.btn_trash_orphaned": {"en": "Trash All Orphaned ({count})", "ko": "Orphaned 전체 삭제 ({count})"},
    "fh.confirm_trash_orphaned": {
        "en": "Delete all orphaned file history?\n\n{count} orphaned entries.\n[dim]Moved to trash, recoverable[/]",
        "ko": "Orphaned 파일 히스토리를 모두 삭제?\n\n{count}개.\n[dim]휴지통으로 이동, 복구 가능[/]",
    },
    # ── Orphaned ──
    # ── Debug/Todos ──
    "dt.info": {
        "en": (
            "[bold]Debug / Todos[/] - Claude Code internal file management\n"
            "[dim]Debug: Internal debug logs created on errors (unnecessary except for bug reports)\n"
            "Todos: Internal task memos created during sessions (not user todos, unnecessary after session)\n"
            "[yellow]Orphaned[/]=session deleted → safe to delete\n"
            "Select an item to preview its content on the right.[/]"
        ),
        "ko": (
            "[bold]Debug / Todos[/] - Claude Code 내부 파일 관리\n"
            "[dim]Debug: Claude Code 오류 시 생성되는 [bold]내부 디버그 로그[/dim][dim] (버그 리포트 외 불필요)\n"
            "Todos: Claude가 세션 중 생성하는 [bold]내부 작업 메모[/dim][dim] (사용자 todo 아님, 세션 끝나면 불필요)\n"
            "[yellow]Orphaned[/]=해당 세션이 삭제됨 → 안전하게 삭제 가능\n"
            "항목을 선택하면 오른쪽에 내용 미리보기가 표시됩니다.[/]"
        ),
    },
    "dt.select_file": {"en": "Select a file to preview", "ko": "파일을 선택하세요"},
    "dt.filter_placeholder": {
        "en": "Filter debug / todo by file or project...",
        "ko": "파일 또는 프로젝트로 디버그 / Todo 필터...",
    },
    "dt.sort_project": {"en": "Project", "ko": "프로젝트"},
    "dt.sort_size": {"en": "Size", "ko": "크기"},
    "dt.sort_status": {"en": "Status", "ko": "상태"},
    "dt.btn_sort": {"en": "Sort: {mode}", "ko": "정렬: {mode}"},
    "dt.btn_trash_debug": {"en": "Trash Selected Debug", "ko": "선택 디버그 삭제"},
    "dt.btn_trash_todo": {"en": "Trash Selected Todo", "ko": "선택 Todo 삭제"},
    "dt.confirm_debug": {
        "en": "Move debug file to trash?\n\nFile: {name}\n[dim]Moved to trash, recoverable[/]",
        "ko": "Debug file을 휴지통으로 이동?\n\nFile: {name}\n[dim]휴지통으로 이동되며 복구 가능합니다[/]",
    },
    "dt.confirm_todo": {
        "en": "Move todo file to trash?\n\nFile: {name}\n[dim]Moved to trash, recoverable[/]",
        "ko": "Todo file을 휴지통으로 이동?\n\nFile: {name}\n[dim]휴지통으로 이동되며 복구 가능합니다[/]",
    },
    "dt.empty_file": {
        "en": "(Empty file - no content)\nSafe to delete.",
        "ko": "(빈 파일 - 내용 없음)\n안전하게 삭제 가능합니다.",
    },
    "dt.none": {"en": "(none)", "ko": "(없음)"},
    "dt.btn_prune_debug": {"en": "Prune Empty ({count})", "ko": "빈 파일 정리 ({count})"},
    "dt.btn_prune_todo": {"en": "Prune Empty ({count})", "ko": "빈 파일 정리 ({count})"},
    "dt.confirm_prune_debug": {
        "en": "Delete all empty debug files?\n\n{count} empty files found.\n[dim]Moved to trash, recoverable[/]",
        "ko": "빈 디버그 파일을 모두 삭제하시겠습니까?\n\n{count}개의 빈 파일.\n[dim]휴지통으로 이동, 복구 가능[/]",
    },
    "dt.confirm_prune_todo": {
        "en": "Delete all empty todo files?\n\n{count} empty files found.\n[dim]Moved to trash, recoverable[/]",
        "ko": "빈 Todo 파일을 모두 삭제하시겠습니까?\n\n{count}개의 빈 파일.\n[dim]휴지통으로 이동, 복구 가능[/]",
    },
    "dt.prune_ok": {"en": "Pruned {ok} empty files ({fail} failed)", "ko": "빈 파일 {ok}개 정리 완료 ({fail}개 실패)"},
    "dt.btn_trash_orphaned_debug": {"en": "Trash Orphaned ({count})", "ko": "Orphaned 삭제 ({count})"},
    "dt.btn_trash_orphaned_todo": {"en": "Trash Orphaned ({count})", "ko": "Orphaned 삭제 ({count})"},
    "dt.confirm_trash_orphaned_debug": {
        "en": "Delete all orphaned debug files?\n\n{count} orphaned files.\n[dim]Moved to trash, recoverable[/]",
        "ko": "Orphaned 디버그 파일 모두 삭제?\n\n{count}개.\n[dim]휴지통으로 이동, 복구 가능[/]",
    },
    "dt.confirm_trash_orphaned_todo": {
        "en": "Delete all orphaned todo files?\n\n{count} orphaned files.\n[dim]Moved to trash, recoverable[/]",
        "ko": "Orphaned Todo 파일 모두 삭제?\n\n{count}개.\n[dim]휴지통으로 이동, 복구 가능[/]",
    },
    # ── Migrate ──
    "mig.info": {
        "en": (
            "[bold]Move / Copy[/] - Sessions to another project\n"
            "[dim]Select source project on the left, target on the right, pick sessions, then Move or Copy.\n"
            "Move keeps the session id and sends originals to trash (recovery snapshot). Copy keeps originals and gives copies new ids. Subagent/tool-result folders go along; memory is merged.[/]"
        ),
        "ko": (
            "[bold]이동 / 복사[/] - 세션을 다른 프로젝트로\n"
            "[dim]왼쪽에서 원본 프로젝트, 오른쪽에서 대상 폴더를 고르고 세션을 선택한 뒤 Move 또는 Copy를 누르세요.\n"
            "Move는 세션 id를 유지하고 원본을 휴지통으로 보냅니다(복구 스냅샷). Copy는 원본을 두고 새 id로 복제합니다. 서브에이전트·도구 결과 폴더도 함께 가고, 메모리는 합쳐집니다.[/]"
        ),
    },
    "mig.source_title": {
        "en": "[bold cyan]Source[/] [dim]- only projects with sessions[/]",
        "ko": "[bold cyan]Source[/] (원본) [dim]- 세션이 있는 프로젝트만 표시[/]",
    },
    "mig.target_title": {"en": "[bold green]Target[/] (destination)", "ko": "[bold green]Target[/] (대상)"},
    "mig.not_selected": {"en": "[dim]Not selected[/]", "ko": "[dim]선택되지 않음[/]"},
    "mig.select_source": {"en": "Select source project on the left", "ko": "왼쪽에서 소스 프로젝트를 선택하세요"},
    "mig.select_target": {"en": "Select target project on the right", "ko": "오른쪽에서 대상 프로젝트를 선택하세요"},
    "mig.same_error": {"en": "Source and target are the same", "ko": "소스와 대상이 같습니다"},
    "mig.no_sessions_selected": {"en": "No sessions selected", "ko": "선택된 세션이 없습니다"},
    "mig.confirm": {
        "en": "Move sessions?\n\nSource: {src}\nTarget: {tgt}\nMode: {mode}\n\n[dim]Same session ids. Originals go to trash (recoverable).[/]",
        "ko": "세션을 이동할까요?\n\n원본: {src}\n대상: {tgt}\n모드: {mode}\n\n[dim]세션 id는 그대로입니다. 원본은 휴지통으로 갑니다(복구 가능).[/]",
    },
    "mig.confirm_copy": {
        "en": "Copy sessions?\n\nSource: {src}\nTarget: {tgt}\n\n[dim]Copies get new session ids. Originals stay.[/]",
        "ko": "세션을 복사할까요?\n\n원본: {src}\n대상: {tgt}\n\n[dim]복사본은 새 세션 id를 받습니다. 원본은 그대로 둡니다.[/]",
    },
    "mig.complete": {"en": "Done!", "ko": "완료!"},
    "mig.failed": {"en": "Failed", "ko": "실패"},
    # ── Backups ──
    "bak.info": {
        "en": (
            "[bold]Backups[/] - Create, restore, and export/import backups\n"
            "[dim]Stored in ~/.asm/backups/. SPACE=multi-select, Export as .tar.gz for server migration.[/]"
        ),
        "ko": (
            "[bold]Backups[/] - 백업 생성, 복원, 내보내기/가져오기\n"
            "[dim]~/.asm/backups/ 에 저장됩니다. SPACE=다중 선택, 서버 이동 시 .tar.gz로 내보내기/가져오기 가능.[/]"
        ),
    },
    "bak.btn_config": {"en": "Config", "ko": "Config"},
    "bak.btn_full": {"en": "Full", "ko": "Full"},
    "bak.btn_settings": {"en": "Settings", "ko": "Settings"},
    "bak.btn_plugins": {"en": "Plugins", "ko": "Plugins"},
    "bak.btn_sessions": {"en": "Sessions", "ko": "Sessions"},
    "bak.btn_codex": {"en": "Backup Codex Sessions", "ko": "Codex 세션 백업"},
    "bak.btn_restore": {"en": "Restore", "ko": "복원"},
    "bak.btn_delete": {"en": "Delete", "ko": "삭제"},
    "bak.btn_export": {"en": "Export .tar.gz", "ko": "내보내기 .tar.gz"},
    "bak.btn_import": {"en": "Import .tar.gz", "ko": "가져오기 .tar.gz"},
    "bak.filter_placeholder": {
        "en": "Filter backups / recovery snapshots...",
        "ko": "백업 / 복구 스냅샷 필터...",
    },
    "bak.sort_newest": {"en": "Newest", "ko": "최신순"},
    "bak.sort_largest": {"en": "Largest", "ko": "크기순"},
    "bak.sort_type": {"en": "Type", "ko": "유형"},
    "bak.btn_sort": {"en": "Sort: {mode}", "ko": "정렬: {mode}"},
    "bak.recovery_title": {"en": "Recovery Snapshots", "ko": "복구 스냅샷"},
    "bak.recovery_info": {
        "en": "[dim]Created automatically before app-managed deletes. Restore to the original path or overwrite the current path.[/]",
        "ko": "[dim]앱에서 삭제하기 전에 자동 생성됩니다. 원래 경로로 복원하거나 현재 경로를 덮어쓸 수 있습니다.[/]",
    },
    "bak.recovery_none": {"en": "(no recovery snapshots)", "ko": "(복구 스냅샷 없음)"},
    "bak.recovery_status_ready": {"en": "Ready to restore", "ko": "복원 가능"},
    "bak.recovery_status_exists": {"en": "Original path exists", "ko": "원래 경로가 이미 존재"},
    "bak.btn_recovery_restore": {"en": "Restore Snapshot", "ko": "스냅샷 복원"},
    "bak.btn_recovery_overwrite": {"en": "Overwrite Restore", "ko": "덮어쓰기 복원"},
    "bak.btn_recovery_delete": {"en": "Delete Snapshot", "ko": "스냅샷 삭제"},
    "bak.confirm_recovery_restore": {
        "en": "Restore recovery snapshot '{name}'?\n\nOriginal path:\n{path}",
        "ko": "복구 스냅샷 '{name}'을(를) 복원하시겠습니까?\n\n원래 경로:\n{path}",
    },
    "bak.confirm_recovery_overwrite": {
        "en": "Restore '{name}' and overwrite the current path if it exists?\n\nCurrent data at the original path will be moved to trash first.",
        "ko": "'{name}'을(를) 복원하고 현재 경로가 있으면 덮어쓰시겠습니까?\n\n현재 경로의 데이터는 먼저 휴지통으로 이동됩니다.",
    },
    "bak.recovery_restored": {"en": "Recovered: {path}", "ko": "복구 완료: {path}"},
    "bak.recovery_restore_failed": {"en": "Recovery restore failed: {reason}", "ko": "복구 실패: {reason}"},
    "bak.confirm_recovery_delete": {
        "en": "Delete recovery snapshot '{name}'?",
        "ko": "복구 스냅샷 '{name}'을(를) 삭제하시겠습니까?",
    },
    "bak.recovery_deleted": {"en": "Deleted recovery snapshot: {name}", "ko": "복구 스냅샷 삭제: {name}"},
    "bak.recovery_delete_failed": {"en": "Failed to delete recovery snapshot", "ko": "복구 스냅샷 삭제 실패"},
    "bak.confirm_full": {
        "en": "Create a full backup of .claude directory?\nThis may take a moment.",
        "ko": ".claude 디렉토리 전체 백업을 생성하시겠습니까?\n시간이 다소 걸릴 수 있습니다.",
    },
    "bak.confirm_settings": {
        "en": "Create settings backup?\n(settings.json, settings.local.json, keybindings.json)",
        "ko": "설정 백업을 생성하시겠습니까?\n(settings.json, settings.local.json, keybindings.json)",
    },
    "bak.confirm_plugins": {
        "en": "Create plugins backup?\n(plugins/ and skills/ directories)\nThis may take a moment.",
        "ko": "플러그인 백업을 생성하시겠습니까?\n(plugins/, skills/ 디렉토리)\n시간이 다소 걸릴 수 있습니다.",
    },
    "bak.confirm_sessions": {
        "en": "Create sessions backup?\n(projects/ directory - all session data)\nThis may take a moment.",
        "ko": "세션 백업을 생성하시겠습니까?\n(projects/ 디렉토리 - 전체 세션 데이터)\n시간이 다소 걸릴 수 있습니다.",
    },
    "bak.confirm_codex": {
        "en": "Create Codex sessions backup?\n(~/.codex/sessions + index/config, excludes caches)\nThis may take a moment.",
        "ko": "Codex 세션 백업을 생성하시겠습니까?\n(~/.codex/sessions + 인덱스/설정, 캐시 제외)\n시간이 다소 걸릴 수 있습니다.",
    },
    "bak.confirm_restore": {
        "en": "Restore backup '{name}'?\nCurrent data will be backed up first.",
        "ko": "백업 '{name}'을(를) 복원하시겠습니까?\n현재 데이터가 먼저 백업됩니다.",
    },
    "bak.confirm_delete": {"en": "Delete backup '{name}'?", "ko": "백업 '{name}'을(를) 삭제하시겠습니까?"},
    "bak.confirm_bulk_delete": {
        "en": "Delete {count} selected backups?",
        "ko": "선택한 {count}개 백업을 삭제하시겠습니까?",
    },
    "bak.symlink_warning": {
        "en": "Restored with warnings:\n{count} symlink(s) have broken targets.\nThese plugins/skills may not work:\n{items}",
        "ko": "복원 완료 (경고):\n{count}개 symlink의 대상이 존재하지 않습니다.\n해당 플러그인/스킬이 동작하지 않을 수 있습니다:\n{items}",
    },
    # Common
    "common.trashed": {"en": "Trashed: {name}", "ko": "삭제됨: {name}"},
    "common.failed": {"en": "Failed", "ko": "실패"},
    "common.active": {"en": "[green]Active[/]", "ko": "[green]Active[/]"},
    "common.orphaned": {"en": "[yellow]Orphaned[/]", "ko": "[yellow]Orphaned[/]"},
    "common.trash_bulk_ok": {"en": "Deleted {ok} items ({fail} failed)", "ko": "{ok}개 삭제 완료 ({fail}개 실패)"},
    "common.no_items": {"en": "No items to delete", "ko": "삭제할 항목 없음"},
    "bak.config_created": {"en": "Config backup created: {path}", "ko": "Config 백업 생성: {path}"},
    "bak.settings_created": {"en": "Settings backup created: {path}", "ko": "Settings 백업 생성: {path}"},
    "bak.plugins_creating": {"en": "Creating plugins backup...", "ko": "Plugins 백업 생성 중..."},
    "bak.plugins_created": {"en": "Plugins backup created: {path}", "ko": "Plugins 백업 생성: {path}"},
    "bak.sessions_creating": {"en": "Creating sessions backup...", "ko": "Sessions 백업 생성 중..."},
    "bak.sessions_created": {"en": "Sessions backup created: {path}", "ko": "Sessions 백업 생성: {path}"},
    "bak.codex_creating": {"en": "Creating Codex backup...", "ko": "Codex 백업 생성 중..."},
    "bak.codex_created": {"en": "Codex backup created: {path}", "ko": "Codex 백업 생성: {path}"},
    "bak.backup_failed": {"en": "Failed to create backup", "ko": "백업 생성 실패"},
    "bak.full_creating": {"en": "Creating full backup...", "ko": "전체 백업 생성 중..."},
    "bak.full_created": {"en": "Full backup created: {path}", "ko": "Full 백업 생성: {path}"},
    "bak.restored": {"en": "Restored: {name}", "ko": "복원 완료: {name}"},
    "bak.restore_failed": {"en": "Restore failed", "ko": "복원 실패"},
    "bak.deleted": {"en": "Deleted: {name}", "ko": "삭제 완료: {name}"},
    "bak.delete_failed": {"en": "Delete failed", "ko": "삭제 실패"},
    "bak.bulk_deleted": {"en": "Deleted {ok} backups ({fail} failed)", "ko": "{ok}개 백업 삭제 ({fail}개 실패)"},
    "bak.exported": {"en": "Exported: {path}", "ko": "내보내기 완료: {path}"},
    "bak.export_failed": {"en": "Export failed", "ko": "내보내기 실패"},
    "bak.imported": {"en": "Imported: {name}", "ko": "가져오기 완료: {name}"},
    "bak.import_failed": {"en": "Import failed", "ko": "가져오기 실패"},
    "bak.no_source": {"en": "Nothing to backup (source not found)", "ko": "백업할 대상이 없습니다 (소스 없음)"},
}


def set_lang(lang: str) -> None:
    """Set the current language. Falls back to 'en' if unsupported."""
    global _current_lang
    _current_lang = lang if lang in ("en", "ko") else "en"


def get_lang() -> str:
    return _current_lang


def t(key: str, **kwargs) -> str:
    """Get translated string. Supports {placeholder} formatting."""
    entry = TRANSLATIONS.get(key)
    if not entry:
        raise KeyError(f"Unknown translation key: {key}")
    if _current_lang not in entry:
        raise KeyError(f"Missing {_current_lang} translation: {key}")
    return entry[_current_lang].format(**kwargs)


def init_lang(cli_lang: str | None = None) -> None:
    """Initialize language from CLI arg > env var > default (en)."""
    lang = cli_lang or os.environ.get("ASM_LANG", "en")
    set_lang(lang)
