"use client";

import { listProducts } from "@/lib/api/products";
import {
  SCHEDULE_KIND_INSTRUCTION,
  SCHEDULE_KIND_PRODUCT_TICK,
  type Schedule,
  type ScheduleCreate,
  createSchedule,
  deleteSchedule,
  getSchedules,
  setScheduleEnabled,
  setScheduleOutputMode,
} from "@/lib/api/schedules";
import { getWorkspace } from "@/lib/api/workspace";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useState } from "react";

/**
 * Settings → Schedules (S3): the PWA producer for `workspace_schedules` — the
 * channel that lets BSVibe start work on its own on a recurring cadence.
 *
 * Honesty rules baked in here:
 *  - It authors BOTH kinds the backend runs (#961): `instruction` (the founder's text is
 *    the task) and `product_tick` (for one product, BSVibe decides and does the next
 *    step). A tick takes a product instead of text, is pre-selected only when there is
 *    exactly one product (a wrong default would start autonomous work on the wrong one),
 *    is disabled with a reason when there is none, and never offers "deliver without
 *    approval" — PT3: BSVibe chose the work, so the founder approves the result.
 *  - `skill` / `plugin_action` ARE genuinely unbuilt (they are not in the
 *    backend's `_SUPPORTED_KINDS`), so the surface still never advertises a
 *    capability that would silently do nothing. Both halves are machine-checked
 *    in `test/schedule-kinds-backend-parity.test.ts` rather than asserted here.
 *  - Recurrence is a small preset picker (매일 09:00 / 매주 월요일 09:00 / 매시간)
 *    plus a raw cron field as the "advanced" escape hatch. A preset just fills
 *    the cron field — there is no NL→cron parsing (S3 scope).
 *  - `next_run_at` comes back as UTC; it is rendered in the workspace time zone
 *    (`workspaces.timezone`, N1b) so "09:00" reads as the founder's local time.
 *
 * The list toggles (enable/disable) and delete are optimistic with revert-on-
 * failure, mirroring the sibling settings tabs.
 */

interface CronPreset {
  id: string;
  cron: string;
}

// A tiny, honest set of common cadences. `advanced` is not a preset — it is the
// raw cron field the founder can always type into directly.
const PRESETS: CronPreset[] = [
  { id: "daily9", cron: "0 9 * * *" },
  { id: "weeklyMon9", cron: "0 9 * * 1" },
  { id: "hourly", cron: "0 * * * *" },
];

export default function SchedulesTab() {
  const t = useTranslations("settings.schedules");

  const [schedules, setSchedules] = useState<Schedule[] | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [timezone, setTimezone] = useState<string>("UTC");

  // Create-form state.
  const [kind, setKind] = useState<string>(SCHEDULE_KIND_INSTRUCTION);
  const [products, setProducts] = useState<{ id: string; name: string }[]>([]);
  const [productId, setProductId] = useState("");
  const [text, setText] = useState("");
  const [cron, setCron] = useState("");
  const [title, setTitle] = useState("");
  const [direct, setDirect] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    getSchedules()
      .then((rows) => {
        if (active) setSchedules(rows);
      })
      .catch(() => {
        if (active) setLoadFailed(true);
      });
    listProducts()
      .then((rows) => {
        if (!active) return;
        setProducts(rows.map((p) => ({ id: p.id, name: p.name })));
        // One product → it is the only sensible target. Several → the founder picks.
        if (rows.length === 1) setProductId(rows[0].id);
      })
      .catch(() => {
        /* no products listed → the tick option stays disabled, which is honest */
      });
    getWorkspace()
      .then((w) => {
        if (active && w.timezone) setTimezone(w.timezone);
      })
      .catch(() => {
        /* tz is best-effort — fall back to UTC (labeled) */
      });
    return () => {
      active = false;
    };
  }, []);

  const timeFormatter = useMemo(
    () =>
      new Intl.DateTimeFormat(undefined, {
        timeZone: timezone,
        dateStyle: "medium",
        timeStyle: "short",
      }),
    [timezone],
  );

  function formatNextRun(iso: string): string {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    try {
      return timeFormatter.format(d);
    } catch {
      return iso;
    }
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (creating) return;
    const trimmedText = text.trim();
    const trimmedCron = cron.trim();
    if (!canSubmit) return;

    setCreating(true);
    setCreateError(null);
    const body: ScheduleCreate = isTick
      ? {
          kind: SCHEDULE_KIND_PRODUCT_TICK,
          text: "",
          cron_expr: trimmedCron,
          product_id: productId,
          title: title.trim() || undefined,
          output_mode: "safe",
        }
      : {
          kind: SCHEDULE_KIND_INSTRUCTION,
          text: trimmedText,
          cron_expr: trimmedCron,
          title: title.trim() || undefined,
          output_mode: direct ? "direct" : "safe",
        };
    createSchedule(body)
      .then((row) => {
        setSchedules((prev) => [row, ...(prev ?? [])]);
        setText("");
        setCron("");
        setTitle("");
        setDirect(false);
      })
      .catch(() => setCreateError(t("createError")))
      .finally(() => setCreating(false));
  }

  const isTick = kind === SCHEDULE_KIND_PRODUCT_TICK;
  const canSubmit =
    !creating && cron.trim() !== "" && (isTick ? productId !== "" : text.trim() !== "");
  const productName = (id: string | null) =>
    products.find((p) => p.id === id)?.name ?? t("deletedProduct");

  function toggleEnabled(row: Schedule) {
    const next = !row.enabled;
    // Optimistic — reflect immediately, PATCH, reconcile, revert on failure.
    setSchedules((prev) =>
      (prev ?? []).map((s) => (s.id === row.id ? { ...s, enabled: next } : s)),
    );
    setScheduleEnabled(row.id, next)
      .then((saved) =>
        setSchedules((prev) => (prev ?? []).map((s) => (s.id === saved.id ? saved : s))),
      )
      .catch(() =>
        setSchedules((prev) =>
          (prev ?? []).map((s) => (s.id === row.id ? { ...s, enabled: row.enabled } : s)),
        ),
      );
  }

  // #1072 — "deliver without approval": optimistic like the enable toggle.
  function toggleDirect(row: Schedule) {
    const next = row.output_mode === "direct" ? "safe" : "direct";
    setSchedules((prev) =>
      (prev ?? []).map((s) => (s.id === row.id ? { ...s, output_mode: next } : s)),
    );
    setScheduleOutputMode(row.id, next)
      .then((saved) =>
        setSchedules((prev) => (prev ?? []).map((s) => (s.id === saved.id ? saved : s))),
      )
      .catch(() =>
        setSchedules((prev) =>
          (prev ?? []).map((s) => (s.id === row.id ? { ...s, output_mode: row.output_mode } : s)),
        ),
      );
  }

  function remove(row: Schedule) {
    const previous = schedules ?? [];
    setSchedules(previous.filter((s) => s.id !== row.id));
    deleteSchedule(row.id).catch(() => setSchedules(previous)); // revert on failure
  }

  return (
    <div className="general-tab">
      <p className="general-tab__lede">{t("lede")}</p>

      <form className="schedules-form" onSubmit={submit} aria-label={t("createTitle")}>
        <h2 className="section-label">{t("createTitle")}</h2>

        <fieldset className="schedules-form__kind">
          <legend className="settings-field__label">{t("kindLabel")}</legend>
          <div className="schedules-form__kind-options">
            {[SCHEDULE_KIND_INSTRUCTION, SCHEDULE_KIND_PRODUCT_TICK].map((k) => (
              <label key={k} className="schedules-form__kind-option">
                <input
                  type="radio"
                  name="schedule-kind"
                  value={k}
                  checked={kind === k}
                  disabled={creating || (k === SCHEDULE_KIND_PRODUCT_TICK && products.length === 0)}
                  onChange={() => setKind(k)}
                />
                <span>{t(`kind.${k}`)}</span>
              </label>
            ))}
          </div>
          {products.length === 0 && <p className="settings-hint">{t("tickNeedsProduct")}</p>}
        </fieldset>

        {isTick ? (
          <>
            <label className="settings-field settings-field--stacked">
              <span className="settings-field__label">{t("productLabel")}</span>
              <select
                className="settings-field__input"
                aria-label={t("productLabel")}
                value={productId}
                disabled={creating}
                onChange={(e) => setProductId(e.target.value)}
              >
                {products.length > 1 && <option value="">{t("productPlaceholder")}</option>}
                {products.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
            <p className="settings-hint">{t("tickCaption")}</p>
          </>
        ) : (
          <label className="settings-field settings-field--stacked">
            <span className="settings-field__label">{t("instructionLabel")}</span>
            <textarea
              className="settings-field__input schedules-form__instruction"
              aria-label={t("instructionLabel")}
              placeholder={t("instructionPlaceholder")}
              value={text}
              rows={3}
              disabled={creating}
              onChange={(e) => setText(e.target.value)}
            />
          </label>
        )}

        <fieldset className="schedules-form__recurrence">
          <legend className="settings-field__label">{t("recurrenceLabel")}</legend>
          <div className="schedules-form__presets">
            {PRESETS.map((preset) => (
              <button
                key={preset.id}
                type="button"
                className="schedules-form__preset"
                aria-pressed={cron.trim() === preset.cron}
                onClick={() => setCron(preset.cron)}
                disabled={creating}
              >
                {t(`preset.${preset.id}`)}
              </button>
            ))}
          </div>
          <label className="settings-field settings-field--stacked">
            <span className="settings-field__label">{t("cronLabel")}</span>
            <input
              type="text"
              className="settings-field__input schedules-form__cron"
              aria-label={t("cronLabel")}
              placeholder="0 9 * * 1"
              value={cron}
              disabled={creating}
              onChange={(e) => setCron(e.target.value)}
            />
            <span className="settings-field__caption">{t("cronCaption")}</span>
          </label>
        </fieldset>

        <label className="settings-field settings-field--stacked">
          <span className="settings-field__label">{t("titleLabel")}</span>
          <input
            type="text"
            className="settings-field__input"
            aria-label={t("titleLabel")}
            placeholder={t("titlePlaceholder")}
            value={title}
            disabled={creating}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>

        {!isTick && (
          <>
            <label className="schedule-row__toggle">
              <input
                type="checkbox"
                aria-label={t("directLabel")}
                checked={direct}
                disabled={creating}
                onChange={(e) => setDirect(e.target.checked)}
              />
              <span>{t("directLabel")}</span>
            </label>
            <p className="settings-hint">{t("directCaption")}</p>
          </>
        )}

        <button type="submit" className="schedules-form__submit" disabled={!canSubmit}>
          {creating ? t("creating") : t("create")}
        </button>
        {createError && (
          <p className="schedules-form__error" role="alert">
            {createError}
          </p>
        )}
      </form>

      <section className="schedules-list" aria-label={t("listTitle")}>
        <h2 className="section-label">{t("listTitle")}</h2>

        {loadFailed ? (
          <p className="schedules__note" role="alert">
            {t("loadError")}
          </p>
        ) : schedules === null ? (
          <p className="schedules__note">{t("loading")}</p>
        ) : schedules.length === 0 ? (
          <div className="schedules-empty">
            <p className="schedules-empty__title">{t("emptyTitle")}</p>
            <p className="schedules__note">{t("emptyHint")}</p>
          </div>
        ) : (
          <ul className="schedules-list__items">
            {schedules.map((row) => {
              const heading =
                row.title?.trim() ||
                (row.kind === SCHEDULE_KIND_PRODUCT_TICK
                  ? t("tickHeading", { product: productName(row.product_id) })
                  : row.text);
              return (
                <li key={row.id} className="schedule-row">
                  <div className="schedule-row__main">
                    <p className="schedule-row__title" title={heading}>
                      {heading}
                    </p>
                    {row.title?.trim() && (
                      <p className="schedule-row__text" title={row.text}>
                        {row.text}
                      </p>
                    )}
                    <p className="schedule-row__meta">
                      <code className="schedule-row__cron">{row.cron_expr}</code>
                      <span className="schedule-row__next">
                        {t("nextRun", { when: formatNextRun(row.next_run_at) })}
                      </span>
                    </p>
                  </div>
                  <div className="schedule-row__actions">
                    {row.kind !== SCHEDULE_KIND_PRODUCT_TICK && (
                      <label className="schedule-row__toggle">
                        <input
                          type="checkbox"
                          aria-label={`${heading} — ${t("directLabel")}`}
                          checked={row.output_mode === "direct"}
                          onChange={() => toggleDirect(row)}
                        />
                        <span>{t("directLabel")}</span>
                      </label>
                    )}
                    <label className="schedule-row__toggle">
                      <input
                        type="checkbox"
                        aria-label={`${heading} — ${t("enabledLabel")}`}
                        checked={row.enabled}
                        onChange={() => toggleEnabled(row)}
                      />
                      <span>{t("enabledLabel")}</span>
                    </label>
                    <button
                      type="button"
                      className="schedule-row__delete"
                      aria-label={`${t("delete")} — ${heading}`}
                      onClick={() => remove(row)}
                    >
                      {t("delete")}
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </div>
  );
}
