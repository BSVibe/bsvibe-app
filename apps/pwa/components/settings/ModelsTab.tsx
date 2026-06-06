import { useTranslations } from "next-intl";
import DefaultAccountPicker from "./DefaultAccountPicker";
import ExecutorWorkers from "./ExecutorWorkers";
import ModelAccounts from "./ModelAccounts";
import RoutingRules from "./RoutingRules";

/**
 * Settings → Models tab. Hosts the existing model-accounts surface, the
 * executor-workers surface (the design's "subscription accounts" — the
 * founder's own coding-agent CLIs the agent loop can route to), the Lift E2
 * workspace-default ModelAccount picker (the dispatch resolver's fallback
 * when no rule matches), and beneath them the ROUTING section. All
 * sub-surfaces are owned here only as children; this wrapper does not touch
 * their internals.
 */
export default function ModelsTab() {
  const t = useTranslations("settings.models");
  return (
    <div className="general-tab">
      <p className="general-tab__lede">{t("lede")}</p>
      <ModelAccounts />
      <ExecutorWorkers />
      <DefaultAccountPicker />
      <RoutingRules />
    </div>
  );
}
