<div align="center">

# agent-session-manager

**Claude Code와 Codex가 남기는 모든 것을, 터미널 대시보드 하나로.**
`~/.claude`와 `~/.codex`의 비용·세션을 한 화면에서 보고, Claude / Codex 필터로 정리합니다.

[![PyPI](https://img.shields.io/pypi/v/agent-session-manager?style=flat-square&color=blue)](https://pypi.org/project/agent-session-manager/)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Built with Textual](https://img.shields.io/badge/Built%20with-Textual-5A2CA0?style=flat-square)](https://github.com/Textualize/textual)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20Windows-orange?style=flat-square)](#)
[![Status](https://img.shields.io/badge/Status-Personal--use-lightgrey?style=flat-square)](#)

`asm` · **[English](README.md)**

</div>

---

> **⚠️ 먼저 알아두세요**
> 이 도구는 **Claude Code와 OpenAI Codex의 내부 데이터**(`~/.claude`, `~/.codex`)를 직접 읽고 수정합니다. 모든 삭제는 OS 휴지통으로 가고 로그에 남지만, 대량 작업 전에는 백업을 권장합니다. Anthropic·OpenAI와 무관한 개인 프로젝트입니다.

---

## 무엇인가요

**Claude Code**와 **Codex**를 쓰다 보면 `~/.claude`, `~/.codex`에 세션 기록·비용 로그·디버그·task 목록·스냅샷이 쌓입니다. 몇 주만 지나면 어떤 프로젝트가 토큰을 가장 많이 썼는지, 어떤 파일이 정리해도 되는지 알기 어렵죠.

**agent-session-manager**(`asm`)는 이 모든 걸 터미널 대시보드 하나에 모읍니다. 두 에이전트를 함께 보여주고, 키 하나(`s`)로 **All / Claude / Codex** 필터를 전환해 비용을 비교하거나 한쪽만 들여다볼 수 있습니다.

### 💡 왜 만들었나요

- **문제:** 서로 다른 두 코딩 에이전트의 비용·세션 데이터가 각각 불투명한 디렉토리에 흩어져, 함께 보는 화면이 없음
- **해결:** 둘 다 읽어 정확히 가격을 매기고, 휴지통·복구 스냅샷으로 안전하게 정리하는 TUI 하나

---

## 데모

통합 대시보드(두 에이전트 합산 비용, 소스 태그된 행) → 같은 화면을 **Claude** / **Codex**로 필터링:

<img src="docs/demo.gif" alt="asm 데모" width="820"/>

---

## ✨ 주요 기능

### 통합 대시보드
- **두 에이전트 합산 비용** + 소스 필터 — `All / Claude / Codex` **클릭** 또는 `s` 키
- 모델별 토큰·비용 (Opus / Sonnet / Haiku / GPT-5.x), 각 행을 소스 태그로 구분
- 일별 / 주별 / 월별 사용량 표(한 번 스캔으로 전부), 프로젝트 비용 Top 10 차트
- **LiteLLM에서 실시간으로 받아오는** 정확한 단가 (15분 캐시, 오프라인 시 내장 테이블 폴백) — 신모델도 릴리스를 기다리지 않고 바로 정확하게 계산되고, 어떤 단가 출처를 썼는지 대시보드에 항상 표시
- **영구 사용량 장부** (`~/.asm/usage.db`): 세션은 한 번만 파싱하고 변경된 것만 재스캔, 비용은 **스캔 시점의 요율로 동결**(과거 세션을 현재 가격으로 재계산하지 않음), Claude Code가 오래된 세션을 지워도 **비용 이력은 보존** — Codex도 최근 N개 캡 없이 전체 이력 집계

### 통합 세션 (Claude + Codex)
- 한 트리에 Claude 프로젝트와 Codex 작업 디렉토리를 함께, 각 세션을 **C** / **X**로 구분
- 프로젝트를 펼쳐 양쪽 에이전트 대화를 미리보기, 개별 세션 삭제
- **instruction 편집:** 프로젝트별 `CLAUDE.md`·`CLAUDE.local.md`·`AGENTS.md`·`AGENTS.local.md`를 내장 편집기로 보기/편집/생성(Ctrl+S 저장)
- **Codex 세션 이동:** 다른 작업 디렉토리로 이동(세션의 `cwd` 재작성 — `codex resume --cd`가 세션을 연결하는 방식)
- **고아 정리:** 매칭 프로젝트 없는 Claude 세션·파일히스토리·디버그·task 일괄 정리
- **중복 세션:** 여러 프로젝트에 복사된 동일 세션을 찾아 개별 복사본 삭제
- **빈 세션:** 제목/메타만 있고 대화가 없는(resume 불가) stub 세션 정리
- **마이그레이션:** Claude 프로젝트 간 세션 복사(원본 유지), 경로 자동 갱신

### 아티팩트
- Claude Code **Artifact 도구**로 발행한 페이지를 세션 기록에서 찾아 최신순으로 나열
- 터미널을 떠나지 않고 브라우저로 열기(`Enter`/`o`)·URL 복사(`c`) — `asm artifacts`(`--json`)로도 조회 가능

### Claude ↔ Codex (`F8` 탭)
- **MCP 서버 양방향:** `~/.codex/config.toml`과 `~/.claude.json`을 비교해 한쪽에만 있는 항목을 찾고, 각 에이전트의 `mcp add` CLI로 추가한다(파일 포맷·주석 보존). 인증 헤더와 env는 그대로 옮기며, `codex mcp add`가 HTTP 헤더를 표현하지 못해 그 부분만 `[mcp_servers.<name>.http_headers]`로 덧붙인다
- **세션 양방향 복사:** Claude 대화록 ↔ Codex rollout 스레드 변환 — 실제 CLI에서 resume해 맥락이 복원되는 것까지 확인했다. Claude→Codex는 공식 importer와 같은 `content_sha256` 원장에 기록해 Codex가 이미 가져온 세션을 중복 생성하지 않고, `/import`와 달리 원하는 세션만 고를 수 있다(30일·50개 제한 없음)
- 가져온 사본은 **토큰 사용량이 0**으로 기록돼, 옮긴 세션이 두 번 과금 집계되지 않는다
- 쓰기 전에 항상 계획을 먼저 보여준다: 가져올 수 있음 / 건너뜀 / 미지원 목록을 미리 확인하고, 첫 쓰기 전에 백업 스냅샷을 만든다
- **기본 선택은 없다.** 계획은 최신 200개까지만 잡고(2.5만 개 rollout을 전부 해시하면 느리다), 제외된 오래된 개수를 함께 표시해 잘린 목록이 "이게 전부"로 읽히지 않게 한다

### 기본이 안전
- 모든 삭제는 **OS 휴지통**으로, 감사 로그에 기록
- 삭제 전 **복구 스냅샷** 생성 (Claude·Codex 모두), 용량/개수 상한으로 무한 누적 방지
- 백업+**복원**: Claude(config / settings / plugins / sessions / 전체), Codex(세션, 대용량 캐시 제외). 복원은 롤백 안전(원본을 옆으로 옮겨두고 복사 실패 시 되돌림), 자격증명 포함 백업은 소유자 전용(0600)으로 저장

---

## 작동 방식

```
   ~/.claude  ┐
              ├──►  asm  ──►  대시보드 하나  ──►  필터: All / Claude / Codex
   ~/.codex   ┘              (비용 · 세션 · 정리 · 백업)
```

`asm`은 두 데이터 디렉토리를 직접 읽습니다 — 데몬·설정 없음. Claude는 프로젝트별, Codex는 작업 디렉토리별로 묶고, 비용은 각 세션에 기록된 토큰 사용량으로 계산합니다.

| 경로 | 내용 |
|:---|:---|
| `~/.claude.json` · `~/.claude/projects/` | Claude 프로젝트·비용·세션 JSONL |
| `~/.claude/file-history/` · `debug/` · `tasks/` | 스냅샷·디버그 로그·세션별 task 목록 |
| `~/.codex/sessions/` | Codex rollout 세션 파일 |
| `~/.asm/backups/` · `trash-log.jsonl` · `usage.db` | 백업(기존 `~/.cc-tui`에서 자동 이전)·삭제 감사 로그·사용량 장부 |

---

## 🛠️ 기술 스택

- **TUI:** [Textual](https://github.com/Textualize/textual) + [Rich](https://github.com/Textualize/rich)
- **안전장치:** [send2trash](https://github.com/arsenetar/send2trash) (`rm`이 아닌 OS 휴지통)
- **세션:** 내장 JSONL 파서 — 무거운 의존성 없음; 필요 시 [claude-agent-sdk](https://pypi.org/project/claude-agent-sdk/)를 `pip install 'agent-session-manager[sdk]'`로 추가
- **Python:** 3.11+

---

## 🚀 시작하기

### 설치 (권장)

```bash
# uv
uv tool install agent-session-manager

# pip
pip install agent-session-manager
```

둘 다 단일 `asm` 명령을 설치합니다. 최초 실행 시 기존 `~/.cc-tui` 데이터가 `~/.asm`로 자동 이전됩니다.

<details>
<summary><strong>소스에서 실행</strong></summary>

```bash
git clone https://github.com/Changroro/agent-session-manager.git
cd agent-session-manager
uv sync && uv run asm
```

</details>

### 사용법

```bash
asm                       # 실행 — Claude + Codex 함께 표시
asm --source codex        # 대시보드를 Codex 필터로 시작
asm --path /your/project  # 특정 Claude 프로젝트만
asm --lang ko             # 한국어 UI  (또는 ASM_LANG=ko)
asm --no-update-check     # 시작 시 업데이트 확인 건너뛰기
```

두 소스는 항상 함께 다룹니다. `--source`는 대시보드 초기 필터만 정하며, 실행 중 `s`로 언제든 바꿉니다.

### Headless CLI

모든 기능을 서브커맨드로도 쓸 수 있습니다 — 스크립트나 AI 에이전트에서 유용합니다. 터미널에서는 rich 테이블, 파이프로 받으면 plain text, `--json`이면 기계가 읽는 JSON으로 출력됩니다.

```bash
# 조회 (읽기 전용)
asm cost --period weekly          # 모델별·기간별 비용/토큰 통계
asm projects                      # 전체 프로젝트 (Claude + Codex)
asm sessions --search "방화벽"     # 세션 제목 검색
asm preview <session-id>          # 대화 내용 출력
asm resume <session-id>           # 세션의 프로젝트로 이동해 바로 resume (Claude/Codex)
asm artifacts                     # Artifact 도구로 발행한 페이지 목록
asm import list --to claude       # Codex -> Claude 로 옮길 수 있는 세션
asm backup list / asm recovery list

# 변경 — 실행 전 항상 확인을 묻습니다(--yes로 생략). TUI와 동일하게
# 모든 삭제는 OS 휴지통 + 복구 스냅샷을 경유합니다.
asm clean empty --dry-run         # empty | orphaned | debug | todos
asm trash <session-id>
asm backup create --type full     # config|full|settings|plugins|sessions|codex
asm backup restore <path> / asm recovery restore <id>
asm migrate /old/project /new/project
asm import session <session-id>   # 세션 1개 이동(방향은 id로 자동 판별)
asm import mcp --to codex         # MCP 서버 이동
```

Codex App·CLI·동반 에이전트는 세션 저장소 하나를 공유한다 —
[Codex 데이터 홈](docs/codex-data-home.ko.md) 참고.

`asm import list`는 **마지막 활동 시각** 기준으로 정렬한다 — Codex rollout 파일명의
시각은 세션을 *시작한* 때라 순서가 다르게 보인다. 그래서 활동 시각을 함께 출력해
정렬 근거가 눈에 보이게 했다. 나머지(200개 스캔 창, 사본에 부여되는 새 id, 세션의
`cwd`로 목적지 폴더를 정하는 방식)는 `asm import --help`에 적혀 있다.

### 키보드

| 키 | 동작 |
|:---:|:---|
| `F1`~`F8` | 탭 전환 |
| `s` / 클릭 | 대시보드 소스 필터 (All / Claude / Codex) |
| `Tab` / `Shift+Tab` · `1` `2` `3` | 대시보드 기간 (Daily / Weekly / Monthly) |
| `d` / `D` | 선택 삭제 / 전체 고아 삭제 |
| `Space` · `Enter` | 선택 토글 · 대화 미리보기 |
| `r` · `q` | 전체 새로고침 · 종료 |

### 업데이트

PyPI에 새 버전이 있으면 `asm` 실행 시 `y/N` 업그레이드 프롬프트가 뜹니다(`uv tool` 또는 `pip`). 비대화형 셸·오프라인에서는 건너뜁니다.

---

## 🗺️ 로드맵

- [ ] 데이터 개요에 소스별 디스크 사용량 표시
- [ ] CI에 ruff + mypy

---

## ⚠️ 상태 & 범위

- **개인용 / 프리릴리스**, 활발히 개발 중.
- Claude Code·Codex 내부 데이터를 직접 다룹니다 — **대량 삭제 전 백업**하세요.
- 삭제는 OS 휴지통 + 복구 스냅샷으로 처리, 제자리 `rm` 없음.
- 무보증. Anthropic·OpenAI와 무관.

---

## 📄 라이선스

[MIT](LICENSE)

<div align="center">
<br/>
Made with <b>Claude Code</b> · and now <b>Codex</b> too
</div>
