# E2E — `selection` 도 `delivery_config` 와 같이 리댁트된다 (#1033)

**대상 PR**: 응답 3경로에 `public_binding_selection` 적용 + 표면 전수 가드
**왜 생겼나**: #1032 가 `selection` 을 `delivery_config` **오버라이드**로 만들면서 둘이
같은 키 공간이 됐는데, 리댁션은 한쪽에만 있었다.

> 📏 **지금 prod `selection` 세 행에 시크릿은 없다.** 이건 유출 수습이 아니라
> **#1032 가 방금 만든 초대장을 닫는 것**이다. 그래서 배포 후에도 *"뭔가 가려졌나"* 가
> 아니라 **"멀쩡한 값이 그대로 나오나"** 가 1순위 확인이다.

---

## 배포 전 — 실측 (완료)

- [x] **나가는 경로 전수** — `backend/api` + `backend/mcp` 를 훑어 `selection` 을 언급하는
      파일 **7개**를 세고, 그중 **내보내는 셋**을 갈랐다

      | 경로 | 성격 |
      |---|---|
      | `mcp/tools/bindings_tools.py` `_row_to_dict` | MCP 목록 |
      | `api/v1/products/_schemas.py` `ResourceBindingResponse` | REST **3개 라우트**가 공유 |
      | `api/v1/workspace_compliance.py` | 🚨 **GDPR Art.15/20 export** |

      나머지 넷은 면제 — 입력 전용(`bindings.py`) · 다른 의미(`workers.py` 의 stream
      selection) · 독스트링 둘. **면제마다 이유를 테스트에 적었다**
- [x] **범위 밖을 측정으로 갈랐다** — `stages/intake.py:209` 가 `selection` 을 인바운드
      페이로드에 통째로 싣지만, 재보니 `requests.payload`(DB)로 **영속화**되는 축이고
      응답 경로가 아니다. *"같은 값이 두 저장소로 복제된다"* 는 **다른 명제**라
      같은 PR 에 안 섞었다(이슈에 측정 결과만 남김)
- [x] **리댁터는 별칭이지 두 번째 구현이 아니다** — `public_binding_selection =
      public_delivery_config`. 같은 본문을 다시 쓰는 것이 `connector_redaction.py` 독스트링이
      적어 둔 바로 그 사고다(*"미러라서 한 번 어긋났고, 어긋난 결과가 응답에 라이브
      크리덴셜 노출이었다"*). **별칭은 어긋날 수가 없다**
- [x] **전선 절단 5건**, 전부 컴파일됐고(매번 **9개 실행**) 정확히 의도한 칸만 뒤집혔다

      | 절단 | 뒤집힌 것 |
      |---|---|
      | MCP 만 제거 | 2건 (MCP + 전체 키 집합) |
      | **REST 응답모델만 제거** | **1건만** |
      | **GDPR export 만 제거** | **1건만** |
      | 별칭을 자기 구현으로 | **3경로 전부** 4건 |
      | **새 파일이 `selection` 을 만짐** | **전수 가드가 잡는다** |

      가운데 둘이 핵심이다 — 세 직렬화기가 **서로 다른 모양**(dict 리터럴 · pydantic
      모델 · dict 리터럴)이라 **호출 지점별로 각각** 증명해야 한다
- [x] CI 다섯 스텝 전부: `ruff check` · `ruff format` · `lint-imports`(6 kept 0 broken) ·
      `mypy`(596 files) · `pytest` **6,461 passed / 49 skipped**

## 배포 후 — 걸 것

- [ ] **멀쩡한 값이 그대로 나오나부터** — `bindings_list` 가 `selection: {"chat_id": "8242700007"}`
      를 **그대로** 돌려주는지. 리댁터가 시크릿만 지우고 정상 키는 안 건드린다는 prod 증거다
- [ ] REST `GET /api/v1/products/{id}/bindings` 도 같은 값인지 — MCP 와 **다른 직렬화기**다
- [ ] 🚨 **GDPR export** `GET /api/v1/workspace/export` 의 `resource_bindings[].selection`
      확인. 제일 나쁜 경로였으니 제일 확실히 봐야 한다
- [ ] ⚠️ **양성 대조군 없이 "시크릿 안 보임"을 보고하지 마라** — 지금 prod `selection` 에는
      시크릿이 **애초에 없다.** 안 보이는 게 리댁터 덕인지 원래 없어서인지 구분이 안 된다.
      갈라내려면 바인딩 하나에 `webhook_secret` 을 **일부러 넣고**(⚠️ 원래 값 먼저 기록)
      세 경로가 전부 지우는지 본 뒤 **되돌려라**.
      *(못 하면 그 이유를 여기 적을 것 — 바인딩 쓰기는 권한 분류기에 막힐 수 있다)*

## 범위 밖 — 따로 봐야 하는 것

- [ ] `intake.py` 가 `selection` 을 `requests.payload` 로 복제하는 축 (#1033 코멘트)
- [ ] `SECRET_DELIVERY_KEYS` 가 **열거 목록**이라는 한계 — #1027 에서는 *"이름 목록 대신
      값 전량 리댁션"* 이 옳았다. 여기는 키를 남겨야 설정이 읽히므로 같은 수를 못 쓴다.
      **새 커넥터가 시크릿 키를 들고 오면 목록에 넣어야 한다**는 부채가 남는다
