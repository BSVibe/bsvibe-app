# 회고 선언 — 요청을 "답할 수 있는 순간"으로 옮긴다

> 2026-09-01. 인수인계 §Ⅳ.3(선언율 9.4%)의 처방을 **재측정**한 결과.

## 인수인계의 처방과 실측이 갈렸다

인수인계는 *"저장 배선은 끝났고 **생산 문제**다 — 프롬프트·루프 설계"* 라고 적었다.
맞지만 너무 넓다. 실제로 잰 모양:

| 잰 것 | 결과 |
|---|---|
| `_SYSTEM_PROMPT` 가 `knowledge` 를 언급하는가 | ❌ **한 글자도 없다** |
| 회고의 채널 | `declare_verification` 의 OPTIONAL 인자 **하나뿐** |
| 그 툴을 언제 부르라고 시키는가 | **"BEFORE any file_write"** — 배우기 전 |
| `record_knowledge` | 🚨 `_drive_loop.py:489` **주석에만** 존재. 코드에 없다 |
| 늦게 재선언하면 회고가 실리는가 | ✅ 실린다 (`if declared is not None:` 래치) |
| 실제 생산된 회고의 성격 | ✅ 진짜 사후 학습 (`seeds/retrospect/783ab917…`) |

⇒ 프롬프트가 서툰 것이 아니라 **요청이 존재하지 않았다.** 스키마 자신이
*"Only you, who did the work, can see the tacit knowledge"* 라고 쓰면서 **작업 전에**
묻고 있었다. 서브시스템이 아니라 링크 하나.

## 초안이 틀렸고, 프로브가 잡았다

첫 문장은 *"re-declaring keeps the contract you already made"* 였다. **거짓이다** —
`checks` 는 필수 인자이고 재선언은 계약을 **덮어쓴다**(실측):

```
1st contract: [{'command': 'uv run pytest tests/test_real.py'}]
2nd contract: [{'command': 'true'}]        ← 앞선 계약이 사라졌다
2nd knowledge: RememberableKnowledge(...)  ← 회고는 실렸다
```

그대로 나갔다면 **회고를 남기려던 에이전트가 자기 진짜 계약을 조용히 파괴했을 것이다.**
⇒ 규율 49 그대로: 판단(또는 문구)을 확정하기 전에 실험 하나를 더.

## 체크리스트

- [x] 브리핑이 회고를 **끝**에서 요청한다 (인자 이름 · 나르는 툴 · 시점 셋 다)
- [x] 브리핑이 **재선언은 계약을 덮어쓴다**고 경고하고 `checks` 반복을 시킨다
- [x] 그 경고가 실제 동작과 일치한다 — 동작 쪽도 테스트로 핀
- [x] 두 가드 모두 **전선을 끊어 빨강 실증** (문구 제거 / 재선언을 병합으로 변경)
- [x] `_ASK_SYSTEM_PROMPT` 에는 안 들어갔다 — prod `fae09a47` 판정 보존
- [x] 브리핑이 per-turn 예산(3,000자) 안에 있다
- [x] `record_knowledge` 유령 주석 제거
- [ ] prod 실측 — 배포 후 실제 런에서 회고가 선언되는지, 그리고 **계약이 살아있는지**

## prod 실측 방법 (배포 후)

```bash
# 코드 변경이 있는 런을 하나 돌린 뒤
```
MCP: `bsvibe_knowledge_list_recent(subdir="seeds/retrospect")` → `total` 이 늘었는가.
그리고 그 런의 계약이 stub 으로 덮이지 않았는지 딜리버리 리포트의 check 목록으로 확인.

⚠️ **`total` 만 세지 마라.** 늘어난 노트가 *사후 학습*인지 *의도 재진술*인지 본문을
읽어라 — 요청을 늘리면 노이즈도 같이 는다. 판정 축은 개수가 아니라 **diff 가 보여주지
않는 것을 말하는가**이다.
