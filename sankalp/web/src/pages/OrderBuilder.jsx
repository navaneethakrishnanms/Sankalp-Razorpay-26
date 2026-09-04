import React, { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api.js";
import { useAuth } from "../AuthContext.jsx";
import Layout from "../Layout.jsx";
import DecisionResult from "../components/DecisionResult.jsx";

function emptyForm() {
  return {
    merchantId: null,
    qty: {},                 // itemName -> quantity
    excludedIngredients: [],
    vegOnly: false,
    noDessert: false,
    budgetCeiling: "",
    deliveryMinutes: "",
  };
}

export default function OrderBuilder() {
  const { token, refresh } = useAuth();
  const [catalogue, setCatalogue] = useState([]);
  const [form, setForm] = useState(emptyForm());
  const [result, setResult] = useState(null);
  const [lastPayload, setLastPayload] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [overriding, setOverriding] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api.catalogue().then((rows) => {
      setCatalogue(rows);
      setForm((f) => ({ ...f, merchantId: rows[0]?.id ?? null }));
    });
  }, []);

  const merchant = catalogue.find((m) => m.id === form.merchantId);

  const ingredientOptions = useMemo(() => {
    if (!merchant) return [];
    const set = new Set();
    merchant.items.forEach((it) => it.ingredients.forEach((ing) => set.add(ing)));
    return [...set].sort();
  }, [merchant]);

  const lineItems = useMemo(() => {
    if (!merchant) return [];
    return Object.entries(form.qty)
      .filter(([, q]) => q > 0)
      .map(([name, q]) => {
        const it = merchant.items.find((i) => i.name === name);
        return { name, quantity: q, unit_price: Number(it.unit_price) };
      });
  }, [form.qty, merchant]);

  const total = lineItems.reduce((s, i) => s + i.unit_price * i.quantity, 0);

  function setQty(name, delta) {
    setForm((f) => {
      const next = Math.max(0, (f.qty[name] ?? 0) + delta);
      return { ...f, qty: { ...f.qty, [name]: next } };
    });
  }

  function switchMerchant(id) {
    setForm({ ...emptyForm(), merchantId: id });
  }

  function toggleIngredient(ing) {
    setForm((f) => ({
      ...f,
      excludedIngredients: f.excludedIngredients.includes(ing)
        ? f.excludedIngredients.filter((x) => x !== ing)
        : [...f.excludedIngredients, ing],
    }));
  }

  async function submit(overridePayload) {
    setError("");
    setSubmitting(!overridePayload);
    const payload = overridePayload ?? {
      merchant_id: form.merchantId,
      items: lineItems.map(({ name, quantity }) => ({ name, quantity })),
      excluded_ingredients: form.excludedIngredients,
      veg_only: form.vegOnly,
      no_dessert: form.noDessert,
      budget_ceiling: form.budgetCeiling || null,
      delivery_minutes: form.deliveryMinutes || null,
    };
    try {
      const resp = await api.placeOrder(token, payload);
      setResult(resp);
      setLastPayload(payload);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not place order.");
    } finally {
      setSubmitting(false);
      setOverriding(false);
    }
  }

  async function handleOverride(confirm) {
    if (!confirm) {
      setResult(null);
      setForm(emptyForm());
      return;
    }
    setOverriding(true);
    await submit({ ...lastPayload, confirm_override: true });
  }

  function placeAnother() {
    setResult(null);
    setForm(emptyForm());
  }

  if (result) {
    return (
      <Layout>
        <div className="page-header">
          <div>
            <h1>Order decision</h1>
            <p>
              {result.order.merchant} · {result.order.items.map((i) => `${i.quantity}× ${i.name}`).join(", ")}
            </p>
          </div>
          <button className="btn-secondary" onClick={placeAnother}>
            Place another order
          </button>
        </div>
        <DecisionResult order={result.order} onOverride={handleOverride} overriding={overriding} />
      </Layout>
    );
  }

  return (
    <Layout>
      <div className="page-header">
        <div>
          <h1>Place an order</h1>
          <p>Pick items, set what matters to you, and SANKALP decides whether it clears — live.</p>
        </div>
      </div>

      <div className="merchant-tabs">
        {catalogue.map((m) => (
          <button
            key={m.id}
            className={`merchant-tab ${m.id === form.merchantId ? "active" : ""}`}
            onClick={() => switchMerchant(m.id)}
          >
            {m.name}
          </button>
        ))}
      </div>

      <div className="builder-grid">
        <div>
          <div className="item-grid">
            {merchant?.items.map((it) => (
              <div className="item-tile" key={it.name}>
                <div className="item-name">{it.name}</div>
                <div className="item-price mono">₹{it.unit_price}</div>
                <div className="item-tags">
                  {it.category && <span className="tag">{it.category}</span>}
                </div>
                <div className="qty-row">
                  <button className="qty-btn" onClick={() => setQty(it.name, -1)}>
                    −
                  </button>
                  <span className="qty-value">{form.qty[it.name] ?? 0}</span>
                  <button className="qty-btn" onClick={() => setQty(it.name, 1)}>
                    +
                  </button>
                </div>
              </div>
            ))}
          </div>

          <div className="section-title">Hard requirements</div>
          <div className="card">
            <label>Budget ceiling (₹, optional)</label>
            <input
              type="number"
              min="0"
              placeholder="e.g. 500"
              value={form.budgetCeiling}
              onChange={(e) => setForm((f) => ({ ...f, budgetCeiling: e.target.value }))}
              style={{ marginBottom: 14 }}
            />
            <label>Deliver within (minutes, optional)</label>
            <input
              type="number"
              min="1"
              placeholder="e.g. 30"
              value={form.deliveryMinutes}
              onChange={(e) => setForm((f) => ({ ...f, deliveryMinutes: e.target.value }))}
              style={{ marginBottom: 14 }}
            />
            <label>Exclude ingredients</label>
            <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 14px" }}>
              {ingredientOptions.map((ing) => (
                <div className="checkrow" key={ing}>
                  <input
                    type="checkbox"
                    id={`ing-${ing}`}
                    checked={form.excludedIngredients.includes(ing)}
                    onChange={() => toggleIngredient(ing)}
                  />
                  <label htmlFor={`ing-${ing}`} style={{ margin: 0, textTransform: "capitalize" }}>
                    {ing}
                  </label>
                </div>
              ))}
            </div>
          </div>

          <div className="section-title">Soft preferences</div>
          <div className="card">
            <div className="checkrow">
              <input
                type="checkbox"
                id="veg-only"
                checked={form.vegOnly}
                onChange={(e) => setForm((f) => ({ ...f, vegOnly: e.target.checked }))}
              />
              <label htmlFor="veg-only" style={{ margin: 0 }}>
                Prefer vegetarian only
              </label>
            </div>
            <div className="checkrow">
              <input
                type="checkbox"
                id="no-dessert"
                checked={form.noDessert}
                onChange={(e) => setForm((f) => ({ ...f, noDessert: e.target.checked }))}
              />
              <label htmlFor="no-dessert" style={{ margin: 0 }}>
                Prefer no dessert
              </label>
            </div>
            <div className="note-box" style={{ marginTop: 12, marginBottom: 0 }}>
              Soft preferences never block alone. Violate <em>two at once</em> and SANKALP genuinely routes
              to CLARIFY instead of guessing — try it.
            </div>
          </div>
        </div>

        <div className="card summary-card">
          <h3 style={{ marginBottom: 14 }}>Order summary</h3>
          {lineItems.length === 0 ? (
            <div className="empty-state" style={{ padding: 16 }}>Add items to see your total.</div>
          ) : (
            lineItems.map((i) => (
              <div className="summary-line" key={i.name}>
                <span>
                  {i.quantity}× {i.name}
                </span>
                <span className="amt">₹{(i.unit_price * i.quantity).toFixed(2)}</span>
              </div>
            ))
          )}
          <div className="summary-line total">
            <span>Total</span>
            <span className="amt">₹{total.toFixed(2)}</span>
          </div>
          {error && <div className="error-text" style={{ marginTop: 10 }}>{error}</div>}
          <button
            className="btn-primary"
            style={{ width: "100%", marginTop: 16 }}
            disabled={lineItems.length === 0 || submitting}
            onClick={() => submit()}
          >
            {submitting ? "Checking with SANKALP…" : "Place order"}
          </button>
        </div>
      </div>
    </Layout>
  );
}
