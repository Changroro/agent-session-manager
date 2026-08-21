# Codex 데이터 홈

Codex App·Codex CLI·동반 에이전트는 **`CODEX_HOME` 하나를 공유**한다. 기본값은
`~/.codex`이며 asm도 이 홈 하나만 읽는다. 형제 `~/.codex-*` 디렉터리를 자동으로
찾거나 합산하지 않는다.

계정 선택은 Codex 앱이나 설정된 provider/proxy가 요청을 보낼 때 처리하는 문제다.
계정마다 프로젝트·세션 저장소가 따로 생기지 않는다. 프로젝트, rollout, 사용량,
task 이력은 공유 홈에 남는다.

## 활성 홈 확인

```console
$ asm import home
Codex home: /home/you/.codex  25190 sessions
```

표준 환경변수 `CODEX_HOME`으로 한 프로세스에서 다른 홈을 지정할 수 있다. 용도는
여러 활성 홈을 합산하는 것이 아니라 **레거시·보관 데이터 조회/가져오기**다.

```bash
CODEX_HOME="$HOME/.codex-old" asm import list --to claude
CODEX_HOME="$HOME/.codex-old" asm import session <id> --dry-run
```

이 실행의 대시보드·비용·삭제·백업·복구 범위는 지정한 홈 하나로 격리된다.

## 과거 분리 홈 디렉터리

`~/.codex-hermes` 같은 디렉터리가 예전 런처 구조의 흔적으로 남아 있을 수 있다.
디렉터리가 존재한다고 활성 홈인 것은 아니다. 이름으로 추측하지 말고 실행 중인
app-server의 `CODEX_HOME`이나 해당 프로세스의 `asm import home`으로 확인한다.

예전에 ledger가 레거시 홈에서 읽어 둔 사용량 행은 asm의 "대화 파일을 지워도 이력
보존" 규약에 따라 남긴다. 일반 대시보드 조회는 활성 홈 경로로 필터링하므로 현재
합계에는 포함하지 않고, 포렌식용 과거 기록만 보존한다.
