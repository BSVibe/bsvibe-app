import Connectors from "./Connectors";

/**
 * Settings → Connectors tab. A thin host for the connectors catalog surface.
 * The <Connectors/> component now carries its own heading + lede (the catalog
 * reframe), so the host stays a plain wrapper.
 */
export default function ConnectorsTab() {
  return (
    <div className="general-tab">
      <Connectors />
    </div>
  );
}
