"""The agent-facing surface: capabilities as callable tools, and one being invoked."""

import json
from pathlib import Path

import pytest

from cua.catalog import Catalog, run_agent_request
from cua.config import TenantConfig, Workspace
from cua.discovery.llm import ModelTurn, ToolCall
from cua.replay.engine import ReplayEngine, ReplayOptions
from cua.secrets import DictSecretStore
from tests.conftest import CREDS

ROOT = Path(__file__).resolve().parents[1]
SECRETS = DictSecretStore({"teller_username": CREDS[0], "teller_password": CREDS[1]})


@pytest.fixture(scope="module")
def book() -> Catalog:
    return Catalog(ROOT / "capabilities", Workspace(ROOT / "config"))


class OneCallModel:
    """A stand-in caller: calls the named tool once, then answers from the tool result."""

    model_id = "scripted-caller"

    def __init__(self, tool: str, arguments: dict) -> None:
        self.tool, self.arguments = tool, arguments
        self.seen_tools: list[dict] = []
        self.turns = 0

    def next_turn(self, system, tools, messages):
        self.seen_tools = tools
        self.turns += 1
        if self.turns == 1:
            call = ToolCall(id="c1", name=self.tool, input=self.arguments)
            return ModelTurn(content=[{"type": "tool_use", "id": "c1", "name": self.tool, "input": self.arguments}],
                             tool_calls=[call], text="", stop_reason="tool_use", model=self.model_id)
        result = json.loads(messages[-1]["content"][0]["content"])
        answer = (f"balance {result['outputs']['savings_balance']['amount']}" if result["status"] == "success"
                  else f"{result['status']}: {json.dumps(result.get('outcome') or result.get('blocked'))}")
        return ModelTurn(content=[{"type": "text", "text": answer}], tool_calls=[], text=answer,
                         stop_reason="end_turn", model=self.model_id)


def test_catalog_exposes_latest_versions_as_typed_tools(book):
    names = {e.name for e in book.entries()}
    assert names == {"member.savings_balance.lookup@1.0.0", "member.share_account.open@1.1.0"}, names

    tools = {t["name"]: t for t in book.tools()}
    lookup = tools["member_savings_balance_lookup"]
    assert lookup["input_schema"]["required"] == ["member_number"]
    assert lookup["input_schema"]["properties"]["member_number"]["pattern"] == r"^\d{6}$"
    assert "Risk: read" in lookup["description"]
    assert "member_not_found" in lookup["description"] and "access_denied" in lookup["description"]
    assert "Risk: irreversible" in tools["member_share_account_open"]["description"]


def test_capabilities_are_findable_by_id_version_or_tool_name(book):
    assert book.find("member.savings_balance.lookup").capability.version == "1.0.0"
    assert book.find("member.share_account.open@1.0.0").capability.version == "1.0.0"
    assert book.find("member_share_account_open").capability.version == "1.1.0"
    with pytest.raises(KeyError, match="no capability named"):
        book.find("member.transfer.funds")


@pytest.mark.browser
def test_an_agent_picks_a_capability_and_gets_a_typed_answer(book, browser, live_a, tmp_path):
    live_a.faults.clear()
    tenant = TenantConfig(tenant_id="cu_test", display_name="Test", app="cu_teller", base_url=live_a.base_url)
    app = Workspace(ROOT / "config").app("cu_teller")
    engine = ReplayEngine(browser, SECRETS)

    def invoke(entry, arguments):
        return engine.run(entry.capability, tenant, app, arguments,
                          ReplayOptions(evidence_root=tmp_path))

    model = OneCallModel("member_savings_balance_lookup", {"member_number": "100871"})
    outcome = run_agent_request(book, model, "What is this member's savings balance?", invoke)

    assert outcome["answer"] == "balance 12940.00"
    [invocation] = outcome["invocations"]
    assert invocation["capability"] == "member_savings_balance_lookup"
    assert invocation["result"]["outputs"]["savings_balance"] == {"amount": "12940.00", "currency": "USD"}
    assert {t["name"] for t in model.seen_tools} == {"member_savings_balance_lookup", "member_share_account_open"}


@pytest.mark.browser
def test_a_business_outcome_reaches_the_agent_as_an_answer(book, browser, live_a, tmp_path):
    live_a.faults.clear()
    tenant = TenantConfig(tenant_id="cu_test", display_name="Test", app="cu_teller", base_url=live_a.base_url)
    app = Workspace(ROOT / "config").app("cu_teller")
    engine = ReplayEngine(browser, SECRETS)

    def invoke(entry, arguments):
        return engine.run(entry.capability, tenant, app, arguments, ReplayOptions(evidence_root=tmp_path))

    model = OneCallModel("member_savings_balance_lookup", {"member_number": "999999"})
    outcome = run_agent_request(book, model, "Look up member 999999's savings balance.", invoke)

    result = outcome["invocations"][0]["result"]
    assert result["status"] == "business_outcome"
    assert result["outcome"]["code"] == "member_not_found" and "outputs" not in result
    assert "member_not_found" in outcome["answer"]


def test_unknown_capability_is_reported_to_the_agent_not_raised(book):
    model = OneCallModel("member_transfer_funds", {"amount": "10"})
    outcome = run_agent_request(book, model, "Move some money.", lambda entry, args: None)
    assert outcome["invocations"][0]["result"]["status"] == "error"
    assert "no capability named" in outcome["invocations"][0]["result"]["error"]
