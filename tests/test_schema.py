import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from cua.schema.artifact import Capability, OutputSpec, literal_leaks
from cua.schema.results import InvalidInputError, RunResult, validate_inputs
from cua.schema_export import export_schemas
from tests.samples import balance_lookup, balance_lookup_dict

ROOT = Path(__file__).resolve().parents[1]


def invalid(mutate) -> str:
    data = balance_lookup_dict()
    mutate(data)
    with pytest.raises(ValidationError) as err:
        Capability.model_validate(data)
    return str(err.value)


def step(data, step_id):
    return next(s for s in data["steps"] if s["id"] == step_id)


def test_sample_roundtrips_through_json():
    cap = balance_lookup()
    assert Capability.model_validate_json(cap.model_dump_json()) == cap


def test_contract_exposes_typed_io_risk_and_business_outcomes():
    contract = balance_lookup().contract()
    assert contract["risk"] == "read" and contract["requires_human"] is False
    assert contract["inputs"]["properties"]["member_number"]["pattern"] == r"^\d{6}$"
    assert contract["outputs"]["properties"]["savings_balance"]["type"] == "object"
    assert set(contract["business_outcomes"]) == {"member_not_found", "access_denied"}


def test_unknown_fields_are_rejected():
    assert "extra" in invalid(lambda d: d.update(notes="x")).lower()


def test_template_must_reference_declared_input():
    assert "unknown input 'member_id'" in invalid(
        lambda d: step(d, "s03_submit_search")["post"][0].update(path="/teller/member/{{inputs.member_id}}")
    )


def test_every_output_extracted_exactly_once():
    assert "must be extracted by exactly one step" in invalid(
        lambda d: d["outputs"].update(member_name={"type": "string", "description": "x"})
    )
    extract = lambda d: step(d, "s04_read_savings_balance")["action"]  # noqa: E731
    assert "undeclared output" in invalid(lambda d: extract(d).update(output="other"))


def test_navigation_step_needs_postconditions():
    assert "must declare postconditions" in invalid(lambda d: step(d, "s03_submit_search").update(post=[]))


def test_only_write_steps_can_be_irreversible():
    assert "only write steps" in invalid(lambda d: step(d, "s03_submit_search").update(irreversible=True))


def test_target_required_by_action_kind():
    assert "requires a target" in invalid(lambda d: step(d, "s02_fill_member_number").update(target=None))


def test_recoverable_handlers_need_a_recovery_and_others_must_not_have_one():
    handler = lambda d: step(d, "s03_submit_search")["handlers"][0]  # noqa: E731
    assert "recovery is required" in invalid(lambda d: handler(d).update(kind="recoverable"))
    assert "recovery is required" in invalid(lambda d: handler(d).update(recovery={"kind": "recheck"}))


def test_secrets_must_be_declared():
    assert "undeclared secret" in invalid(
        lambda d: step(d, "s02_fill_member_number")["action"].update(value={"secret": "teller_password"})
    )


def test_literal_leaks_finds_frozen_discovery_values():
    assert literal_leaks(balance_lookup(), ["100234", "$4,182.55"]) == []
    leaked = balance_lookup_dict()
    step(leaked, "s03_submit_search")["post"][0]["path"] = "/teller/member/100234"
    assert literal_leaks(Capability.model_validate(leaked), ["100234"]) == ["steps[2].post[0].path"]


def test_money_output_parsing():
    spec = OutputSpec(type="money", description="x", currency="USD")
    assert spec.parse(" $4,182.55 ") == {"amount": "4182.55", "currency": "USD"}
    assert spec.parse("($12.00)")["amount"] == "-12.00"
    with pytest.raises(ValueError):
        spec.parse("N/A")


def test_validate_inputs_is_a_caller_contract_check():
    cap = balance_lookup()
    assert validate_inputs(cap, {"member_number": "100234"}) == {"member_number": "100234"}
    with pytest.raises(InvalidInputError, match="does not match"):
        validate_inputs(cap, {"member_number": "12ab"})
    with pytest.raises(InvalidInputError, match="missing input"):
        validate_inputs(cap, {})


def result(**overrides) -> dict:
    base = {
        "run_id": "r1", "status": "success",
        "capability": {"id": "member.savings_balance.lookup", "version": "1.0.0", "resolved_hash": "abc"},
        "tenant": "cu_alpha", "inputs": {"member_number": "[pii]"},
        "outputs": {"savings_balance": {"amount": "1.00", "currency": "USD"}},
        "started_at": datetime.now(UTC), "duration_ms": 5, "evidence_dir": "runs/r1",
    }
    return base | overrides


def test_result_status_and_payload_must_agree():
    assert RunResult.model_validate(result()).exit_code == 0
    outcome = {"code": "member_not_found", "message": "x", "step_id": "s03_submit_search"}
    ok = RunResult.model_validate(result(status="business_outcome", outputs={}, outcome=outcome))
    assert ok.exit_code == 10
    with pytest.raises(ValidationError, match="'outcome' must be set"):
        RunResult.model_validate(result(outcome=outcome))
    with pytest.raises(ValidationError, match="outputs are only returned on success"):
        RunResult.model_validate(result(status="business_outcome", outcome=outcome))


def test_drifted_steps_reports_fallback_locators():
    r = RunResult.model_validate(result(locator_ranks={"s01": 0, "s02": 2}))
    assert r.drifted_steps == ["s02"]


def test_checked_in_json_schemas_are_current(tmp_path):
    export_schemas(tmp_path)
    for generated in tmp_path.iterdir():
        committed = ROOT / "schemas" / generated.name
        assert committed.exists(), f"run `uv run cua schema` to create {committed.name}"
        assert json.loads(committed.read_text()) == json.loads(generated.read_text()), (
            f"{committed.name} is stale; run `uv run cua schema`"
        )
