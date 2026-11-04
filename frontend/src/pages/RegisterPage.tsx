import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth, type SignupRole } from "../auth/AuthProvider";
import { describeApiError } from "../api/client";

export function RegisterPage() {
  const { register } = useAuth();
  const navigate = useNavigate();
  const [form, setForm] = useState({ email: "", username: "", password: "", role: "learner" as SignupRole });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      await register(form.email, form.username, form.password, form.role);
      navigate("/app", { replace: true });
    } catch (err) {
      setError(describeApiError(err, "Registration failed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-card">
        <p className="eyebrow">PROFEXIA</p>
        <h1>Create your account.</h1>
        <form onSubmit={submit} className="stack">
          <label>Email<input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} required /></label>
          <label>Username<input minLength={3} value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} required /></label>
          <label>Password<input type="password" minLength={8} value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required /></label>
          <label>Role
            <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as SignupRole })}>
              <option value="learner">Learner</option>
              <option value="teacher">Teacher</option>
            </select>
          </label>
          {error && <div className="error">{error}</div>}
          <button disabled={busy}>{busy ? "Creating…" : "Create account"}</button>
        </form>
        <p className="muted">Already registered? <Link to="/login">Sign in</Link></p>
      </section>
    </main>
  );
}