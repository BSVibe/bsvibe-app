import { useTranslations } from "next-intl";
import ModelAccounts from "./ModelAccounts";

/**
 * Settings → Models tab. A thin host for the existing model-accounts surface.
 * The <ModelAccounts/> component is owned by a parallel lift — this wrapper does
 * not touch it, it only places it under the Models tab.
 */
export default function ModelsTab() {
  const t = useTranslations("settings.models");
  return (
    <div className="general-tab">
      <p className="general-tab__lede">{t("lede")}</p>
      <ModelAccounts />
    </div>
  );
}
