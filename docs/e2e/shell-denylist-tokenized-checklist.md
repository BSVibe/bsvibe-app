# E2E — shell_exec denylist: 구조적(토큰화) 매칭

기능: `shell_exec` 데니리스트가 명령 전체 문자열의 raw substring이 아니라
`shlex` 토큰화된 argv(파이프/복합/치환 세그먼트별)로 판정한다. 정상 명령의
토큰이 우연히 위험 패턴을 substring으로 포함해도 통과하고, 실제로 위험한
바이너리가 invoke될 때만 거부한다. ("파싱 대신 패턴매칭" 오류 제거 — INV-7 잔여 큐)

## 배경 (라이브 실증된 오탐)

- `git add -A` / `git add .` → 구 데니리스트 `"dd "`에 차단됨 (`add `가 `dd `로 끝남).
  **git add는 verify/commit 코어 플로우 — 최악의 오탐.**
- `pytest -k async` → `"nc "`에 차단됨 (`async `가 `nc `로 끝남, 라이브 run `86bb4354`).
- `cargo add serde` → `"dd "`에 차단됨.

## 샌드박스 네트워크 posture (조사 결과)

`infrastructure/sandbox/docker_manager.py::_create`의 `docker run`은 `--network none`
(또는 다른 egress 격리)을 **주지 않는다** — `--memory`/`--memory-swap`와 `-v` 바인드
마운트만. 즉 샌드박스는 egress가 있으므로 이 데니리스트는 genuine(soft) egress+파괴
가드다. 단 **airtight가 아니며 그럴 의도도 없다**(`bash -c`, `eval`, alias, `c""url`로
우회 가능). **샌드박스가 진짜 경계**이고 이건 편의적 defense-in-depth (코드 주석에 명시).

## 체크리스트

- [x] 유닛: 정상 명령 12종 허용 — `git add -A`, `git add .`, `git add -A && git commit`,
      `pytest -k async`, `python -m asyncio`, `cargo add serde`, `echo padding`,
      `cat foo | grep`, `echo hi > /dev/null`, `rm foo.txt`, `ls -la`, `npm run build`
      (실제 `ToolRegistry._shell_exec` 구동, 스텁 샌드박스가 명령을 기록)
- [x] 유닛: 실제 위험 25종 거부 — `dd if=/dev/zero of=x`, `curl`, `nc -l`, `ncat -l`,
      `wget`, `ssh`, `scp`, `telnet`, `rm -rf /`, `rm -fr`, `rm -r build`, `rm /etc/passwd`,
      `sudo rm`, `mkfs.ext4 /dev/sda`, `chmod 777 /`, `kill -9 -1`, `shutdown`, `reboot`,
      `cat foo > /dev/sda`, `echo data | dd of=/dev/sda`(파이프 세그먼트), `; curl`,
      `|| wget`, `$(curl …)`, `` `nc …` ``(치환), 포크밤 (거부 전 샌드박스 미접근 단언)
- [x] 유닛: 파싱 불가(따옴표 불균형) → fail SAFE 거부
- [x] 유닛: 빈 명령 거부
- [x] 실제 레지스트리 경로 구동 (인자만 기록하는 스텁 아님 — 허용 시 샌드박스 `exec` 호출,
      거부 시 미호출로 가드 발동 시점 증명)
- [x] CI 게이트 전수 통과: pytest(+80% floor), sdk tests, mypy --strict, ruff check,
      ruff format --check, lint-imports
- [ ] 라이브(executor run): 코딩 에이전트가 `git add -A` 실행 시 거부 없이 커밋 진행
      (구 데니리스트에서 차단되던 것) — founder 확인
- [ ] 라이브: 에이전트가 `curl`/`nc` 시도 시 여전히 거부됨 — founder 확인
