import pytest

from mockbank.app import create_app

CREDS = ("teller01", "test-pass")


@pytest.fixture
def app():
    return create_app("a", credentials=CREDS)


@pytest.fixture
def client(app):
    c = app.test_client()
    resp = c.post("/login", data={"f1": CREDS[0], "f2": CREDS[1]})
    assert resp.status_code == 302
    return c


def faults(app):
    return app.extensions["faults"]


def text(resp) -> str:
    return resp.get_data(as_text=True)


def test_unauthenticated_teller_page_shows_session_expired():
    c = create_app("a", credentials=CREDS).test_client()
    assert "SES-440 SESSION EXPIRED" in text(c.get("/teller/inquiry"))


def test_bad_login_is_rejected():
    c = create_app("a", credentials=CREDS).test_client()
    assert "ERR-001" in text(c.post("/login", data={"f1": "teller01", "f2": "nope"}))


def test_frameset_has_nav_and_main_frames(client):
    body = text(client.get("/teller"))
    assert '<frame name="nav"' in body and '<frame name="main"' in body


def test_search_found_redirects_to_detail(client):
    resp = client.post("/teller/inquiry", data={"f3": "100234"})
    assert resp.headers["Location"].endswith("/teller/member/100234")
    detail = text(client.get("/teller/member/100234"))
    assert "Regular Savings" in detail and "$4,182.55" in detail


@pytest.mark.parametrize(
    ("value", "expected"),
    [("12ab", "ERR-102"), ("999999", "ERR-404 MEMBER NOT ON FILE")],
)
def test_search_validation_and_not_found(client, value, expected):
    assert expected in text(client.post("/teller/inquiry", data={"f3": value}))


def test_restricted_member_is_access_denied(client):
    client.post("/teller/inquiry", data={"f3": "100555"})
    assert "SEC-7 ACCESS DENIED" in text(client.get("/teller/member/100555"))


def test_labels_live_in_neighbouring_cells_without_ids(client):
    body = text(client.get("/teller/inquiry"))
    assert "<label" not in body and " id=" not in body and "data-testid" not in body


def test_session_expired_fault_is_one_shot(app, client):
    faults(app).set("session_expired")
    assert "SES-440" in text(client.get("/teller/home"))
    client.post("/login", data={"f1": CREDS[0], "f2": CREDS[1]})
    assert "SES-440" not in text(client.get("/teller/home"))


def test_maintenance_notice_interstitial_then_continue(app, client):
    faults(app).set("maintenance_notice")
    resp = client.get("/teller/member/100234")
    assert resp.status_code == 302 and "/teller/notice?next=" in resp.headers["Location"]
    notice = text(client.get(resp.headers["Location"]))
    assert "SYSTEM NOTICE" in notice and 'href="/teller/member/100234"' in notice


def test_notice_rejects_offsite_next(client):
    assert 'href="/teller/home"' in text(client.get("/teller/notice?next=https://evil.example"))


def test_server_error_fault_is_sticky(app, client):
    faults(app).set("server_error")
    assert client.get("/teller/member/100234").status_code == 500
    assert client.get("/teller/member/100234").status_code == 500
    faults(app).clear()
    assert client.get("/teller/member/100234").status_code == 200


def test_unknown_dialog_fault(app, client):
    faults(app).set("unknown_dialog")
    assert 'role="dialog"' in text(client.get("/teller/member/100234"))
    assert 'role="dialog"' not in text(client.get("/teller/member/100234"))


def test_open_share_happy_path_returns_confirmation(app, client):
    form = {"f7": "05", "f8": "25.00"}
    review = text(client.post("/teller/member/100871/open-share", data=form))
    assert "Confirm" in review and "$25.00" in review
    done = text(client.post("/teller/member/100871/open-share/commit", data=form))
    assert "SHARE OPENED SUCCESSFULLY" in done and "CNF-" in done
    assert len(app.extensions["members"]["100871"].shares) == 3


@pytest.mark.parametrize(
    ("form", "expected"),
    [
        ({"f7": "", "f8": "10.00"}, "ERR-209"),
        ({"f7": "01", "f8": "ten"}, "ERR-210"),
        ({"f7": "01", "f8": "1.00"}, "ERR-211"),
    ],
)
def test_open_share_validation(client, form, expected):
    assert expected in text(client.post("/teller/member/100234/open-share", data=form))


def test_variant_b_differs_in_labels_version_and_columns():
    c = create_app("b", credentials=CREDS).test_client()
    c.post("/login", data={"f1": CREDS[0], "f2": CREDS[1]})
    detail = text(c.get("/teller/member/100234"))
    assert "Account No.:" in detail and "Available" in detail and "v7.3.1" in detail
    assert "Account Inquiry" in text(c.get("/teller/nav"))


def test_admin_faults_endpoint(app):
    c = app.test_client()
    assert c.post("/__admin/faults", json={"name": "slow_load", "value": "2"}).json["slow_load_seconds"] == 2
    assert c.post("/__admin/faults", json={"name": "bogus"}).status_code == 400
    assert c.post("/__admin/faults", json={"clear": True}).json["slow_load_seconds"] == 0
