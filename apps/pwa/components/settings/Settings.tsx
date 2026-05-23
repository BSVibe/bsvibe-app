import Connectors from "./Connectors";
import ModelAccounts from "./ModelAccounts";

/**
 * The Settings surface (the left-rail "Settings" route). It is a thin host for
 * the founder's configuration sections:
 *
 *  - Model accounts — the LLM account(s) the agent loop runs on (load-bearing:
 *    the worker pauses a run without an active model account).
 *  - Connectors — wiring an external service in (inbound webhook) and out
 *    (outbound delivery target) without curl.
 *
 * Each is an independent <section>; keeping this host thin means a new surface
 * is just another section alongside these.
 */
export default function Settings() {
  return (
    <div className="settings">
      <h1 className="settings__heading">Settings</h1>
      <p className="settings__lede">Wire up the model and services I work with.</p>
      <ModelAccounts />
      <Connectors />
    </div>
  );
}
