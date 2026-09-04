import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api.js";
import { useAuth } from "../AuthContext.jsx";
import Layout from "../Layout.jsx";

function timeAgo(iso) {
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

export default function Dashboard() {
  const { user, token, refresh } = useAuth();
  const [orders, setOrders] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    refresh();
    api
      .orderHistory(token)
      .then(setOrders)
      .finally(() => setLoading(false));
  }, [token]);

  if (!user) return null;

  return (
    <Layout>
      <div className="page-header">
        <div>
          <h1>Welcome back, {user.name.split(" ")[0]}</h1>
          <p>Every order below ran through the real SANKALP clearing pipeline — nothing here is scripted.</p>
        </div>
        <Link to="/order">
          <button className="btn-primary">+ New order</button>
        </Link>
      </div>

      <div className="wallet-row">
        <div className="wallet-tile accent">
          <div className="label">Wallet balance</div>
          <div className="value">₹{Number(user.balance).toLocaleString("en-IN")}</div>
        </div>
        <div className="wallet-tile">
          <div className="label">Spent today</div>
          <div className="value">₹{Number(user.spent_today).toLocaleString("en-IN")}</div>
        </div>
        <div className="wallet-tile">
          <div className="label">Available under daily limit</div>
          <div className="value">₹{Number(user.available_today).toLocaleString("en-IN")}</div>
        </div>
        <div className="wallet-tile">
          <div className="label">Daily limit</div>
          <div className="value">₹{Number(user.daily_limit).toLocaleString("en-IN")}</div>
        </div>
      </div>

      <div className="section-title">Recent orders</div>
      {loading ? (
        <div className="empty-state">Loading…</div>
      ) : orders.length === 0 ? (
        <div className="empty-state">No orders yet. Place your first order to see a live clearing decision.</div>
      ) : (
        orders.map((o) => (
          <div className="order-row" key={o.order_id}>
            <div className="items">
              <div className="merchant">{o.merchant}</div>
              <div className="meta">
                {o.items.map((i) => `${i.quantity}× ${i.name}`).join(", ")} · {timeAgo(o.created_at)}
              </div>
            </div>
            <span className={`badge badge-${o.effective_action}`}>{o.effective_action.replace(/_/g, " ")}</span>
            <div className="amount">₹{o.total}</div>
          </div>
        ))
      )}
    </Layout>
  );
}
