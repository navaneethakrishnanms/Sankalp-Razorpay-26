"""Tests for api/bank.py — the wallet/order demo wired to the real clearing
pipeline. Covers all four real settlement actions this endpoint can reach:
EXECUTE (clean order), ABORT (hard violation), CLARIFY (two soft preferences
violated at once, via genuine inferred-weight accumulation — see the
'WHY THIS FILE BUILDS PER-CRITERION VerifierOutputs' docstring in
api/bank.py), and the wallet-layer BLOCKED_BY_WALLET case, kept distinct from
SANKALP's own verdict."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from api.bank import ORDERS, USERS
from api.main import app

client = TestClient(app)


def _login(user_id: str = "u-aarav") -> dict:
    resp = client.post("/api/bank/login", json={"user_id": user_id, "password": "sankalp123"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _reset(user_id: str) -> None:
    USERS[user_id].balance = Decimal("5000") if user_id == "u-aarav" else USERS[user_id].balance
    USERS[user_id].spent_today = Decimal("0")
    ORDERS[user_id].clear()


class TestLogin:
    def test_valid_login_returns_a_token(self):
        body = _login()
        assert "token" in body and body["user"]["id"] == "u-aarav"

    def test_wrong_password_is_rejected(self):
        resp = client.post("/api/bank/login", json={"user_id": "u-aarav", "password": "wrong"})
        assert resp.status_code == 401

    def test_session_requires_a_token(self):
        resp = client.get("/api/bank/session")
        assert resp.status_code == 401

    def test_session_returns_the_logged_in_user(self):
        token = _login()["token"]
        resp = client.get("/api/bank/session", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["id"] == "u-aarav"


class TestCatalogue:
    def test_lists_every_merchant_with_priced_items(self):
        resp = client.get("/api/bank/catalogue")
        assert resp.status_code == 200
        merchants = resp.json()
        assert any(m["id"] == "rest-biryani" for m in merchants)
        biryani = next(m for m in merchants if m["id"] == "rest-biryani")
        assert any(i["name"] == "Chicken Biryani" for i in biryani["items"])


class TestCleanOrderExecutes:
    def setup_method(self):
        _reset("u-aarav")

    def test_clean_order_executes_and_debits_the_wallet(self):
        token = _login()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        resp = client.post("/api/bank/orders", headers=headers, json={
            "merchant_id": "rest-biryani",
            "items": [{"name": "Chicken Biryani", "quantity": 1}],
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["order"]["sankalp_action"] == "EXECUTE"
        assert body["order"]["effective_action"] == "EXECUTE"
        assert body["order"]["debited"] == "280.00"
        assert body["user"]["balance"] == "4720.00"


class TestHardViolationAborts:
    def setup_method(self):
        _reset("u-aarav")

    def test_excluded_ingredient_present_aborts_with_no_debit(self):
        token = _login()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        resp = client.post("/api/bank/orders", headers=headers, json={
            "merchant_id": "rest-biryani",
            "items": [{"name": "Chicken Biryani", "quantity": 1}],
            "excluded_ingredients": ["chicken"],
        })
        body = resp.json()
        assert body["order"]["sankalp_action"] == "ABORT"
        assert body["order"]["effective_action"] == "ABORT"
        assert body["order"]["debited"] == "0"
        assert body["user"]["balance"] == "5000"

    def test_budget_breach_aborts(self):
        token = _login()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        resp = client.post("/api/bank/orders", headers=headers, json={
            "merchant_id": "rest-biryani",
            "items": [{"name": "Mutton Biryani", "quantity": 2}],
            "budget_ceiling": "100",
        })
        body = resp.json()
        assert body["order"]["sankalp_action"] == "ABORT"
        assert body["order"]["reason_code"] == "STATED_CRITERION_FAILED"


class TestSoftPreferencesRouteToClarify:
    def setup_method(self):
        _reset("u-aarav")

    def test_one_soft_violation_notes_but_executes(self):
        token = _login()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        resp = client.post("/api/bank/orders", headers=headers, json={
            "merchant_id": "rest-biryani",
            "items": [{"name": "Chicken Biryani", "quantity": 1}],
            "veg_only": True,
        })
        body = resp.json()
        assert body["order"]["sankalp_action"] == "EXECUTE"
        assert body["order"]["reason_code"] == "INFERRED_FAILURE_NOTED"

    def test_two_soft_violations_at_once_genuinely_clarify(self):
        token = _login()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        resp = client.post("/api/bank/orders", headers=headers, json={
            "merchant_id": "rest-biryani",
            "items": [{"name": "Chicken Biryani", "quantity": 1}, {"name": "Gulab Jamun", "quantity": 1}],
            "veg_only": True, "no_dessert": True,
        })
        body = resp.json()
        assert body["order"]["sankalp_action"] == "CLARIFY"
        assert body["order"]["effective_action"] == "CLARIFY"
        assert body["order"]["debited"] == "0"

    def test_confirm_override_forces_execute_after_clarify(self):
        token = _login()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        resp = client.post("/api/bank/orders", headers=headers, json={
            "merchant_id": "rest-biryani",
            "items": [{"name": "Chicken Biryani", "quantity": 1}, {"name": "Gulab Jamun", "quantity": 1}],
            "veg_only": True, "no_dessert": True, "confirm_override": True,
        })
        body = resp.json()
        assert body["order"]["sankalp_action"] == "CLARIFY"          # SANKALP's own verdict, unchanged
        assert body["order"]["effective_action"] == "EXECUTE"         # user's explicit override
        assert body["order"]["overridden_by_user"] is True
        assert Decimal(body["order"]["debited"]) > 0


class TestWalletLayerIsSeparateFromSankalp:
    def setup_method(self):
        _reset("u-kabir")

    def test_sankalp_clean_but_wallet_insufficient_blocks_without_debit(self):
        token = _login("u-kabir")["token"]
        headers = {"Authorization": f"Bearer {token}"}
        resp = client.post("/api/bank/orders", headers=headers, json={
            "merchant_id": "rest-biryani",
            "items": [{"name": "Mutton Biryani", "quantity": 6}],   # 2280, over Kabir's 2000 balance
        })
        body = resp.json()
        assert body["order"]["sankalp_action"] == "EXECUTE"           # SANKALP found nothing wrong
        assert body["order"]["effective_action"] == "BLOCKED_BY_WALLET"
        assert body["order"]["debited"] == "0"
        assert body["user"]["balance"] == "2000"


class TestOrderHistory:
    def setup_method(self):
        _reset("u-aarav")

    def test_history_reflects_placed_orders_newest_first(self):
        token = _login()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        for qty in (1, 2):
            client.post("/api/bank/orders", headers=headers, json={
                "merchant_id": "rest-biryani",
                "items": [{"name": "Raita", "quantity": qty}],
            })
        resp = client.get("/api/bank/orders", headers=headers)
        rows = resp.json()
        assert len(rows) == 2
        assert rows[0]["items"][0]["quantity"] == 2   # most recent first
