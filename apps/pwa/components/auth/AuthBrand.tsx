import { useTranslations } from "next-intl";

/** Shared brand block for the auth surfaces (login / forgot-password / callback):
 *  the spark mark + BSVibe wordmark + tagline, centered. Notion-craft (UX §5). */
export function AuthBrand() {
  const t = useTranslations("auth");
  return (
    <div className="login__brand">
      <SparkMark />
      <span className="login__wordmark">{t("wordmark")}</span>
      <span className="login__tagline">{t("tagline")}</span>
    </div>
  );
}

function SparkMark() {
  return (
    <svg
      className="login__mark"
      width="22"
      height="22"
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
    >
      <path d="M12 0c.9 6.3 4.8 10.2 11.1 11.1v1.8C16.8 13.8 12.9 17.7 12 24c-.9-6.3-4.8-10.2-11.1-11.1v-1.8C7.2 10.2 11.1 6.3 12 0Z" />
    </svg>
  );
}
