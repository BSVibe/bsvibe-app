/**
 * Settings → Notifications tab — a real route target with a placeholder body. A
 * later lift fills it; the nav already enumerates it so navigation works today.
 */
export default function NotificationsTab() {
  return (
    <div className="general-tab">
      <p className="general-tab__lede">Notifications — how I reach you.</p>
      <section className="settings-stub" aria-label="Notifications">
        <h2 className="section-label">Notifications</h2>
        <p className="settings-stub__note">Coming soon.</p>
      </section>
    </div>
  );
}
