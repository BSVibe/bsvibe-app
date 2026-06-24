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
    <div className="general-tab models-tab">
      <p className="general-tab__lede">{t("lede")}</p>

      {/* COMPUTE — the sources the agent loop can run on (LLM accounts + the
          founder's own coding-agent CLIs). */}
      <div className="models-group">
        <p className="models-group__label">{t("groupCompute")}</p>
        <ModelAccounts />
        <ExecutorWorkers />
      </div>

      {/* ROUTING — how a run picks among that compute (the default + any rules).
          Grouped apart so policy reads separately from inventory. */}
      <div className="models-group">
        <p className="models-group__label">{t("groupRouting")}</p>
        <DefaultAccountPicker />
        <RoutingRules />
      </div>
    </div>
  );
}
