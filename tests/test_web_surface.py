import pytest

from cua.schema.targets import Css, LabelNeighbor, RoleName, RowMatch, TableCell, Target, Text
from cua.schema.targets import LocatorCandidate as C
from cua.surface.base import AmbiguousLocatorError, LocatorNotFoundError, StaleElementError
from cua.surface.web.surface import WebSurface
from tests.conftest import signed_in_page

pytestmark = pytest.mark.browser

MAIN = ("main",)


@pytest.fixture
def page_a(browser, live_a):
    live_a.faults.clear()
    page = signed_in_page(browser, live_a)
    yield page
    page.close()


def goto_main(page, live, path):
    main = page.frame(name="main")
    main.goto(f"{live.base_url}{path}")
    return main


def only(obs, role, name):
    matches = [n for n in obs.nodes if n.role == role and n.name == name]
    assert len(matches) == 1, [(n.role, n.name) for n in obs.nodes]
    return matches[0]


def test_observation_spans_frames_and_names_unlabelled_textbox(page_a, live_a):
    goto_main(page_a, live_a, "/teller/inquiry")
    obs = WebSurface(page_a).observe()
    assert {f.path for f in obs.frames} == {(), ("nav",), ("main",)}
    assert only(obs, "link", "Member Inquiry").frame_path == ("nav",)
    textbox = only(obs, "textbox", "Member Number")
    assert textbox.name_source == "neighbor_label" and textbox.frame_path == MAIN
    assert 'textbox "Member Number" (named from neighbouring label)' in obs.render()


def test_password_value_is_never_observed(browser, live_a):
    page = browser.new_page()
    page.goto(f"{live_a.base_url}/login")
    page.locator("input[name=f2]").fill("super-secret")
    obs = WebSurface(page).observe()
    assert "super-secret" not in obs.model_dump_json()
    page.close()


def test_target_for_textbox_prefers_neighbor_label_and_replays(page_a, live_a):
    goto_main(page_a, live_a, "/teller/inquiry")
    surface = WebSurface(page_a)
    obs = surface.observe()
    target = surface.target_for(only(obs, "textbox", "Member Number").eid)
    assert target.frame_path == MAIN
    assert target.locators[0].locator == LabelNeighbor(role="textbox", label="Member Number")
    assert target.locators[-1].locator.kind == "css"
    surface.resolve(target).handle.fill("100234")
    assert page_a.frame(name="main").locator("input[name=f3]").input_value() == "100234"


def test_balance_cell_locator_is_value_independent(page_a, live_a):
    goto_main(page_a, live_a, "/teller/member/100234")
    surface = WebSurface(page_a)
    obs = surface.observe()
    target = surface.target_for(only(obs, "cell", "$4,182.55").eid)
    first = target.locators[0].locator
    assert first == TableCell(column="Balance", row=RowMatch(column="Share ID", equals="S01"))
    assert all(c.locator.kind != "role_name" for c in target.locators)

    goto_main(page_a, live_a, "/teller/member/100871")
    resolution = surface.resolve(target)
    assert resolution.rank == 0
    assert resolution.handle.inner_text().strip() == "$12,940.00"


def test_stale_element_ids_are_rejected(page_a, live_a):
    goto_main(page_a, live_a, "/teller/inquiry")
    surface = WebSurface(page_a)
    eid = only(surface.observe(), "button", "Search").eid
    surface.observe()
    goto_main(page_a, live_a, "/teller/home")
    with pytest.raises(StaleElementError):
        surface.element(eid)


def main_target(*locators) -> Target:
    return Target(frame_path=MAIN, locators=tuple(C(locator=loc, rationale="test") for loc in locators))


def test_ambiguous_match_is_an_error_not_first_match(page_a, live_a):
    goto_main(page_a, live_a, "/teller/member/100234")
    target = main_target(Css(selector="input[type=submit]"), RoleName(role="button", name="New Inquiry"))
    with pytest.raises(AmbiguousLocatorError) as err:
        WebSurface(page_a).resolve(target)
    assert err.value.rank == 0 and err.value.count == 2


def test_ladder_falls_through_to_next_rank_when_not_found(page_a, live_a):
    goto_main(page_a, live_a, "/teller/member/100234")
    target = main_target(RoleName(role="button", name="Does Not Exist"), Text(role="cell", text="Regular Savings"))
    assert WebSurface(page_a).resolve(target).rank == 1


def test_frames_are_still_observable_after_renavigation(page_a, live_a):
    """Playwright keeps replaced frames in child_frames; pairing with one makes every evaluate fail."""
    page_a.goto(f"{live_a.base_url}/teller", wait_until="load")
    surface = WebSurface(page_a)
    surface.settle()
    obs = surface.observe()
    assert obs.unavailable_frames == ()
    assert {f.path for f in obs.frames} == {(), ("nav",), ("main",)}
    assert only(obs, "link", "Member Inquiry").frame_path == ("nav",)


def test_dialog_is_observed(page_a, live_a):
    live_a.faults.set("unknown_dialog")
    goto_main(page_a, live_a, "/teller/member/100234")
    obs = WebSurface(page_a).observe()
    assert only(obs, "dialog", "Compliance Review").frame_path == MAIN
    assert only(obs, "button", "Acknowledge").in_dialog


def test_variant_b_label_drift_is_detected_but_columns_still_resolve(browser, live_a, live_b):
    page_a = signed_in_page(browser, live_a)
    goto_main(page_a, live_a, "/teller/inquiry")
    surface_a = WebSurface(page_a)
    derived = surface_a.target_for(only(surface_a.observe(), "textbox", "Member Number").eid)
    textbox_target = Target(frame_path=MAIN, locators=derived.locators[:1])
    goto_main(page_a, live_a, "/teller/member/100234")
    balance_target = surface_a.target_for(only(surface_a.observe(), "cell", "$4,182.55").eid)
    page_a.close()

    page_b = signed_in_page(browser, live_b)
    surface_b = WebSurface(page_b)
    goto_main(page_b, live_b, "/teller/inquiry")
    with pytest.raises(LocatorNotFoundError):
        surface_b.resolve(textbox_target)
    goto_main(page_b, live_b, "/teller/member/100234")
    assert surface_b.resolve(balance_target).handle.inner_text().strip() == "$4,182.55"
    page_b.close()
