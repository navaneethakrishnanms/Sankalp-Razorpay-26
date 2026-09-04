import React, { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api.js";
import { TestModeBadge } from "../Layout.jsx";

export default function UserPicker() {
  const [users, setUsers] = useState([]);
  const navigate = useNavigate();

  useEffect(() => {
    api.listUsers().then(setUsers).catch(() => setUsers([]));
  }, []);

  return (
    <div className="centered-page">
      <TestModeBadge />
      <div className="hero-title">
        <h1>
          SAN<span style={{ color: "var(--brass)" }}>KALP</span> Wallet
        </h1>
        <p>Pick a demo account to sign in as.</p>
      </div>
      <div className="user-grid">
        {users.map((u) => (
          <div key={u.id} className="user-card" onClick={() => navigate(`/login/${u.id}`)}>
            <span className="avatar">{u.avatar}</span>
            <div className="name">{u.name}</div>
            <div className="hint">demo password: {u.demo_password}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
