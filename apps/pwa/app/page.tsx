export default function LoginPage() {
  return (
    <main className="login">
      <h1>BSVibe</h1>
      <p>Sign in</p>
      <form>
        <label htmlFor="email">Email</label>
        <input id="email" type="email" name="email" autoComplete="email" disabled />
        <button type="submit" disabled>
          Continue
        </button>
      </form>
      <p className="placeholder">Phase 0 placeholder — auth wires up in Phase 1.</p>
    </main>
  );
}
