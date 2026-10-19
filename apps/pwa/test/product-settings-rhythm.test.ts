/**
 * 설정 탭의 섹션 리듬 (#1042 잔여 — 2026-09-24 prod 눈 확인 §⑤).
 *
 * 「위험 구역」이 「커넥터 바인딩」 빈 상태 바로 밑에 바짝 붙어, 바인딩 섹션의 일부처럼
 * 읽혔다. 원인은 CSS 한 줄이다: `.product-resources, .product-bindings` 는 `margin-top: 28px`
 * 인데 `.product-danger` 에는 위 여백이 없었다. 인증된 화면이라 로컬 Playwright 가 못 닿으므로
 * 스타일시트를 직접 잰다.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const css = readFileSync(join(__dirname, "..", "app", "globals.css"), "utf8");

function block(selector: string): string {
  const re = new RegExp(`(^|\\n)${selector.replace(/[.]/g, "\\.")}\\s*\\{([^}]*)\\}`);
  const m = css.match(re);
  if (!m) throw new Error(`no rule for ${selector}`);
  return m[2];
}

function px(decls: string, prop: string): number {
  const m = decls.match(new RegExp(`${prop}\\s*:\\s*(\\d+)px`));
  return m ? Number(m[1]) : 0;
}

describe("settings tab rhythm", () => {
  it("위험 구역은 다른 섹션보다 더 떨어져 있고 경계선으로 갈린다", () => {
    const danger = block(".product-danger");
    const sectionGap = px(css.match(/\.product-bindings\s*\{([^}]*)\}/)?.[1] ?? "", "margin-top");
    expect(sectionGap).toBeGreaterThan(0);
    expect(px(danger, "margin-top")).toBeGreaterThan(sectionGap);
    expect(danger).toMatch(/border-top\s*:/);
  });
});

describe("tab row → first section", () => {
  it("설정 탭의 첫 섹션은 자기 위 여백을 더하지 않는다 (live: 활동 탭보다 34px 아래서 시작했다)", () => {
    const m = css.match(/\.product-tabs\s*\+\s*\.product-resources\s*\{([^}]*)\}/);
    expect(m?.[1]).toMatch(/margin-top:\s*0/);
  });
});
