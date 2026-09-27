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
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const ROOT: FileTreeEntry[] = [
  { name: "src", path: "src", kind: "dir" },
  { name: "README.md", path: "README.md", kind: "file" },
];
const SRC: FileTreeEntry[] = [{ name: "app.py", path: "src/app.py", kind: "file" }];

afterEach(() => vi.restoreAllMocks());

it("renders the root level on mount", async () => {
  const listFiles = vi.fn(async () => ROOT);
  render(<ProductFiles productId="p1" listFiles={listFiles} getContent={vi.fn()} />);

  await waitFor(() => expect(screen.getByRole("button", { name: /src/ })).toBeInTheDocument());
  expect(screen.getByRole("button", { name: "README.md" })).toBeInTheDocument();
  // Lazy: only the root was fetched (no eager walk into src/).
  expect(listFiles).toHaveBeenCalledTimes(1);
  expect(listFiles).toHaveBeenCalledWith("p1");
});

it("expands a folder lazily and fetches its children with the subdir path", async () => {
  const listFiles = vi.fn(async (_pid: string, path = "") => (path === "src" ? SRC : ROOT));
  render(<ProductFiles productId="p1" listFiles={listFiles} getContent={vi.fn()} />);

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
