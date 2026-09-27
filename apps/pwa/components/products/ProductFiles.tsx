"use client";

import {
  getProductFileContent as realGetContent,
  listProductFiles as realListFiles,
  searchProductFiles as realSearchFiles,
} from "@/lib/api/products";
import type { FileTreeEntry, ProductFileContent, ProductFileSearch } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";

/**
 * "Files" — a lazy file-tree browser over the product's git `main`. Replaces
 * the old flat per-deliverable artifact_refs list (which only showed the files
 * a single run touched and didn't scale to a real repo). Each directory's
 * children are fetched on demand from `GET /products/{id}/files?path=` so a
 * large repo stays cheap to browse; selecting a file fetches its content from
 * `GET /products/{id}/files/content?path=`. List left, content right.
 *
 * The list/content clients are injected so the surface is unit-testable.
 */
type ListState =
  | { state: "loading" }
  | { state: "error" }
  | { state: "ready"; entries: FileTreeEntry[] };

type SearchState =
  /** 검색창이 비어 있다 — 트리를 보여준다. */
  | { state: "off" }
  | { state: "searching" }
  | { state: "error" }
  | { state: "ready"; results: FileTreeEntry[]; truncated: boolean };

type ContentState =
  | { state: "idle" }
  | { state: "loading" }
  | { state: "error" }
  | { state: "ready"; content: ProductFileContent };

/** 들어가자마자 열 파일 — 없으면 `null` (#1042).
 *
 * 규칙이 **결정적**이어야 한다: 열리는 파일이 런마다 다르면 그 화면을 근거로
 * 아무 말도 할 수 없다. README 를 먼저 보는 이유는 그게 사람이 이 탭에 들어온
 * 이유에 가장 가깝기 때문이고, 없으면 루트의 첫 **파일**이다. 디렉터리는 고르지
 * 않는다 — 내용을 읽으러 가면 404 를 받아 "읽을 수 없음"이 뜨는데, 그건 빈 상자
 * 보다 나쁘다.
 */
function fileToOpenOnArrival(entries: FileTreeEntry[]): string | null {
  const files = entries.filter((e) => e.kind === "file");
  const readme = files.find((e) => e.name.toLowerCase().startsWith("readme"));
  return (readme ?? files[0])?.path ?? null;
}

export default function ProductFiles({
  productId,
  listFiles = realListFiles,
  getContent = realGetContent,
  searchFiles = realSearchFiles,
}: {
  productId: string;
  listFiles?: (productId: string, path?: string) => Promise<FileTreeEntry[]>;
  getContent?: (productId: string, path: string) => Promise<ProductFileContent>;
  searchFiles?: (productId: string, q: string) => Promise<ProductFileSearch>;
}) {
  const t = useTranslations("products");
  const [root, setRoot] = useState<ListState>({ state: "loading" });
  // Cached children per expanded directory path.
  const [children, setChildren] = useState<Record<string, FileTreeEntry[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [loadingDirs, setLoadingDirs] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState<ContentState>({ state: "idle" });
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchState>({ state: "off" });

  const selectFile = useCallback(
    (path: string) => {
      setSelected(path);
      setContent({ state: "loading" });
      getContent(productId, path)
        .then((c) => setContent({ state: "ready", content: c }))
        .catch(() => setContent({ state: "error" }));
    },
    [getContent, productId],
  );

  // Load the root level on mount / product change. Reset all tree state so a
  // product switch never shows the previous product's tree.
  useEffect(() => {
    let active = true;
    setRoot({ state: "loading" });
    setChildren({});
    setExpanded(new Set());
    setSelected(null);
    setContent({ state: "idle" });
    listFiles(productId)
      .then((entries) => {
        if (!active) return;
        setRoot({ state: "ready", entries });
        // #1042 — 빈 상자로 맞이하지 않는다. 트리만 있고 오른쪽이 비어 있으면
        // 사람은 "여기서 뭘 해야 하지"부터 풀어야 한다.
        const first = fileToOpenOnArrival(entries);
        if (first !== null) selectFile(first);
      })
      .catch(() => active && setRoot({ state: "error" }));
    return () => {
      active = false;
    };
  }, [productId, listFiles, selectFile]);

  // 검색. 타이핑마다 요청이 나가면 느린 응답이 빠른 응답을 덮어써서 **다른
  // 질의의 결과**가 화면에 남는다(질의 "io" 를 쳤는데 "i" 의 결과가 보이는 모양).
  // 그래서 (1) 디바운스하고 (2) 자기 세대 번호가 최신일 때만 반영한다.
  const searchGeneration = useRef(0);
  useEffect(() => {
    const q = query.trim();
    if (!q) {
      setHits({ state: "off" });
      return;
    }
    const mine = ++searchGeneration.current;
    setHits({ state: "searching" });
    const timer = setTimeout(() => {
      searchFiles(productId, q)
        .then((r) => {
          if (searchGeneration.current !== mine) return;
          setHits({ state: "ready", results: r.results, truncated: r.truncated });
        })
        .catch(() => {
          if (searchGeneration.current !== mine) return;
          setHits({ state: "error" });
        });
    }, 180);
    return () => clearTimeout(timer);
  }, [query, productId, searchFiles]);

  const setDirOpen = useCallback(
    async (path: string, willExpand: boolean) => {
      setExpanded((prev) => {
        const next = new Set(prev);
        if (willExpand) {
          next.add(path);
        } else {
          next.delete(path);
        }
        return next;
      });
      // Lazy-load children the first time the folder is opened.
      if (willExpand && children[path] === undefined) {
        setLoadingDirs((s) => new Set(s).add(path));
        try {
          const entries = await listFiles(productId, path);
          setChildren((c) => ({ ...c, [path]: entries }));
        } catch {
          setChildren((c) => ({ ...c, [path]: [] }));
        } finally {
          setLoadingDirs((s) => {
            const n = new Set(s);
            n.delete(path);
            return n;
          });
        }
      }
    },
    [children, listFiles, productId],
  );

  const toggleDir = useCallback(
    (path: string) => setDirOpen(path, !expanded.has(path)),
    [expanded, setDirOpen],
  );

  /** 브레드크럼용 — **펼치기만** 한다. 토글로 두면 이미 열린 디렉터리를 눌렀을 때
   *  돌아가려던 사람이 트리를 닫아 버린다(누르는 의도와 정반대다). */
  const openDir = useCallback((path: string) => setDirOpen(path, true), [setDirOpen]);

  function renderNodes(entries: FileTreeEntry[], depth: number) {
    return (
      <ul className="product-files__tree">
        {entries.map((entry) => (
          <li key={entry.path}>
            {entry.kind === "dir" ? (
              <>
                <button
                  type="button"
                  className="product-files__node product-files__node--dir"
                  style={{ paddingLeft: `${depth * 14 + 8}px` }}
                  aria-expanded={expanded.has(entry.path)}
                  onClick={() => toggleDir(entry.path)}
                >
                  <span className="product-files__caret" aria-hidden="true">
                    {expanded.has(entry.path) ? "▾" : "▸"}
                  </span>
                  {entry.name}
                </button>
                {expanded.has(entry.path) &&
                  (loadingDirs.has(entry.path) ? (
                    <p
                      className="product-files__hint"
                      style={{ paddingLeft: `${(depth + 1) * 14 + 8}px` }}
                      aria-busy="true"
                    >
                      {t("fileLoading")}
                    </p>
                  ) : (
                    renderNodes(children[entry.path] ?? [], depth + 1)
                  ))}
              </>
            ) : (
              <button
                type="button"
                className={`product-files__node product-files__node--file${
                  selected === entry.path ? " product-files__node--active" : ""
                }`}
                style={{ paddingLeft: `${depth * 14 + 22}px` }}
                onClick={() => selectFile(entry.path)}
                title={entry.path}
              >
                {entry.name}
              </button>
            )}
          </li>
        ))}
      </ul>
    );
  }

  return (
    <section className="product-files" aria-label={t("files")}>
      <h2 className="section-label">{t("files")}</h2>

      {root.state === "loading" && (
        <p className="product-files__empty" aria-busy="true">
          {t("fileLoading")}
        </p>
      )}
      {root.state === "error" && <p className="product-files__empty">{t("fileError")}</p>}
      {root.state === "ready" && root.entries.length === 0 && (
        <p className="product-files__empty">{t("filesEmpty")}</p>
      )}

      {root.state === "ready" && root.entries.length > 0 && (
        <div className="product-files__split">
          <div className="product-files__list">
            {/* #1042 — 검색. 트리는 한 단계씩 게으르게 가져오므로 **안 펼친
                디렉터리의 파일은 클라이언트에 존재하지 않는다** — 로컬 필터로는
                절대 못 찾는다. 서버가 커밋된 트리를 재귀로 훑는다. */}
            <input
              type="search"
              className="product-files__search"
              aria-label={t("fileSearchLabel")}
              placeholder={t("fileSearchPlaceholder")}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            {hits.state === "off" && renderNodes(root.entries, 0)}
            {hits.state === "searching" && (
              <p className="product-files__hint" aria-busy="true">
                {t("fileSearching")}
              </p>
            )}
            {hits.state === "error" && <p className="product-files__hint">{t("fileError")}</p>}
            {hits.state === "ready" && hits.results.length === 0 && (
              /* 빈 화면은 "없다"와 "아직 로딩중"과 "깨졌다"를 구분해 주지 않는다. */
              <p className="product-files__hint">{t("fileSearchEmpty")}</p>
            )}
            {hits.state === "ready" && hits.results.length > 0 && (
              <ul className="product-files__tree">
                {hits.results.map((hit) => (
                  <li key={hit.path}>
                    <button
                      type="button"
                      className={`product-files__node${
                        selected === hit.path ? " product-files__node--active" : ""
                      }`}
                      style={{ paddingLeft: 8 }}
                      onClick={() => selectFile(hit.path)}
                      title={hit.path}
                    >
                      {hit.path}
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {hits.state === "ready" && hits.truncated && (
              /* 잘린 것을 안 알리면 화면이 "이게 전부"라고 거짓말한다. */
              <p className="product-files__hint">{t("fileSearchTruncated")}</p>
            )}
          </div>

          <div className="product-files__content">
            {content.state === "idle" && (
              <p className="product-files__hint">{t("filesSelectHint")}</p>
            )}
            {content.state === "loading" && (
              <p className="product-files__hint" aria-busy="true">
                {t("fileLoading")}
              </p>
            )}
            {content.state === "error" && <p className="product-files__hint">{t("fileError")}</p>}
            {content.state === "ready" && (
              <>
                <div className="product-files__file-head">
                  {/* #1042 — 경로가 한 덩어리 문자열이었다. 깊은 파일에서는
                      "지금 어디인가"도, "한 단계 위로"도 그 줄에서 읽을 수 없었다.
                      조각으로 쪼개고, 디렉터리 조각은 트리에서 그 폴더를 연다. */}
                  <nav className="product-files__crumbs" aria-label={t("filePathLabel")}>
                    {(() => {
                      const parts = content.content.path.split("/").filter(Boolean);
                      return parts.map((part, i) => {
                        const isLast = i === parts.length - 1;
                        const dirPath = parts.slice(0, i + 1).join("/");
                        return (
                          <span key={dirPath} className="product-files__crumb">
                            {i > 0 && (
                              <span className="product-files__crumb-sep" aria-hidden="true">
                                /
                              </span>
                            )}
                            {isLast ? (
                              <span className="product-files__crumb-leaf">{part}</span>
                            ) : (
                              <button
                                type="button"
                                className="product-files__crumb-dir"
                                onClick={() => openDir(dirPath)}
                              >
                                {part}
                              </button>
                            )}
                          </span>
                        );
                      });
                    })()}
                  </nav>
                </div>
                {content.content.binary ? (
                  <p className="product-files__hint">{t("fileBinary")}</p>
                ) : (
                  <>
                    {content.content.truncated && (
                      <p className="product-files__truncated">{t("fileTruncated")}</p>
                    )}
                    <pre className="product-files__pre">{content.content.content}</pre>
                  </>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
