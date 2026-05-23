import Connectors from "./Connectors";

/**
 * Settings → Connectors tab. A thin host for the existing connectors surface.
 * The <Connectors/> component is unchanged by this lift.
 */
export default function ConnectorsTab() {
  return (
    <div className="general-tab">
      <p className="general-tab__lede">Connectors — the services I reach in and out to.</p>
      <Connectors />
    </div>
  );
}
