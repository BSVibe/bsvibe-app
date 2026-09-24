"use client";

import { listConnectors as realListConnectors } from "@/lib/api/connectors";
import {
  createBinding as realCreateBinding,
  listBindings as realListBindings,
  removeBinding as realRemoveBinding,
  updateBinding as realUpdateBinding,
} from "@/lib/api/resource-bindings";
import {
  type Connector,
  OUTPUT_MODES,
  type OutputMode,
  type ResourceBinding,
  type ResourceBindingCreate,
  type ResourceBindingUpdate,
} from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

/**
 * "Connector bindings" — the per-Product × ConnectorAccount 3-knob binding
 * surface (Workflow §3). Each row shows the connector + the connector-side
 * `resource_id`, with one control for the knob founders flip day-to-day:
 *
 *   - `output_mode`      ← select    (`safe` | `direct`; `safe` is the default
 *                                     for non-founder triggers, sending the
 *                                     Deliverable to the Safe Mode queue)
 *
 * 2026-09-22 — 여기 `trigger.enabled` 체크박스가 하나 더 있었다. #924 가
 * 백엔드에서 그 키를 지운 뒤로 그 컨트롤은 **없는 값을 읽어 항상 꺼짐으로
 * 보이고**, 켜면 PATCH 가 REST 의 `extra="forbid"` 에 걸려 422 로 튕겨
 * `changeError` 만 띄웠다. 죽은 노브를 되살리는 대신 컨트롤을 지웠다 —
 * "반응하나"는 `trigger.filters` 가 이미 말하고, 그건 아직 UI 가 없다(B10c).
 *
 * `trigger.filters` 와 JSON 모양의 `selection` 노브는 아직 안 올렸다 — 커넥터마다
 * 모양이 달라 한 UI 로 안 덮인다.
 *
 * The list/mutate clients (+ the connector list for the Add form's
 * dropdown) are injected so the surface is unit-testable against mocks
 * (mirrors ProductResources / NewProductForm).
 */
type ListState =
  | { state: "loading" }
  | { state: "error" }
  | { state: "ready"; rows: ResourceBinding[] };

/** 고를 수 있는 커넥터인가 — 취소된 것은 아니다 (#1057).
 *
 * 옛 응답이 `is_active` 를 안 실어 주면(`undefined`) 막지 않는다: 알 수 없음을
 * 취소로 읽으면 멀쩡한 커넥터가 조용히 사라진다. */
function isSelectable(c: Connector): boolean {
  return c.is_active !== false;
}

export default function ProductBindings({
  productId,
  listBindings = realListBindings,
  createBinding = realCreateBinding,
  updateBinding = realUpdateBinding,
  removeBinding = realRemoveBinding,
  listConnectors = realListConnectors,
}: {
  productId: string;
  listBindings?: (productId: string) => Promise<ResourceBinding[]>;
  createBinding?: (productId: string, input: ResourceBindingCreate) => Promise<ResourceBinding>;
  updateBinding?: (
    productId: string,
    bindingId: string,
    patch: ResourceBindingUpdate,
  ) => Promise<ResourceBinding>;
  removeBinding?: (productId: string, bindingId: string) => Promise<void>;
  listConnectors?: () => Promise<Connector[]>;
}) {
  const t = useTranslations("products.bindings");

  const [list, setList] = useState<ListState>({ state: "loading" });
  const [connectors, setConnectors] = useState<Connector[]>([]);
  // #1057 — 취소된(soft-revoke) 커넥터는 **고를 수 있는 것이 아니다.**
  // `DELETE /api/v1/connectors/{id}` 는 `is_active` 를 내리고 ingress 는 404 하며,
  // `connectors/resolver.py` 는 해소할 때 `is_active` 로 거른다 ⇒ 거기 건 바인딩은
  // 절대 해소되지 않는다. 그런데 폼은 그걸 유효한 선택지로 줬고, 고르면 성공했고,
  // 행도 멀쩡히 렌더됐다(2026-09-24 prod 실측). 유령은 메뉴에서 뺀다.
  //
  // 서버가 준 목록은 그대로 들고 있는다 — 거르는 것은 **선택지**뿐이다.
  const selectable = connectors.filter(isSelectable);
  // Form state for adding a new binding.
  const [adding, setAdding] = useState(false);
  const [formConnectorId, setFormConnectorId] = useState<string>("");
  const [formResourceId, setFormResourceId] = useState<string>("");
  const [formOutputMode, setFormOutputMode] = useState<OutputMode>("safe");
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // Per-row mutation state: the row whose knob/remove is in flight (to disable
  // its controls) + a calm inline error when a change fails (previously a
  // failed mutation just silently re-read the list, reverting the control with
  // no explanation).
  const [busyRow, setBusyRow] = useState<string | null>(null);
  const [changeError, setChangeError] = useState<string | null>(null);

  async function load() {
    try {
      const rows = await listBindings(productId);
      setList({ state: "ready", rows });
    } catch {
      setList({ state: "error" });
    }
  }

  useEffect(() => {
    let active = true;
    listBindings(productId)
      .then((rows) => {
        if (active) setList({ state: "ready", rows });
      })
      .catch(() => {
        if (active) setList({ state: "error" });
      });
    return () => {
      active = false;
    };
  }, [productId, listBindings]);

  // Lazy-load the connector list for the Add form's picker the first time the
  // founder opens it — keeps the steady-state surface a single GET.
  useEffect(() => {
    if (!adding) return;
    let active = true;
    listConnectors()
      .then((rows) => {
        if (!active) return;
        setConnectors(rows);
        // 기본 선택도 **고를 수 있는 것** 중에서 골라야 한다. `rows[0]` 로 두면
        // 첫 커넥터가 취소된 것일 때 폼이 열리자마자 유령을 집고 있게 된다 —
        // 화면에는 그 옵션이 없는데 제출은 그 id 로 나간다.
        const usable = rows.filter(isSelectable);
        if (usable.length > 0 && !formConnectorId) setFormConnectorId(usable[0].id);
      })
      .catch(() => {
        /* leave dropdown empty — submit is gated on a chosen connector */
      });
    return () => {
      active = false;
    };
  }, [adding, listConnectors, formConnectorId]);

  const canSubmit = !submitting && formConnectorId.length > 0 && formResourceId.trim().length > 0;

  async function submitAdd() {
    if (!canSubmit) return;
    setSubmitting(true);
    setFormError(null);
    try {
      await createBinding(productId, {
        connector_account_id: formConnectorId,
        resource_id: formResourceId,
        output_mode: formOutputMode,
      });
      // Reset form, close, and re-read.
      setFormResourceId("");
      setFormOutputMode("safe");
      setAdding(false);
      await load();
    } catch {
      setFormError(t("addError"));
    } finally {
      setSubmitting(false);
    }
  }

  async function runRowMutation(rowId: string, op: () => Promise<unknown>) {
    setBusyRow(rowId);
    setChangeError(null);
    try {
      await op();
    } catch {
      // Surface the failure instead of letting the re-read silently revert the
      // control — the founder must know the change didn't take.
      setChangeError(t("changeError"));
    } finally {
      setBusyRow(null);
      await load();
    }
  }

  async function setOutputMode(row: ResourceBinding, next: OutputMode) {
    await runRowMutation(row.id, () => updateBinding(productId, row.id, { output_mode: next }));
  }

  async function remove(row: ResourceBinding) {
    await runRowMutation(row.id, () => removeBinding(productId, row.id));
  }

  return (
    <section className="product-bindings" aria-label={t("heading")}>
      <header className="product-bindings__head">
        <h2 className="section-label">{t("heading")}</h2>
        <button
          type="button"
          className="product-bindings__add"
          onClick={() => setAdding((v) => !v)}
        >
          {adding ? t("cancel") : t("add")}
        </button>
      </header>

      {list.state === "loading" && (
        <p className="product-bindings__note" aria-busy="true">
          {t("loading")}
        </p>
      )}
      {list.state === "error" && (
        <p className="product-bindings__note" aria-live="polite">
          {t("loadError")}
        </p>
      )}
      {list.state === "ready" && list.rows.length === 0 && !adding && (
        <p className="product-bindings__empty">{t("empty")}</p>
      )}
      {list.state === "ready" && list.rows.length > 0 && (
        <ul className="product-bindings__list">
          {list.rows.map((row) => (
            <li key={row.id} className="product-bindings__row">
              <div className="product-bindings__ident">
                <span className="product-bindings__resource">{row.resource_id}</span>
                {/* #1042 — 여기엔 `connector_account_id` 앞 8자가 있었다. uuid 조각은
                    형님께 아무 의미가 없는데 제목 옆 가장 눈에 띄는 자리를 차지했고,
                    `aria-hidden` 이라 **시각 사용자에게만 보이는 노이즈**였다.
                    정작 `8242700007` 이 텔레그램 채팅이라는 건 화면에 없었다.
                    서버가 종류를 실어 주므로(ResourceBindingResponse.connector)
                    그걸 보여준다. 없으면(옛 응답) 조용히 비운다 — 깨지지 않는다. */}
                {row.connector && (
                  <span className="product-bindings__connector">{row.connector}</span>
                )}
              </div>

              <label className="product-bindings__knob">
                <span>{t("outputMode")}</span>
                <select
                  value={row.output_mode}
                  onChange={(e) => setOutputMode(row, e.target.value as OutputMode)}
                  aria-label={t("outputMode")}
                  disabled={busyRow === row.id}
                >
                  {OUTPUT_MODES.map((m) => (
                    <option key={m} value={m}>
                      {t(`outputModes.${m}`)}
                    </option>
                  ))}
                </select>
              </label>

              <button
                type="button"
                className="product-bindings__remove"
                onClick={() => remove(row)}
                title={t("remove")}
                aria-label={t("remove")}
                disabled={busyRow === row.id}
                aria-busy={busyRow === row.id}
              >
                {t("remove")}
              </button>
            </li>
          ))}
        </ul>
      )}

      {changeError && (
        <p className="product-bindings__error" role="alert" aria-live="polite">
          {changeError}
        </p>
      )}

      {adding && (
        <form
          className="product-bindings__form"
          aria-label={t("add")}
          onSubmit={(e) => {
            e.preventDefault();
            submitAdd();
          }}
        >
          <div className="product-bindings__field">
            <label htmlFor="binding-connector">{t("form.connector")}</label>
            <select
              id="binding-connector"
              value={formConnectorId}
              onChange={(e) => setFormConnectorId(e.target.value)}
              disabled={submitting}
            >
              {selectable.length === 0 ? (
                <option value="">{t("form.noConnectors")}</option>
              ) : (
                selectable.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.connector}
                    {c.external_ref ? ` — ${c.external_ref}` : ""}
                  </option>
                ))
              )}
            </select>
          </div>

          <div className="product-bindings__field">
            <label htmlFor="binding-resource">{t("form.resourceId")}</label>
            <input
              id="binding-resource"
              type="text"
              value={formResourceId}
              placeholder={t("form.resourceIdPlaceholder")}
              onChange={(e) => setFormResourceId(e.target.value)}
              disabled={submitting}
            />
          </div>

          <div className="product-bindings__field">
            <label htmlFor="binding-output-mode">{t("outputMode")}</label>
            <select
              id="binding-output-mode"
              value={formOutputMode}
              onChange={(e) => setFormOutputMode(e.target.value as OutputMode)}
              disabled={submitting}
            >
              {OUTPUT_MODES.map((m) => (
                <option key={m} value={m}>
                  {t(`outputModes.${m}`)}
                </option>
              ))}
            </select>
          </div>

          {formError && (
            <p className="product-bindings__error" aria-live="polite">
              {formError}
            </p>
          )}

          <div className="product-bindings__form-foot">
            <button
              type="button"
              className="product-bindings__cancel"
              onClick={() => setAdding(false)}
              disabled={submitting}
            >
              {t("cancel")}
            </button>
            <button type="submit" className="product-bindings__submit" disabled={!canSubmit}>
              {submitting ? t("form.adding") : t("form.add")}
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
