/**
 * Settings → Account tab — a real route target with a placeholder body. A later
 * lift fills it (profile, plan, sign-in identities, sessions per the design);
 * the nav already enumerates it so navigation works today.
 */
export default function AccountTab() {
  return (
    <div className="general-tab">
      <p className="general-tab__lede">Account — your profile and plan.</p>
      <section className="settings-stub" aria-label="Account">
        <h2 className="section-label">Account</h2>
        <p className="settings-stub__note">Coming soon.</p>
      </section>
    </div>
  );
}
