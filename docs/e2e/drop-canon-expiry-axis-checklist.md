# canon 제안/액션 만료 축 삭제 — 검증 체크리스트

> 2026-08-30. 감사 **5-9**. 그 문서의 처방은 *"배선하라"* 였는데 **재측정이 전제를
> 무너뜨렸다.** 그리고 이 PR 을 만드는 동안 **내 측정이 두 번 틀렸고 둘 다 장치가
> 잡았다** — 그 두 건이 이 문서의 값이다.

## 왜 배선이 아니라 삭제인가

| 측정 (prod, 2026-08-30) | |
|---|---|
| canon action/proposal 파일 | **1,167** |
| 상태 분포 | `applied` 1,100 · `accepted` 67 |
| `expire_stale` 의 대상(`draft`/`pending_approval`/`pending`) | **0건** |
| `expires_at` 으로 무언가를 막는 코드 | `expire_stale` **자신 외 0** |

배선하면 **0건을 쓸어담는 다섯 번째 폴링 워커**가 생긴다. 이 저장소는 강제된 적
없는 축을 두 번 다 삭제했다(관할권 축 · `region` #845).

## ⚠️ 내가 틀린 것 ① — "안 보인다"가 거짓이었다

처음 보고: *"canon `expires_at` 은 API/MCP 에 안 나간다 → **0**"*.
**틀렸다.** `decisions` 를 **파일명만 보고** "다른 축"으로 제외했는데, 실제로는
`_proposal_to_dict(proposal: models.ProposalEntry)` 가 그 만료일을 MCP
`bsvibe_decisions_list`/`show` 와 REST `GET /api/v1/decisions` 로 내보낸다.

형님이 그 잘못된 근거로 승인하셨기에 **전제가 바뀐 사실을 알리고 다시 결정**을
받았다. 결과는 같았지만(전체 삭제), 근거가 달라졌다 — *아무것도 만료시키지 않는
만료일을 **보여주는** 것은, 안 보이는 죽은 필드보다 나쁜 거짓말이다.*

⇒ **필터를 걸어 "0" 을 얻었으면 그 필터가 무엇을 버렸는지 먼저 열어봐라.**
`decisions/_schemas.py` 라는 파일명이 `ProposalEntry` 를 담고 있었다.

## ⚠️ 내가 틀린 것 ② — 마커 사이를 자르면 사이에 있는 것도 간다

`ExpireResult` 를 *"그 앞 `@dataclass` 부터 다음 `@dataclass` 까지"* 로 잘라냈다.
그 사이에 **무관한 Decision 상수 둘**(`DECISION_STATUSES` · `DECAY_PROFILES`)이
살고 있었고 함께 사라졌다.

**전체 스위트가 잡았다** — `module 'models' has no attribute 'DECAY_PROFILES'`.
canon 스위트만 돌렸으면 통과했을 자리다(그 상수를 쓰는 테스트는 다른 파일에 있었다).

⇒ **구조 단위(클래스/함수)가 아니라 텍스트 마커로 자르면 범위는 우연에 맡겨진다.**

## 지운 것과 남긴 것 — 철자가 같다고 같은 축이 아니다

| 필드 | 강제되나 | 표면 노출 | 처분 |
|---|---|---|---|
| `ActionEntry.expires_at` | ❌ | ❌ | **삭제** |
| `ProposalEntry.expires_at` | ❌ | ✅ MCP + REST | **삭제 (BREAKING)** |
| `DecisionEntry.expires_at` | ✅ `decisions.py:54` | — | **유지** |
| `PolicyEntry.expires_at` | ✅ `policies.py:175` | — | **유지** |

함께 지운 것: `expire_stale` · `ExpireResult` · `_NON_TERMINAL_ACTION_STATUSES` ·
`_DEFAULT_PROPOSAL_TTL` · `_DEFAULT_EXPIRY` · `create_action_draft(expires_in=...)`.

## 체크리스트

- [x] 만료 축 식별자가 트리 어디에도 없다 (AST 스캔)
- [x] `ProposalEntry` · `ActionEntry` 에 `expires_at` 필드가 없다
- [x] **MCP 와 REST 양쪽**에서 제안 만료가 사라졌다 — 한쪽만 지우면 두 표면이
      갈라진다 ([[mirrored-surface-drifts-in-the-direction-of-least-testing]])
- [x] **음성 대조군 4층 — 각 층이 자기 테스트만 떨어뜨린다.** 그중 **둘은
      "지키는" 대조군**이다:
      | 변이 | 떨어지는 테스트 |
      |---|---|
      | `ProposalEntry` 만료 부활 | 부재 가드 |
      | MCP 응답에 만료 부활 | 표면 가드 |
      | **`DecisionEntry` 만료를 잘못 지움** | **보존 대조군** |
      | **store 의 policy 만료를 잘못 지움** | **보존 대조군** |
- [x] 양성 대조군: 스캐너가 살아 있는 심볼을 실제로 찾는다
- [x] 양성 대조군: Decision·Policy 테스트 23건이 그대로 통과 (강제되는 축 무손상)
- [x] 전체 게이트 (fresh PG · pytest+cov80 · ruff · format · mypy · lint-imports)

## ⚠️ 게이트가 만든 실시간 사례 — 산문이 남의 가드를 물었다

첫 게이트에서 **무관한 테스트**가 떨어졌다:
`test_the_unenforced_jurisdiction_axis_is_gone::test_no_source_still_names_the_axis`.

원인은 **이 파일의 docstring**이었다. 선례를 인용하며 지워진 관할권 축의 이름을
적었는데, 그 가드는 **줄 텍스트를 스캔**하므로 산문도 후보가 된다.

내가 오늘 자산화한 [[absence-guard-listing-spellings-proves-only-imagination]] 의
*"산문이 자기 가드를 문다"* 를 **내가 그대로 재현했다** — 이번엔 남의 가드를.

정석은 그 가드를 AST 로 바꾸는 것이지만, 세션 말미에 무관한 보안 인접 가드를
흔드는 대신 인용에서 **리터럴 토큰만 뺐다**. 그 가드의 계약(*"어떤 소스도 이 축을
명명하지 않는다"*)을 존중하는 쪽이다. 별건으로 남는다.

## 마이그레이션 없음 — 기존 노트는 그대로 둔다

vault 의 1,167개 파일은 frontmatter 에 `expires_at` 키를 계속 갖는다. 읽는 쪽이
그 키를 무시하므로 무해하고, 되돌릴 수 없는 일괄 재작성보다 낫다.
