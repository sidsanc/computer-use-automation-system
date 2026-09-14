from pathlib import Path

import pytest

from cua.config import Workspace
from cua.paths import path_matches
from cua.policy.gate import ActionContext, PolicyNarrowing, PolicyWideningError, evaluate

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:5001"


@pytest.fixture(scope="module")
def policy():
    ws = Workspace(ROOT / "config")
    return ws.policy_for(ws.tenant("cu_alpha"))


def ctx(**kw) -> ActionContext:
    defaults = {"action": "click", "mode": "replay", "location_url": f"{BASE}/teller/inquiry"}
    return ActionContext(**(defaults | kw))


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("/teller/**", "/teller", True),
        ("/teller/**", "/teller/member/100234", True),
        ("/teller/member/*/open-share", "/teller/member/100234/open-share", True),
        ("/teller/member/*/open-share", "/teller/member/100234/open-share/commit", False),
        ("/teller/member/*", "/teller/member/100234?x=1", True),
        ("/login", "/__admin/faults", False),
    ],
)
def test_path_patterns(pattern, path, expected):
    assert path_matches(pattern, path) is expected


def test_search_post_is_navigation_not_write(policy):
    search = ctx(destination_url=f"{BASE}/teller/inquiry", destination_method="POST", control_name="Search")
    d = evaluate(policy, search)
    assert (d.verdict, d.risk) == ("allow", "navigation")


def test_unlisted_post_is_conservatively_a_write(policy):
    d = evaluate(policy, ctx(destination_url=f"{BASE}/teller/member/100234/notes", destination_method="POST"))
    assert (d.verdict, d.risk) == ("allow", "write")


def test_action_outside_allowlist_is_blocked(policy):
    narrowed = policy.narrowed(PolicyNarrowing(allowed_actions=("click", "extract")))
    assert evaluate(narrowed, ctx(action="fill")).rule == "action_not_allowed"


@pytest.mark.parametrize(
    "kw",
    [
        {"location_url": "https://evil.example/teller/inquiry"},
        {"location_url": f"{BASE}/__admin/faults"},
        {"destination_url": "https://evil.example/phish", "destination_method": "GET"},
    ],
)
def test_locations_and_destinations_outside_allowlist_are_blocked(policy, kw):
    assert evaluate(policy, ctx(**kw)).verdict == "block"


COMMIT = {"destination_url": f"{BASE}/teller/member/100871/open-share/commit", "destination_method": "POST",
          "control_name": "Confirm"}


def test_irreversible_in_discovery_needs_a_human(policy):
    d = evaluate(policy, ctx(mode="discovery", **COMMIT))
    assert (d.verdict, d.risk, d.rule) == ("require_approval", "irreversible", "irreversible_needs_human")


def test_irreversible_unattended_replay_is_blocked(policy):
    assert evaluate(policy, ctx(**COMMIT)).rule == "irreversible_unattended"
    # Approval alone is not enough; the caller must also opt in for this invocation.
    assert evaluate(policy, ctx(capability_approved=True, **COMMIT)).verdict == "block"


def test_irreversible_attended_replay_needs_approval_unless_preapproved(policy):
    assert evaluate(policy, ctx(attended=True, **COMMIT)).verdict == "require_approval"
    d = evaluate(policy, ctx(capability_approved=True, allow_irreversible=True, **COMMIT))
    assert (d.verdict, d.rule) == ("allow", "irreversible_preapproved")


def test_commit_like_control_name_is_irreversible_even_on_unlisted_route(policy):
    d = evaluate(policy, ctx(destination_url=f"{BASE}/teller/member/1/wire", destination_method="POST",
                             control_name="Transfer Funds"))
    assert d.risk == "irreversible"


def test_tenant_policy_can_narrow_but_never_widen(policy):
    assert policy.narrowed(PolicyNarrowing(max_steps=10)).max_steps == 10
    with pytest.raises(PolicyWideningError, match="allowed_paths"):
        policy.narrowed(PolicyNarrowing(allowed_paths=("/teller/**", "/__admin/**")))
    with pytest.raises(PolicyWideningError, match="max_steps"):
        policy.narrowed(PolicyNarrowing(max_steps=500))
    stricter = policy.narrowed(PolicyNarrowing(extra_irreversible_paths=("/teller/member/*/open-share",)))
    review = ctx(destination_url=f"{BASE}/teller/member/1/open-share", destination_method="POST")
    assert evaluate(stricter, review).risk == "irreversible"


def test_beta_tenant_policy_is_bound_to_its_own_origin():
    ws = Workspace(ROOT / "config")
    beta = ws.policy_for(ws.tenant("cu_beta"))
    assert beta.allowed_origins == ("http://127.0.0.1:5002",) and beta.max_steps == 20
    assert evaluate(beta, ctx()).rule == "location_not_allowed"
