import React, { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api.js";
import { useAuth } from "../AuthContext.jsx";
import { TestModeBadge } from "../Layout.jsx";

export default function Login() {
  const { userId } = useParams();
  const navigate = useNavigate();
  const { login } = useAuth();
  const [users, setUsers] = useState([]);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.listUsers().then(setUsers).catch(() => setUsers([]));
  }, []);

  const target = users.find((u) => u.id === userId);

  async function submit(e) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await login(userId, password);
      navigate("/dashboard");
    } catch (err) {
      setError(err.message || "Login failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="centered-page">
      <TestModeBadge />
      <div className="login-card">
        <div className="rzp-header">
          <span className="avatar">{target?.avatar ?? "🔒"}</span>
          <div>
            <div className="to">Signing in as</div>
            <div className="name">{target?.name ?? userId}</div>
          </div>
        </div>
        <form className="rzp-body" onSubmit={submit}>
          <span className="rzp-badge">TEST MODE CHECKOUT</span>
          <div>
            <label htmlFor="pw">Password</label>
            <input
              id="pw"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoFocus
              required
            />
          </div>
          <div className="hint-box">demo password: {target?.demo_password ?? "sankalp123"}</div>
          {error && <div className="error-text">{error}</div>}
          <button className="btn-primary" type="submit" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
          <button
            className="btn-ghost"
            type="button"
            onClick={() => navigate("/")}
            style={{ textAlign: "center" }}
          >
            ← choose a different account
          </button>
        </form>
      </div>
    </div>
  );
}
