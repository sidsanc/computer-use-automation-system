"""Seeded, entirely fictional members. No real people or accounts."""

from dataclasses import dataclass, field
from decimal import Decimal

SHARE_TYPES = {"01": "Regular Savings", "05": "Holiday Club", "20": "Money Market"}
MIN_OPENING_DEPOSIT = Decimal("5.00")


@dataclass
class Share:
    share_id: str
    description: str
    balance: Decimal
    available: Decimal


@dataclass
class Member:
    number: str
    name: str
    member_since: str
    status: str  # ACTIVE | RESTRICTED
    shares: list[Share] = field(default_factory=list)


def seed_members() -> dict[str, Member]:
    members = [
        Member(
            "100234", "AVERY, JORDAN T", "03/14/2009", "ACTIVE",
            [
                Share("S01", "Regular Savings", Decimal("4182.55"), Decimal("4157.55")),
                Share("S10", "Share Draft", Decimal("1210.08"), Decimal("1210.08")),
            ],
        ),
        Member(
            "100871", "ELLIS, MORGAN R", "11/02/2016", "ACTIVE",
            [
                Share("S01", "Regular Savings", Decimal("12940.00"), Decimal("12915.00")),
                Share("S05", "Holiday Club", Decimal("310.25"), Decimal("310.25")),
            ],
        ),
        Member(
            "100555", "ROWAN, CASEY L", "07/21/2012", "RESTRICTED",
            [Share("S01", "Regular Savings", Decimal("88.10"), Decimal("63.10"))],
        ),
    ]
    return {m.number: m for m in members}
