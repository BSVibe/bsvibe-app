# `write_seed` 가 `data["frontmatter"]` 를 반영한다 — 검증 체크리스트

> 2026-08-29. #847 에서 `source_ref` 를 지우며 **같은 처지의 형제**가 하나 더
> 드러났다. `source_ref` 는 아무도 못 읽는 값이라 지웠고, `frontmatter` 는
> **담긴 내용이 실제로 값어치가 있어서** 반영하기로 했다 (형님 판단).

## 무엇이 버려지고 있었나

`write_seed` 는 `data` 에서 `title` · `tags` · `content` 만 읽었다. 그래서
플러그인 4종이 채우던 `frontmatter` 가 노트에 도달한 적이 없다:

| 플러그인 | 버려지던 것 |
|---|---|
| notion | `notion_page_id` · `url` · 원본 `properties` 전체 |
| claude · gpt | `conversation_uuid` · `created_at` · `updated_at` · `message_count` |

`render_frontmatter_only` 의 docstring 은 스스로를 *"handy for **write_seed
metadata**"* 라고 적어 뒀다 — **의도는 처음부터 이 자리였고 배선만 없었다.**

## 이 값이 어디에 쓰이나 (왜 헛수고가 아닌가)

seed 파일을 **개념으로 컴파일하는 것은 아직 없다** — `compile_batch` 는 호출자가
만든 메모리 상의 items 를 받고, `seeds/` 를 훑는 워커는 존재하지 않는다.
그래도 seed 파일은 `file_index_reader` 가 `"seeds"` 카테고리로 **색인**하고 MCP
`read_notes` 로 읽힌다 — 즉 **검색 컨텍스트로는 실제로 들어간다.** 출처가
frontmatter 에 남으면 그 컨텍스트에 함께 실린다.

⚠️ 별건으로 남는 것: **플러그인 seed → 개념 배선이 아직 없다.** 그 배선을 정하는
것이 재임포트 dedup 을 고르는 것보다 앞이다 (dedup 의 정답이 배선에 따라 갈린다).

## 우선순위 — 센 것부터

1. **시스템 필드** `type` · `source` · `captured_at`
2. **최상위** `title` · `tags`
3. 호출자의 `frontmatter` 매핑

claude 의 매핑이 `source: claude.ai` 를 담는데 그 seed 는 `seeds/claude/` 에 산다.
**자기 경로와 어긋난 말을 하는 노트는 그 정보를 생략한 노트보다 나쁘다.**

## 안전 — 두 층

**① 매핑이 아닌 `frontmatter`** 는 쓰기를 실패시키지 않고 건너뛰며
`seed_frontmatter_not_a_mapping` 로 남긴다. seed 쓰기가 예외를 내면 플러그인이
그 항목을 **통째로** 건너뛰기 때문에, 필드 하나를 살리려고 임포트를 잃는 것은
잘못된 거래다.

**② 직렬화 안 되는 값** — 이건 처음에 못 봤고, 실제 위험이 내 예상과 달랐다.

`build_frontmatter` 는 `yaml.dump` 를 쓴다. 임의 객체에 **예외를 내지 않고**
`!!python/object:` 태그를 써 넣는다. 그런데 이 저장소에서 노트 frontmatter 를
읽는 코드는 **전부 `yaml.safe_load`** 이고(`markdown_utils` · `_mutation` ·
`_tombstone` · `_entity_stub` · `ontology`), safe_load 는 그 태그에서 터진다.

⇒ 쓰기는 **성공**하고 그 뒤로 아무도 그 노트를 못 읽는다. 크래시보다 나쁘다 —
조용하고, 되돌아오지 않는다. `yaml.safe_dump` 가 받아들이는 값만 통과시키고
나머지는 키 단위로 버린다(`seed_frontmatter_value_not_serializable`).

⚠️ **남는 것**: 최상위 `title`/`tags` 에는 같은 필터가 없다 — 기존 동작이고 이
PR 의 범위가 아니다. 근본책은 `build_frontmatter` 를 `safe_dump` 로 바꾸는 것인데
garden 노트 전체에 영향이 있어 별건이다.

## 체크리스트

- [x] 호출자 매핑의 키가 노트 frontmatter 에 실린다
- [x] 시스템 필드가 이긴다 — `source` 가 `claude.ai` 로 덮이지 않고 경로와 일치
- [x] 최상위 `title`/`tags` 가 매핑보다 세다
- [x] `frontmatter:` 라는 키 자체는 안 나온다 — 중첩이 아니라 **병합**
- [x] 매핑이 아닌 값이 와도 쓰기가 안 깨진다 (경고만 남는다)
- [x] **직렬화 안 되는 값이 노트를 못 읽게 만들지 않는다** — 나쁜 키만 버리고
      나머지 출처는 살린다
- [x] 양성 대조군: 본문은 여전히 `data["content"]` 그대로
- [x] 양성 대조군: `frontmatter` 를 안 주는 호출자(대다수)의 결과가 무변동
- [x] **음성 대조군 — 알리바이 0건.** 변이 가능한 층은 **넷**이다:

      | 제거한 것 | 떨어진 테스트 |
      |---|---|
      | 병합 자체 (`metadata[key] = value`) | **5건** (전부) |
      | 시스템 필드 스킵 (`key in metadata`) | 1건 |
      | 비-매핑 가드 (`isinstance(extra, Mapping)`) | 2건 |
      | 직렬화 가드 (`_is_safe_yaml`) | 1건 |

      **최상위 `title`/`tags` 우선순위는 다섯 번째 층이 아니라 이중 방어다.**
      병합 순서(title/tags 앞)와 `key in metadata` 가 **각각 독립적으로** 지켜서,
      하나만 깨면 다른 하나가 구해준다 — 그래서 단일 변이로는 안 떨어진다.
      알리바이인지 이중 방어인지는 **둘을 동시에 깨야** 갈렸다: 동시 제거 시
      `test_top_level_title_and_tags_win` 이 정상적으로 떨어진다. ✅

      ⚠️ 처음 만든 ⑤번 변이는 **틀린 변이**였다 — 원래 병합을 남긴 채 뒤에 하나
      더 붙였는데 `key in metadata` 때문에 두 번째 호출이 no-op 이었다. 0건이
      나왔을 때 "테스트가 알리바이"가 아니라 **"내 변이가 아무것도 안 바꿨다"**
      를 먼저 의심해야 했다.
- [x] 전체 게이트 (fresh PG · pytest+cov80 · ruff · format · mypy · lint-imports)
- [x] `_io.py` 350 LOC 캡 — 병합 로직을 `_seed_frontmatter.py` 로 분리해 지켰다
      (캡이 "docstring 을 줄여라"가 아니라 "쪼개라"고 말하는 가드다)

### ⚠️ #847 의 가드가 설계대로 발화했다

`tests/test_the_plugin_source_ref_is_gone.py::test_write_seed_still_reads_only_
title_tags_and_content` 가 이 변경에서 떨어졌다. 그 가드는 스스로에게 이렇게
적어 뒀었다 — *"누군가 나중에 `write_seed` 가 출처를 읽게 만들면 이 테스트가
떨어지고, 그때는 이 PR 의 전제가 바뀐 것이다 — 가드를 고치기 전에 그 판단부터."*

판단은 거쳤다(형님, *"1은 반영하게 하자"*). `source_ref` 가 여전히 그 집합에
**없다**는 이 파일의 명제는 그대로고, 바뀐 것은 형제 키 하나가 소비자를 얻었다는
사실뿐이라 집합에 `frontmatter` 를 더했다.

### ⚠️ RED 가 알려준 것 — 알리바이 3건을 먼저 고쳤다

처음 쓴 7건 중 **RED 가 1건뿐**이었다. `frontmatter` 를 통째로 무시하는 현재
구현이 *"시스템 필드가 이긴다"* · *"최상위가 이긴다"* · *"`frontmatter` 키가 안
나온다"* 를 **공짜로 만족**시켰기 때문이다 — no-op 구현에서도 통과하는 알리바이.

각 단언에 **충돌하지 않는 키**(`conversation_uuid` · `message_count` · `k`)를
같이 넣어 *"병합이 실제로 일어났고 그 위에서 우선순위가 지켜졌다"* 를 요구하게
바꾸자 RED 가 1 → 4 로 늘었다.

## 실사용 검증은 불가 — 그리고 그 이유가 적혀 있다

이 값을 채우는 커넥터 4종은 prod 에 설치돼 있지 않고 `last_import_at` 이 전 행
NULL 이다(#847 측정). 누를 표면이 없어 브라우저 E2E 항목을 만들지 않는다.
