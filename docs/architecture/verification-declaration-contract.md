# 검증 선언 계약 (Verification Declaration Contract)

## 개요

에이전트가 `declare_verification`을 호출해 검증 기준을 선언하면,
`VerificationService.assemble_contract`가 그 선언을 지식 검색 결과와 합쳐
**검증 계약(VerificationContract)**을 조립한다. 이 계약이 이후 두 게이트의
판정 기준이 된다.

## 에이전트 선언 게이트 — verify-first

`file_write` / `file_edit` 도구는 `declare_verification`이 먼저 호출되지
않으면 거부된다. 이것이 verify-first 게이트다 (`work_tools`의 도구 수준 강제).

## 선언이 없으면 사람에게 묻는다

`assemble_contract` (`verification_service.py:438`) 의 핵심 경로:

```python
# verification_service.py:454, 473
checks: list[VerificationCheck] = list(declared.checks) if declared is not None else []
if not checks:
    return None
```

에이전트가 `declare_verification`으로 아무것도 선언하지 않았으면
`assemble_contract`는 `None`을 반환한다. 호출자는 `no_verification_declared`
Decision을 발화해 파운더에게 묻는다.

검색 지식이 있어도 에이전트가 `declare_verification`으로 아무것도 선언하지
않았으면 계약은 성립하지 않는다. 지식 검색 결과는 선언이 만든 계약에 *얹히는*
것이지, 계약 자체를 만들지는 않는다 (`verification_service.py:467` 주석 참고):

> A DECLARATION IS REQUIRED. Retrieved knowledge may ride a contract the agent's
> own declaration made usable … but it must never be the thing that makes a
> contract EXIST — otherwise a run that declared nothing is graded against
> somebody else's criteria instead of routing to the caller's
> `no_verification_declared` Decision and asking.

`declared.checks`는 종류를 가리지 않는다 — `kind="command"`든 `kind="judge"`든
하나라도 선언하면 `checks`는 비지 않으므로 계약이 성립하고
`no_verification_declared`는 발화하지 않는다.

## 게이트 둘의 분리

두 게이트는 별개다.

**게이트 1 — 에이전트 선언 command 게이트**
(`_run_command_checks`, `verification_service.py:888`)

에이전트가 선언한 `kind="command"` 검사를 **에이전트가 작업한 샌드박스/워크트리**
에서 실행한다. 프로젝트 venv(`box.workspace_mount/.venv`)를 사용한다. 이 결과는
Delivery Report의 증거 표면에 기록되지만, 파생 게이트(아래)가 실행될 경우
권위(gate) 가 아니라 참고(advisory)로만 쓰인다.

**게이트 2 — 레포 파생 게이트**
(`_run_derived_gate`, `verification_service.py:967`)

독립 LLM deriver가 레포의 manifest(pyproject.toml, package.json 등)를 읽어
해당 레포에 맞는 검증 명령을 도출하고, 이를 **레포가 선언한 툴체인으로 뜨는
일회용 검증 환경**에서 실행한다. 이 게이트의 exit code가 권위 판정이다.

## 판정 절

모든 게이트를 통과하면 런은 `review_ready`로 이동한다. Safe Mode
워크스페이스에서는 파운더가 승인해야 PR이 열리고, 그 PR이 CI를 통과해야
자동 머지된다. **검증 통과가 곧 머지를 의미하지 않는다** — 파운더 승인 게이트가
판정 절 안에 있다.
