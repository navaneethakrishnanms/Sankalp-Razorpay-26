"""
Tests for api/main.py — the demo backend.

Run these BEFORE demoing. This project's author could not execute anything
in this environment while writing api/main.py (see FAILURES.md's working
constraints), so this file is the substitute for the "cheap offline test
before the expensive/live one" pattern used everywhere else in this project
(e.g. tests/unit/test_stage5_harness.py before any live Stage 5 API call).

Every scenario here is checked against the REAL corpus and REAL clearing
pipeline — nothing in this file mocks core/. The two scripted scenarios
("fooled_judge", "semantic_caution") are checked for the specific structural
claims their scripted-ness is supposed to guarantee: that the floor changes
the ACTION (not just the survivor list), and that the response honestly
flags itself as scripted.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import SCENARIOS, app

client = TestClient(app)


class TestHealth:
    def test_health_reports_ok_and_a_nonzero_corpus(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert int(body["corpus_records"]) > 0


class TestIndex:
    def test_architecture_proof_page_serves_html(self):
        resp = client.get("/architecture")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "<html" in resp.text.lower()


class TestScenarioList:
    def test_lists_every_registered_scenario(self):
        resp = client.get("/api/scenarios")
        assert resp.status_code == 200
        ids = {row["id"] for row in resp.json()}
        assert ids == set(SCENARIOS.keys())


class TestUnknownScenario:
    def test_404_for_unregistered_id(self):
        resp = client.get("/api/clear/not-a-real-scenario")
        assert resp.status_code == 404


class TestEveryScenarioRuns:
    """Every scenario must return 200 with the shared response shape.
    This is the test that would have caught the fooled_judge population
    bug (an empty _pick() result, or a mis-shaped clear() call, both raise
    inside the scenario's run() and would show up here as a 500)."""

    def test_all_scenarios_return_200_with_the_expected_shape(self):
        for scenario_id in SCENARIOS:
            resp = client.get(f"/api/clear/{scenario_id}")
            assert resp.status_code == 200, f"{scenario_id} failed: {resp.text}"
            body = resp.json()
            for key in ("scenario", "obligation", "cart", "semantic_response_is_scripted",
                        "verifiers", "decision"):
                assert key in body, f"{scenario_id} missing {key!r}"
            assert body["decision"]["action"] in ("EXECUTE", "HOLD", "CLARIFY", "ABORT")
            assert len(body["decision"]["settlement_hash"]) == 64


class TestDeterministicScenariosAreUnscripted:
    def test_clean_and_dietary_violation_are_not_scripted(self):
        for scenario_id in ("clean", "budget_breach", "wrong_merchant",
                             "timing_miss", "total_misdeclared", "dietary_violation"):
            body = client.get(f"/api/clear/{scenario_id}").json()
            assert body["semantic_response_is_scripted"] is False
            assert body["counterfactual"] is None


class TestFooledJudgeDemonstratesFloorValue:
    """The whole point of this scenario: with no deterministic backup, the
    floor's presence must change the ACTION, not just which verifiers are
    marked as excluded. If this ever regresses to showing the same action
    both ways, the demo no longer demonstrates anything."""

    def test_is_scripted_and_labelled(self):
        body = client.get("/api/clear/fooled_judge").json()
        assert body["semantic_response_is_scripted"] is True
        assert "scripted_note" in body and len(body["scripted_note"]) > 0

    def test_floor_changes_the_settlement_action(self):
        body = client.get("/api/clear/fooled_judge").json()
        with_floor = body["decision"]["action"]
        without_floor = body["counterfactual"]["action"]
        assert with_floor != without_floor, (
            "fooled_judge must show a different action with vs. without the "
            "floor — otherwise the scenario doesn't demonstrate the floor's value"
        )
        assert without_floor == "EXECUTE", "the wrongly-cleared case must be EXECUTE"
        assert with_floor != "EXECUTE", "the floor must prevent the wrong clear"

    def test_only_one_verifier_and_it_is_excluded_with_floor_on(self):
        body = client.get("/api/clear/fooled_judge").json()
        assert len(body["verifiers"]) == 1
        assert body["verifiers"][0]["role"] == "semantic"
        assert body["verifiers"][0]["basis_class"] == "SELF"
        assert body["verifiers"][0]["survived"] is False


class TestSemanticCautionIsHonest:
    def test_is_scripted_and_labelled(self):
        body = client.get("/api/clear/semantic_caution").json()
        assert body["semantic_response_is_scripted"] is True
        assert "scripted_note" in body and len(body["scripted_note"]) > 0

    def test_semantic_verifier_abstains(self):
        body = client.get("/api/clear/semantic_caution").json()
        semantic = next(v for v in body["verifiers"] if v["role"] == "semantic")
        assert semantic["verdict"] == "ABSTAIN"
