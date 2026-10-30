"use client";

import type { ChangeFootprintFields } from "@/lib/api/types";
import { useTranslations } from "next-intl";

/**
 * How many files a deliverable changes — shown on the Safe Mode card and the report.
 * A change past the backend threshold (`large_change`) gets a warning and its busiest
 * top-level directories, so a sandbox package store swept into the change is visible
 * BEFORE approval. Renders nothing when the deliverable declared no artifact_refs.
 */
export default function ChangeFootprint({ footprint }: { footprint?: ChangeFootprintFields }) {
  const t = useTranslations("changeFootprint");
  const count = footprint?.changed_files;
  if (count === null || count === undefined) return null;
  const large = footprint?.large_change === true;
  const topDirs = footprint?.top_dirs ?? [];

  return (
    <div className={`change-footprint${large ? " change-footprint--large" : ""}`}>
      <span className="change-footprint__count">{t("files", { count })}</span>
      {large && (
        <div className="change-footprint__warning" role="alert">
          <strong>{t("warningTitle")}</strong> {t("warningBody", { count })}
          {topDirs.length > 0 && (
            <>
              <span className="change-footprint__label"> {t("topDirs")}:</span>
              <ul className="change-footprint__dirs">
                {topDirs.map((d) => (
                  <li key={d.dir}>
                    {t("dirEntry", { dir: d.dir === "." ? t("rootDir") : d.dir, count: d.count })}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </div>
  );
}
