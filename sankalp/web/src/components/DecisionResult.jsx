import React from "react";

const VERDICT_LABEL = {
  EXECUTE: "Order placed",
  ABORT: "Order stopped",
  CLARIFY: "Needs your OK",
  HOLD: "On hold",
  BLOCKED_BY_WALLET: "Can't afford this",
};

function VerifierCard({ v }) {
  const ok = v.verdict === "PASS";
  const isNote = v.verdict === "ABSTAIN";

  return (
    <div className={`verifier-card ${v.survived ? "" : "excluded"}`}>
      <span className={`badge badge-${ok ? "EXECUTE" : isNote ? "CLARIFY" : "ABORT"}`}>
        {ok ? "✓" : isNote ? "?" : "✕"}
      </span>
      <div>
        <div className="role">{v.title}</div>
        <div className="reasoning">{v.plain_text}</div>
        {!v.survived && (
          <div className="reasoning" style={{ color: "var(--text-faint)", marginTop: 2 }}>
            Not used to decide this order — {v.trust_note.toLowerCase()}
          </div>
        )}
      </div>
    </div>
  );
}

export default function DecisionResult({ order, onOverride, overriding }) {
  const bannerClass = order.effective_action;
  const showOverride = order.sankalp_action === "CLARIFY" && !order.overridden_by_user;

  return (
    <div>
      <div className={`decision-banner ${bannerClass}`}>
        <div className="verdict-word">{VERDICT_LABEL[bannerClass] ?? bannerClass}</div>
        <div className="detail">
          <div style={{ fontSize: 14, color: "var(--text)" }}>{order.customer_message}</div>
          {order.wallet_note && <div style={{ color: "var(--fail)", marginTop: 4 }}>{order.wallet_note}</div>}
          {order.overridden_by_user && (
            <div style={{ color: "var(--warn)", marginTop: 4 }}>
              We flagged something first — you chose to proceed anyway.
            </div>
          )}
        </div>
      </div>

      {order.debited !== "0" && (
        <div className="note-box">
          ✅ ₹{order.debited} paid from your wallet. (Test mode — no real bank or card was touched.)
        </div>
      )}

      {showOverride && (
        <div className="clarify-actions">
          <button className="btn-primary" onClick={() => onOverride(true)} disabled={overriding}>
            {overriding ? "Placing…" : "Yes, place it anyway"}
          </button>
          <button className="btn-secondary" onClick={() => onOverride(false)} disabled={overriding}>
            No, cancel
          </button>
        </div>
      )}

      <div className="section-title">Why this decision — every check we ran</div>
      <div className="verifier-mesh">
        {order.verifiers.map((v, i) => (
          <VerifierCard key={i} v={v} />
        ))}
      </div>
    </div>
  );
}
