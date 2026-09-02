# E2E — `bsvibe refresh` 가 저장된 refresh token 을 실제로 교환한다

#871 이 만료 세션을 두 상태로 갈랐다. 그중 `EXPIRED_REFRESHABLE` 은 *"갱신할 수
있다"* 고 말하면서 **아무것도 그걸 교환하지 않아서**, 안내가 나머지 하나와 똑같았다
(`bsvibe login`). 이걸 닫는다.

## 서버가 강제하는 것 (코드에서 읽은 것)

```python
# backend/api/oauth.py — /api/oauth/token
client_id: Annotated[str, Form()],           # ← 선택이 아니라 필수

# backend/identity/oauth_service.py — rotate_refresh_token
if parent.client_id != client_id:
    return RefreshRotateOutcome.INVALID, None
```

그런데 로그인은 **익명 DCR** 로 매 로그인마다 새 `client_id` 를 받고
(`login.py`: *"we never want a static client_id"*), 그걸 **저장하지 않았다.**

⇒ 갱신 가능성은 `refresh_token` 유무만으로 정해지지 않는다. `client_id` 가 없는
자격증명은 **그랜트를 만들 수조차 없다.** 그래서:

* 로그인이 `client_id` 를 같이 저장한다 (세 플로우 전부 — 루프백·수동·디바이스)
* 상태 3 의 안내가 **갈린다**. `client_id` 가 있을 때만 `bsvibe refresh` 를 권한다.
  없으면 그건 읽는 사람에게 **실패할 명령을 시키는 것**이고, #871 이 지우려던
  바로 그 종류의 거짓말이다

⚠️ 실패한 refresh 는 **파일을 건드리지 않는다.** 덮어쓰면 복구 가능한 상태가
복구 불가능해진다. 서버는 single-use 로 회전하므로 성공 시 refresh token 도 새것이다.

## 체크리스트

- [x] 유닛 8개: `uv run pytest tests/executors/worker/test_refresh_session.py -q`
- [x] **대조군 실증** — `client_id` 검사를 `True` 로 바꿔 *언제나* refresh 를 권하게
      하면 `test_status_does_not_offer_refresh_to_a_credential_that_predates_it`
      이 죽는가 (없으면 낡은 자격증명을 든 사람에게 실패할 명령을 시킨다)
- [x] 회귀: `uv run pytest tests/executors/worker/ -q` → 360 passed
      (#871 의 `test_status_session.py` 포함)
- [x] **라이브 — 형님의 실제 자격증명으로** (네트워크 호출 전에 멈추므로 무해):
      * `bsvibe status` → rc=3, *"predate `bsvibe refresh` … Run `bsvibe login` once"*
      * `bsvibe refresh` → rc=1, 이유를 그대로 말함
      * 자격증명 파일 키가 `['access_token','expires_at','issuer','refresh_token']`
        — back-compat 테스트가 겨누는 실제 모양
- [x] 전체 게이트 5종 — pytest **6586 passed** / 43 skipped (431s) · ruff · ruff format 1424 files · lint-imports 5 kept 0 broken · mypy 579 clean

## 다음 로그인 이후에 확인할 것 (아직 못 함)

`client_id` 는 **다음 `bsvibe login` 부터** 저장된다. 그래서 진짜 교환은 아직
라이브로 못 봤다 — 유닛(MockTransport)으로만 고정돼 있다.

- [ ] `bsvibe login` 한 번 → 자격증명 파일에 `client_id` 키가 생기는가
- [ ] 만료 후 `bsvibe refresh` → rc=0, `access_token`·`refresh_token` 이 **둘 다** 바뀌는가
- [ ] 같은 refresh token 으로 두 번 → 두 번째는 거부되는가 (single-use 회전)
