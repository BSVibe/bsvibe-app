import Connectors from "./Connectors";

/**
 * The Settings surface (the left-rail "Settings" route). Today it carries one
 * section — Connectors — the founder's front door for wiring an external
 * service (inbound webhook + outbound delivery target) without curl.
 *
 * Model-account management (LLM provider keys) is a deliberate later chunk; it
 * is intentionally not built here. Keeping this surface a thin section host
 * means adding it later is just another <section> alongside Connectors.
 */
export default function Settings() {
  return (
    <div className="settings">
      <h1 className="settings__heading">Settings</h1>
      <p className="settings__lede">Wire up the services I work with.</p>
      <Connectors />
    </div>
  );
}
