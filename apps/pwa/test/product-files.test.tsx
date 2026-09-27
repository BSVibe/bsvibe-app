/**
 * Product "Files" viewer (components/products/ProductFiles.tsx) — a lazy
 * file-tree browser over the product's git main. Verified here:
 *  - the root level renders (dirs + files) on mount
 *  - a folder expands LAZILY (children fetched only on first open, with the
 *    subdir path) and collapses
 *  - selecting a file fetches + renders its content from the product endpoint
 *  - a failed content read degrades to a calm note (no blank, no throw)
 */

import ProductFiles from "@/components/products/ProductFiles";
import type { FileTreeEntry, ProductFileContent } from "@/lib/api/types";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const ROOT: FileTreeEntry[] = [
  { name: "src", path: "src", kind: "dir" },
  { name: "README.md", path: "README.md", kind: "file" },
];
const SRC: FileTreeEntry[] = [{ name: "app.py", path: "src/app.py", kind: "file" }];

function contentOf(path: string): ProductFileContent {
  return { path, content: `content of ${path}`, truncated: false, binary: false };
}

afterEach(() => vi.restoreAllMocks());

it("renders the root level on mount", async () => {
  const listFiles = vi.fn(async () => ROOT);
  // ⚠️ 이 스텁은 **프로미스를 돌려줘야 한다.** 예전엔 `vi.fn()`(→ `undefined`)이었고,
  // 아무도 호출하지 않아서 드러나지 않았다. #1042 의 자동 열기가 붙자 그 스텁이
  // 처음으로 불렸고 `.then of undefined` 로 터졌다 — 프로덕션이 주지 않는 모양을
  // 테스트가 주고 있었던 것이지, 제품이 깨진 게 아니다.
  render(
    <ProductFiles
      productId="p1"
      listFiles={listFiles}
      getContent={vi.fn(async (_p: string, path: string) => contentOf(path))}
    />,
  );

  await waitFor(() => expect(screen.getByRole("button", { name: /src/ })).toBeInTheDocument());
  expect(screen.getByRole("button", { name: "README.md" })).toBeInTheDocument();
  // Lazy: only the root was fetched (no eager walk into src/).
  expect(listFiles).toHaveBeenCalledTimes(1);
  expect(listFiles).toHaveBeenCalledWith("p1");
});

it("expands a folder lazily and fetches its children with the subdir path", async () => {
  const listFiles = vi.fn(async (_pid: string, path = "") => (path === "src" ? SRC : ROOT));
  render(
    <ProductFiles
      productId="p1"
      listFiles={listFiles}
      getContent={vi.fn(async (_p: string, path: string) => contentOf(path))}
    />,
  );

  await waitFor(() => screen.getByRole("button", { name: /src/ }));
  // app.py isn't fetched until the folder is opened.
  expect(screen.queryByRole("button", { name: "app.py" })).not.toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: /src/ }));
  await waitFor(() => expect(screen.getByRole("button", { name: "app.py" })).toBeInTheDocument());
  expect(listFiles).toHaveBeenCalledWith("p1", "src");
});

it("fetches + renders a file's content from the product endpoint", async () => {
  const content: ProductFileContent = {
    path: "README.md",
    content: "# hello world",
    truncated: false,
    binary: false,
  };
  const getContent = vi.fn(async () => content);
  render(
    <ProductFiles productId="p1" listFiles={vi.fn(async () => ROOT)} getContent={getContent} />,
  );

  await waitFor(() => screen.getByRole("button", { name: "README.md" }));
  await userEvent.click(screen.getByRole("button", { name: "README.md" }));

  await waitFor(() => expect(screen.getByText("# hello world")).toBeInTheDocument());
  expect(getContent).toHaveBeenCalledWith("p1", "README.md");
});

it("degrades a failed content read to a calm note", async () => {
  const getContent = vi.fn(async () => {
    throw new Error("404");
  });
  render(
    <ProductFiles productId="p1" listFiles={vi.fn(async () => ROOT)} getContent={getContent} />,
  );

  await waitFor(() => screen.getByRole("button", { name: "README.md" }));
  await userEvent.click(screen.getByRole("button", { name: "README.md" }));
  await waitFor(() => expect(screen.getByText(/Couldn.t open this file/i)).toBeInTheDocument());
});

it("shows a calm empty state when the repo lists nothing", async () => {
  render(<ProductFiles productId="p1" listFiles={vi.fn(async () => [])} getContent={vi.fn()} />);
  await waitFor(() => expect(screen.getByText(/No files produced/i)).toBeInTheDocument());
});

// ── #1042 — 오른쪽 절반이 빈 사각형이었다 ────────────────────────────────────
//
// 2026-09-24 prod 캡처: 파일 탭을 열면 트리는 정상인데 오른쪽은 "볼 파일을
// 선택하세요." 한 줄만 있는 빈 상자였고, 페이지 하단 60%가 공백이었다. 형님이
// 실사용에서 지적한 축이고, 내가 화면으로 확인했다.
//
// 빈 상자를 **예쁘게** 만드는 게 아니라 **채운다**: 들어가자마자 읽을 만한 파일
// 하나를 연다. 규칙은 결정적이어야 한다 — 열릴 파일이 런마다 다르면 그 화면을
// 근거로 아무 말도 할 수 없다.

const ROOT_NO_README: FileTreeEntry[] = [
  { name: "src", path: "src", kind: "dir" },
  { name: "pyproject.toml", path: "pyproject.toml", kind: "file" },
  { name: "uv.lock", path: "uv.lock", kind: "file" },
];

it("opens the README on arrival so the content pane is never an empty box", async () => {
  const getContent = vi.fn(async (_pid: string, path: string) => contentOf(path));
  render(
    <ProductFiles productId="p1" listFiles={vi.fn(async () => ROOT)} getContent={getContent} />,
  );

  await waitFor(() => expect(getContent).toHaveBeenCalledWith("p1", "README.md"));
  expect(await screen.findByText("content of README.md")).toBeInTheDocument();
});

it("prefers the README even when another file sorts before it", async () => {
  // 루트 순서는 백엔드가 정한다. "첫 파일"로만 골랐다면 `.gitignore` 가 열린다 —
  // 아무도 그걸 보려고 파일 탭에 들어오지 않는다.
  const ordered: FileTreeEntry[] = [
    { name: ".gitignore", path: ".gitignore", kind: "file" },
    { name: "README.md", path: "README.md", kind: "file" },
  ];
  const getContent = vi.fn(async (_pid: string, path: string) => contentOf(path));
  render(
    <ProductFiles productId="p1" listFiles={vi.fn(async () => ordered)} getContent={getContent} />,
  );

  await waitFor(() => expect(getContent).toHaveBeenCalledTimes(1));
  expect(getContent).toHaveBeenCalledWith("p1", "README.md");
});

it("falls back to the first root file when there is no README", async () => {
  const getContent = vi.fn(async (_pid: string, path: string) => contentOf(path));
  render(
    <ProductFiles
      productId="p1"
      listFiles={vi.fn(async () => ROOT_NO_README)}
      getContent={getContent}
    />,
  );

  await waitFor(() => expect(getContent).toHaveBeenCalledTimes(1));
  // 디렉터리(`src`)는 내용을 읽을 수 없다 — 파일만 고른다.
  expect(getContent).toHaveBeenCalledWith("p1", "pyproject.toml");
});

it("opens nothing when the root has only directories", async () => {
  const getContent = vi.fn(async (_pid: string, path: string) => contentOf(path));
  render(
    <ProductFiles
      productId="p1"
      listFiles={vi.fn(async () => [{ name: "src", path: "src", kind: "dir" }] as FileTreeEntry[])}
      getContent={getContent}
    />,
  );

  await waitFor(() => screen.getByRole("button", { name: /src/ }));
  // 열 파일이 없으면 **아무것도 안 연다.** 디렉터리를 파일처럼 읽으러 가면
  // 404 를 받아 "읽을 수 없음"이 뜨는데, 그건 빈 상자보다 나쁘다.
  expect(getContent).not.toHaveBeenCalled();
});

// ── 브레드크럼 — 지금은 경로가 한 덩어리 문자열이다 ──────────────────────────

it("breaks the selected path into breadcrumb segments", async () => {
  const listFiles = vi.fn(async (_pid: string, path = "") => (path === "src" ? SRC : ROOT));
  const getContent = vi.fn(async (_pid: string, path: string) => contentOf(path));
  render(<ProductFiles productId="p1" listFiles={listFiles} getContent={getContent} />);

  await waitFor(() => screen.getByRole("button", { name: /src/ }));
  await userEvent.click(screen.getByRole("button", { name: /src/ }));
  await waitFor(() => screen.getByRole("button", { name: "app.py" }));
  await userEvent.click(screen.getByRole("button", { name: "app.py" }));

  const crumb = await screen.findByRole("navigation", { name: /path|경로/i });
  // 마지막 조각은 파일이고, 앞 조각들은 디렉터리다.
  expect(crumb).toHaveTextContent("src");
  expect(crumb).toHaveTextContent("app.py");
});

it("a breadcrumb directory opens that directory in the tree", async () => {
  // 브레드크럼이 장식이면 안 된다 — 누르면 무언가 해야 한다. 여기서는 그 디렉터리를
  // 트리에서 펼친다(접어 둔 채로 파일만 보고 있는 상태에서 돌아가는 길).
  const listFiles = vi.fn(async (_pid: string, path = "") => (path === "src" ? SRC : ROOT));
  const getContent = vi.fn(async (_pid: string, path: string) => contentOf(path));
  render(<ProductFiles productId="p1" listFiles={listFiles} getContent={getContent} />);

  await waitFor(() => screen.getByRole("button", { name: /src/ }));
  await userEvent.click(screen.getByRole("button", { name: /src/ }));
  await waitFor(() => screen.getByRole("button", { name: "app.py" }));
  await userEvent.click(screen.getByRole("button", { name: "app.py" }));
  // 트리에서 src 를 접는다 — 이제 app.py 는 안 보이지만 내용은 열려 있다.
  // ⚠️ 이 시점엔 `/src/` 가 **둘**이다(트리 노드 + 브레드크럼). 트리 노드만
  // `aria-expanded` 를 들고 있으므로 그걸로 좁힌다 — 클래스 선택자로 좁히면
  // 스타일을 바꾸는 사람이 테스트를 깨뜨린다.
  await userEvent.click(screen.getByRole("button", { name: /src/, expanded: true }));
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "app.py" })).not.toBeInTheDocument(),
  );

  const crumb = await screen.findByRole("navigation", { name: /path|경로/i });
  await userEvent.click(within(crumb).getByRole("button", { name: "src" }));

  await waitFor(() => expect(screen.getByRole("button", { name: "app.py" })).toBeInTheDocument());
});

it("a breadcrumb directory that is already open stays open", async () => {
  // ⭐ 이 테스트가 있는 이유: `openDir` 을 토글로 바꿔도 위 테스트는 **초록이었다**
  // (그 시나리오에선 디렉터리가 접혀 있어 토글과 펼치기가 같은 일을 한다).
  // 절단해도 안 뒤집히는 방어는 아무것도 안 지킨다 — 뒤집히는 자리를 만든다.
  //
  // 사람의 의도로 읽으면 이렇다: 브레드크럼의 `src` 를 누르는 것은 "src 로 가자"
  // 이지 "src 를 닫자"가 아니다. 토글이면 정확히 반대가 일어난다.
  const listFiles = vi.fn(async (_pid: string, path = "") => (path === "src" ? SRC : ROOT));
  const getContent = vi.fn(async (_p: string, path: string) => contentOf(path));
  render(<ProductFiles productId="p1" listFiles={listFiles} getContent={getContent} />);

  await waitFor(() => screen.getByRole("button", { name: /src/ }));
  await userEvent.click(screen.getByRole("button", { name: /src/, expanded: false }));
  await waitFor(() => screen.getByRole("button", { name: "app.py" }));
  await userEvent.click(screen.getByRole("button", { name: "app.py" }));

  // 트리에서 src 는 **열려 있는 채로** 브레드크럼의 src 를 누른다.
  const crumb = await screen.findByRole("navigation", { name: /path|경로/i });
  await userEvent.click(within(crumb).getByRole("button", { name: "src" }));

  expect(screen.getByRole("button", { name: "app.py" })).toBeInTheDocument();
});
