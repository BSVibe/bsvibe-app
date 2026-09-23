import { NextResponse } from "next/server";

/**
 * 하이드레이션 전에 제출된 로그인 폼의 **착지점** (#1053).
 *
 * 왜 있나 — 로그인 폼은 `onSubmit` 핸들러 하나에 의존했고 `action`/`method` 가
 * 없었다. React 가 하이드레이션을 끝내기 전에 제출되면 그 핸들러가 아직 안 붙어
 * 있어서 브라우저가 **기본 동작(같은 URL 로 GET)** 을 했고, `name` 이 붙은 모든
 * 필드가 쿼리스트링에 실렸다. 2026-09-23 prod 에서 실측:
 *
 *     https://app.bsvibe.dev/login?email=…&password=…
 *
 * 그 URL 은 사라지지 않는다 — 브라우저 히스토리 · Referer · 액세스 로그(이 건은
 * Vercel).
 *
 * ⚠️ **이 라우트는 로그인을 수행하지 않는다.** 그게 의도다. 지켜야 하는 성질은
 * "자격증명이 URL 에 안 실린다" 이고, `method="post"` 로 본문에 실리는 순간 그건
 * 달성된다. 서버가 대신 로그인하게 만들면 세션을 클라이언트에 심는 경로
 * (`persistSupabaseSession`)를 서버에서 흉내 내야 하고, 그건 이 결함이 요구하는
 * 것보다 훨씬 큰 표면이다 — 그리고 더 크게 틀릴 수 있다.
 *
 * 받은 값은 **읽지도 기록하지도 않고** 버린다. 사용자는 그 사이 로드된 폼에서
 * 다시 누르면 된다.
 */
export async function POST(request: Request): Promise<NextResponse> {
  // 본문을 파싱하지 않는다 — 파싱하면 그 값이 프로세스 메모리와 스택 트레이스에
  // 들어올 수 있다. 여기서 할 일은 "URL 로 안 가게 하는 것" 하나뿐이다.
  const url = new URL("/login", request.url);
  url.searchParams.set("retry", "hydration");
  // 303 — POST 를 GET 으로 바꿔 돌려보낸다(새로고침해도 재전송되지 않는다).
  return NextResponse.redirect(url, 303);
}
