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
import type { FileTreeEntry, ProductFileContent, ProductFileSearch } from "@/lib/api/types";
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

// ── #1042 — 검색 ───────────────────────────────────────────────────────────
//
// 트리는 한 단계씩 게으르게 가져온다 ⇒ **안 펼친 디렉터리의 파일은 클라이언트에
// 존재하지 않는다.** 로컬 필터로는 절대 못 찾는다. 서버가 재귀로 훑는다.

const DEEP_HIT: FileTreeEntry[] = [{ name: "io.py", path: "src/util/io.py", kind: "file" }];

it("finds a file that the lazy tree has not even fetched yet", async () => {
  const listFiles = vi.fn(async () => ROOT); // src/ 는 한 번도 펼쳐지지 않는다
  const searchFiles = vi.fn(async () => ({ results: DEEP_HIT, truncated: false }));
  render(
    <ProductFiles
      productId="p1"
      listFiles={listFiles}
      getContent={vi.fn(async (_p: string, path: string) => contentOf(path))}
      searchFiles={searchFiles}
    />,
  );

  await waitFor(() => screen.getByRole("button", { name: /src/ }));
  await userEvent.type(screen.getByRole("searchbox", { name: /search|검색/i }), "io");

  await waitFor(() => expect(searchFiles).toHaveBeenCalledWith("p1", "io"));
  expect(await screen.findByRole("button", { name: "src/util/io.py" })).toBeInTheDocument();
  // 로컬 필터였다면 절대 못 찾는다 — src 의 자식은 한 번도 안 가져왔다.
  expect(listFiles).toHaveBeenCalledTimes(1);
});

it("opens a search hit in the content pane", async () => {
  const getContent = vi.fn(async (_p: string, path: string) => contentOf(path));
  render(
    <ProductFiles
      productId="p1"
      listFiles={vi.fn(async () => ROOT)}
      getContent={getContent}
      searchFiles={vi.fn(async () => ({ results: DEEP_HIT, truncated: false }))}
    />,
  );

  await waitFor(() => screen.getByRole("button", { name: /src/ }));
  await userEvent.type(screen.getByRole("searchbox", { name: /search|검색/i }), "io");
  await userEvent.click(await screen.findByRole("button", { name: "src/util/io.py" }));

  expect(await screen.findByText("content of src/util/io.py")).toBeInTheDocument();
});

it("says so when the server truncated the results", async () => {
  // 잘린 것을 안 알리면 화면이 "이게 전부"라고 거짓말한다.
  render(
    <ProductFiles
      productId="p1"
      listFiles={vi.fn(async () => ROOT)}
      getContent={vi.fn(async (_p: string, path: string) => contentOf(path))}
      searchFiles={vi.fn(async () => ({ results: DEEP_HIT, truncated: true }))}
    />,
  );

  await waitFor(() => screen.getByRole("button", { name: /src/ }));
  await userEvent.type(screen.getByRole("searchbox", { name: /search|검색/i }), "io");

  expect(await screen.findByText(/더 있|more/i)).toBeInTheDocument();
});

it("shows the tree again when the search box is cleared", async () => {
  const searchFiles = vi.fn(async () => ({ results: DEEP_HIT, truncated: false }));
  render(
    <ProductFiles
      productId="p1"
      listFiles={vi.fn(async () => ROOT)}
      getContent={vi.fn(async (_p: string, path: string) => contentOf(path))}
      searchFiles={searchFiles}
    />,
  );

  await waitFor(() => screen.getByRole("button", { name: /src/ }));
  const box = screen.getByRole("searchbox", { name: /search|검색/i });
  await userEvent.type(box, "io");
  await screen.findByRole("button", { name: "src/util/io.py" });

  await userEvent.clear(box);
  // 검색을 지우면 트리로 돌아온다 — 빈 결과 화면에 갇히면 안 된다.
  await waitFor(() => expect(screen.getByRole("button", { name: /src/ })).toBeInTheDocument());
  expect(screen.queryByRole("button", { name: "src/util/io.py" })).not.toBeInTheDocument();
});

it("says plainly when a search finds nothing", async () => {
  render(
    <ProductFiles
      productId="p1"
      listFiles={vi.fn(async () => ROOT)}
      getContent={vi.fn(async (_p: string, path: string) => contentOf(path))}
      searchFiles={vi.fn(async () => ({ results: [], truncated: false }))}
    />,
  );

  await waitFor(() => screen.getByRole("button", { name: /src/ }));
  await userEvent.type(screen.getByRole("searchbox", { name: /search|검색/i }), "zzz");

  // 빈 화면은 "없다"와 "아직 로딩중"과 "깨졌다"를 구분해 주지 않는다.
  expect(await screen.findByText(/찾은 파일이 없|no files/i)).toBeInTheDocument();
});

it("does not let a slow earlier search overwrite the newest one", async () => {
  // 타이핑마다 요청이 나가면 느린 응답이 빠른 응답을 **덮어쓴다** — "io" 를 쳤는데
  // "i" 의 결과가 화면에 남는 모양이다. 이 방어를 지워도 초록이면 아무것도 안
  // 지키는 것이므로, 순서를 뒤집어 둔 두 응답으로 고정한다.
  const slow: FileTreeEntry[] = [{ name: "old.py", path: "stale/old.py", kind: "file" }];
  // 콜백 안에서만 대입되는 변수를 TS 는 `never` 로 좁힌다 — 해소자를 밖으로 꺼낸다.
  let resolveFirst!: (v: ProductFileSearch) => void;
  const firstCall = new Promise<ProductFileSearch>((res) => {
    resolveFirst = res;
  });
  const searchFiles = vi
    .fn()
    .mockImplementationOnce(() => firstCall)
    .mockImplementationOnce(async () => ({ results: DEEP_HIT, truncated: false }));

  render(
    <ProductFiles
      productId="p1"
      listFiles={vi.fn(async () => ROOT)}
      getContent={vi.fn(async (_p: string, path: string) => contentOf(path))}
      searchFiles={searchFiles}
    />,
  );

  await waitFor(() => screen.getByRole("button", { name: /src/ }));
  const box = screen.getByRole("searchbox", { name: /search|검색/i });
  await userEvent.type(box, "i");
  await waitFor(() => expect(searchFiles).toHaveBeenCalledTimes(1));
  await userEvent.type(box, "o");
  await waitFor(() => expect(searchFiles).toHaveBeenCalledTimes(2));

  // 최신 결과가 먼저 도착하고, 그 **뒤에** 늦은 첫 응답이 온다.
  await screen.findByRole("button", { name: "src/util/io.py" });
  resolveFirst({ results: slow, truncated: false });

  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "stale/old.py" })).not.toBeInTheDocument(),
  );
  expect(screen.getByRole("button", { name: "src/util/io.py" })).toBeInTheDocument();
});
