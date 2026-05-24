import { useTranslations } from "next-intl";
import ExecutorWorkers from "./ExecutorWorkers";
import ModelAccounts from "./ModelAccounts";
import RoutingRules from "./RoutingRules";

/**
 * Settings → Models tab. Hosts the existing model-accounts surface, the
 * executor-workers surface (the design's "subscription accounts" — the
 * founder's own coding-agent CLIs the agent loop can route to), and beneath
 * them the ROUTING section (how work routes to a target model). All
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
      <RoutingRules />
    </div>
  );
}
