"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";

/**
 * A read-only value with a copy-to-clipboard affordance. Used by the one-time
 * connector-secret panel to surface the webhook URL + token. `secret` styles
 * the value as monospace and emphasised so a capability reads as one.
 *
 * Clipboard write is best-effort: if `navigator.clipboard` is unavailable
 * (older browser / insecure context) the value stays selectable on screen, so
 * the founder can copy it by hand — the affordance never blocks access to it.
 */
export default function CopyField({
  label,
  value,
  secret = false,
}: {
  label: string;
  value: string;
  secret?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const t = useTranslations("common");

  async function copy() {
    try {
      await navigator.clipboard?.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard unavailable — the value is on screen and selectable.
    }
  }

  return (
    <div className="copy-field">
      <span className="copy-field__label">{label}</span>
      <div className="copy-field__row">
        <code className={`copy-field__value${secret ? " copy-field__value--secret" : ""}`}>
          {value}
        </code>
        <button
          type="button"
          className="copy-field__btn"
          onClick={copy}
          aria-label={t("copyLabel", { label })}
        >
          {copied ? t("copied") : t("copy")}
        </button>
      </div>
    </div>
  );
}
