"""CU Teller: a deliberately legacy-style credit-union back office.

Framesets, table layouts, labels in neighbouring cells, generic field names,
no ids or test hooks. Two variants stand in for two tenants running the same
vendor product at different versions and branding.
"""

import itertools
import os
import re
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import quote

from flask import Flask, abort, jsonify, redirect, render_template, request, session

from mockbank.data import MIN_OPENING_DEPOSIT, SHARE_TYPES, Share, seed_members
from mockbank.faults import FaultState

IDLE_TIMEOUT_SECONDS = 15 * 60
MEMBER_NUMBER_RE = re.compile(r"\d{6}")
DEPOSIT_RE = re.compile(r"\d{1,7}(\.\d{2})?")


@dataclass(frozen=True)
class Variant:
    institution: str
    product_version: str
    member_label: str
    inquiry_nav: str
    header_bg: str
    table_bg: str
    show_available: bool
    nav_order: tuple[str, ...]


VARIANTS = {
    "a": Variant(
        institution="Alpha Community Credit Union",
        product_version="7.2.4",
        member_label="Member Number",
        inquiry_nav="Member Inquiry",
        header_bg="#003366",
        table_bg="#D4D0C8",
        show_available=False,
        nav_order=("home", "inquiry", "reports", "signoff"),
    ),
    "b": Variant(
        institution="Riverbend Federal Credit Union",
        product_version="7.3.1",
        member_label="Account No.",
        inquiry_nav="Account Inquiry",
        header_bg="#5A1E0A",
        table_bg="#E8E2D0",
        show_available=True,
        nav_order=("inquiry", "home", "reports", "signoff"),
    ),
}


def create_app(variant: str = "a", credentials: tuple[str, str] | None = None) -> Flask:
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant: {variant}")
    app = Flask(__name__)
    app.secret_key = os.environ.get("MOCKBANK_SECRET_KEY") or secrets.token_hex(16)
    username, password = credentials or (
        os.environ.get("CUA_SECRET_TELLER_USERNAME", "teller01"),
        os.environ.get("CUA_SECRET_TELLER_PASSWORD", "change-me-local-only"),
    )
    v = VARIANTS[variant]
    members = seed_members()
    faults = FaultState()
    confirmation_numbers = itertools.count(480121)
    app.extensions["faults"] = faults
    app.extensions["members"] = members

    @app.context_processor
    def _chrome() -> dict:
        return {"v": v}

    def teller_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            now = time.time()
            last_seen = session.get("last_seen", 0)
            expired = now - last_seen > IDLE_TIMEOUT_SECONDS or faults.consume("session_expired")
            if not session.get("user") or expired:
                session.clear()
                return render_template("session_expired.html")
            session["last_seen"] = now
            return view(*args, **kwargs)

        return wrapper

    def notice_interstitial():
        if faults.consume("maintenance_notice"):
            return redirect(f"/teller/notice?next={quote(request.full_path.rstrip('?'))}")
        return None

    @app.get("/")
    def root():
        return redirect("/teller" if session.get("user") else "/login")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            if request.form.get("f1") == username and request.form.get("f2") == password:
                session.clear()
                session["user"] = username
                session["last_seen"] = time.time()
                return redirect("/teller")
            error = "ERR-001 INVALID OPERATOR ID OR PASSWORD"
        return render_template("login.html", error=error)

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    @app.get("/teller")
    @teller_required
    def frameset():
        return render_template("frameset.html")

    @app.get("/teller/nav")
    @teller_required
    def nav():
        return render_template("nav.html")

    @app.get("/teller/home")
    @teller_required
    def home():
        return render_template("home.html", operator=session["user"])

    @app.get("/teller/reports")
    @teller_required
    def reports():
        return render_template("reports.html")

    @app.get("/teller/notice")
    @teller_required
    def notice():
        next_url = request.args.get("next", "/teller/home")
        if not next_url.startswith("/teller/"):
            next_url = "/teller/home"
        return render_template("notice.html", next_url=next_url)

    @app.route("/teller/inquiry", methods=["GET", "POST"])
    @teller_required
    def inquiry():
        if request.method == "GET":
            return notice_interstitial() or render_template("inquiry.html")
        number = request.form.get("f3", "").strip()
        if not MEMBER_NUMBER_RE.fullmatch(number):
            return render_template(
                "inquiry.html", error=f"ERR-102 {v.member_label.upper()} MUST BE 6 DIGITS"
            )
        if number not in members:
            return render_template("inquiry.html", error="ERR-404 MEMBER NOT ON FILE")
        return redirect(f"/teller/member/{number}")

    @app.get("/teller/member/<number>")
    @teller_required
    def member_detail(number: str):
        member = members.get(number)
        if member is None:
            return render_template("inquiry.html", error="ERR-404 MEMBER NOT ON FILE")
        if member.status == "RESTRICTED":
            return render_template("access_denied.html")
        interstitial = notice_interstitial()
        if interstitial:
            return interstitial
        if faults.slow_load_seconds:
            time.sleep(faults.slow_load_seconds)
        if faults.server_error:
            return render_template("server_error.html"), 500
        return render_template(
            "member.html", member=member, show_dialog=faults.consume("unknown_dialog")
        )

    def open_share_guard(number: str):
        member = members.get(number)
        if member is None:
            return None, render_template("inquiry.html", error="ERR-404 MEMBER NOT ON FILE")
        if member.status == "RESTRICTED":
            return None, render_template("access_denied.html")
        return member, None

    def validate_open_share(form) -> tuple[str | None, str, Decimal | None]:
        share_type = form.get("f7", "")
        raw = form.get("f8", "").strip().replace(",", "")
        if share_type not in SHARE_TYPES:
            return "ERR-209 SELECT A SHARE TYPE", share_type, None
        if not DEPOSIT_RE.fullmatch(raw):
            return "ERR-210 INVALID DEPOSIT AMOUNT", share_type, None
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            return "ERR-210 INVALID DEPOSIT AMOUNT", share_type, None
        if amount < MIN_OPENING_DEPOSIT:
            return f"ERR-211 MINIMUM OPENING DEPOSIT IS ${MIN_OPENING_DEPOSIT}", share_type, None
        return None, share_type, amount

    @app.route("/teller/member/<number>/open-share", methods=["GET", "POST"])
    @teller_required
    def open_share(number: str):
        member, blocked = open_share_guard(number)
        if blocked:
            return blocked
        if request.method == "GET":
            return render_template("open_share.html", member=member, share_types=SHARE_TYPES)
        error, share_type, amount = validate_open_share(request.form)
        if error:
            return render_template(
                "open_share.html", member=member, share_types=SHARE_TYPES, error=error
            )
        return render_template(
            "open_share_review.html",
            member=member,
            share_type=share_type,
            share_name=SHARE_TYPES[share_type],
            amount=amount,
        )

    @app.post("/teller/member/<number>/open-share/commit")
    @teller_required
    def open_share_commit(number: str):
        member, blocked = open_share_guard(number)
        if blocked:
            return blocked
        error, share_type, amount = validate_open_share(request.form)
        if error:
            return render_template(
                "open_share.html", member=member, share_types=SHARE_TYPES, error=error
            )
        # Deliberately not idempotent: a double submit opens two shares, as many cores do.
        share_id = f"S{len(member.shares) + 20:02d}"
        member.shares.append(Share(share_id, SHARE_TYPES[share_type], amount, amount))
        return render_template(
            "open_share_done.html",
            member=member,
            share_id=share_id,
            share_name=SHARE_TYPES[share_type],
            amount=amount,
            confirmation=f"CNF-{next(confirmation_numbers)}",
        )

    @app.route("/__admin/faults", methods=["GET", "POST"])
    def admin_faults():
        if request.remote_addr not in {"127.0.0.1", "::1"}:
            abort(403)
        if request.method == "POST":
            body = request.get_json(force=True) or {}
            if body.get("clear"):
                faults.clear()
            else:
                try:
                    faults.set(body.get("name", ""), body.get("value"))
                except ValueError as exc:
                    return jsonify(error=str(exc)), 400
        return jsonify(faults.snapshot())

    @app.errorhandler(404)
    def not_found(_):
        return render_template("server_error.html", code="404 PAGE NOT FOUND"), 404

    @app.template_filter("money")
    def money(value: Decimal) -> str:
        return f"${value:,.2f}"

    return app
