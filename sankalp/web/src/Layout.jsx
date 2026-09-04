import React from "react";
import { NavLink, Navigate } from "react-router-dom";
import { useAuth } from "./AuthContext.jsx";

export function TestModeBadge() {
  return (
    <div className="testmode-badge">
      <span className="dot" />
      TEST MODE — no real payment gateway
    </div>
  );
}

export function RequireAuth({ children }) {
  const { token, ready } = useAuth();
  if (!ready) return null;
  if (!token) return <Navigate to="/" replace />;
  return children;
}

export default function Layout({ children }) {
  const { user, logout } = useAuth();
  return (
    <div className="shell">
      <TestModeBadge />
      <aside className="sidebar">
        <div className="brand">
          SAN<span>KALP</span>
        </div>
        {user && (
          <div className="whoami">
            <span className="avatar">{user.avatar}</span>
            <div>
              <div className="name">{user.name}</div>
              <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)" }}>
                ₹{Number(user.balance).toLocaleString("en-IN")}
              </div>
            </div>
          </div>
        )}
        <nav>
          <NavLink to="/dashboard" className={({ isActive }) => (isActive ? "active" : "")}>
            Dashboard
          </NavLink>
          <NavLink to="/order" className={({ isActive }) => (isActive ? "active" : "")}>
            New order
          </NavLink>
          <a className="external" href="/architecture" target="_blank" rel="noreferrer">
            How it works ↗
          </a>
        </nav>
        <div className="spacer" />
        <button className="btn-ghost" onClick={logout} style={{ textAlign: "left" }}>
          Log out
        </button>
      </aside>
      <main className="main">{children}</main>
    </div>
  );
}
