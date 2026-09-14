"""Hand-authored capability used to exercise schema and replay before discovery exists."""

from cua.schema.artifact import Capability

MAIN = ["main"]


def balance_lookup_dict() -> dict:
    member_number_box = {
        "frame_path": MAIN,
        "locators": [
            {"locator": {"kind": "label_neighbor", "role": "textbox", "label": "Member Number"},
             "rationale": "Caption in neighbouring cell."},
        ],
    }
    return {
        "schema_version": "1.0",
        "id": "member.savings_balance.lookup",
        "version": "1.0.0",
        "status": "draft",
        "title": "Look up a member's savings balance",
        "description": "Searches a member by number and returns the Regular Savings share balance.",
        "app": {"product": "cu_teller", "versions": ">=7.2,<8"},
        "entry": {"path": "/teller"},
        "inputs": {
            "member_number": {"type": "string", "description": "6-digit member number",
                              "pattern": r"^\d{6}$", "sensitivity": "pii"},
        },
        "outputs": {
            "savings_balance": {"type": "money", "description": "Regular Savings balance",
                                "currency": "USD", "sensitivity": "financial"},
        },
        "steps": [
            {
                "id": "s01_open_member_inquiry",
                "intent": "Open Member Inquiry from the main menu",
                "action": {"kind": "click"},
                "target": {"frame_path": ["nav"], "locators": [
                    {"locator": {"kind": "role_name", "role": "link", "name": "Member Inquiry"},
                     "rationale": "Menu link by role and name."}]},
                "effect": "navigation",
                "post": [
                    {"kind": "frame_url", "frame_path": MAIN, "path": "/teller/inquiry"},
                    {"kind": "element_present", "target": member_number_box},
                ],
            },
            {
                "id": "s02_fill_member_number",
                "intent": "Enter the member number",
                "action": {"kind": "fill", "value": {"input": "member_number"}},
                "target": member_number_box,
                "effect": "none",
            },
            {
                "id": "s03_submit_search",
                "intent": "Submit the search",
                "action": {"kind": "click"},
                "target": {"frame_path": MAIN, "locators": [
                    {"locator": {"kind": "role_name", "role": "button", "name": "Search"},
                     "rationale": "Button by role and name."}]},
                "effect": "navigation",
                "post": [{"kind": "frame_url", "frame_path": MAIN, "path": "/teller/member/{{inputs.member_number}}"}],
                "handlers": [
                    {"code": "member_not_found", "kind": "business_outcome", "message": "No member with that number.",
                     "when": [{"kind": "text_visible", "text": "ERR-404 MEMBER NOT ON FILE", "frame_path": MAIN}]},
                    {"code": "access_denied", "kind": "business_outcome",
                     "message": "Operator is not authorized for this member.",
                     "when": [{"kind": "text_visible", "text": "SEC-7 ACCESS DENIED", "frame_path": MAIN}]},
                ],
            },
            {
                "id": "s04_read_savings_balance",
                "intent": "Read the Regular Savings balance",
                "action": {"kind": "extract", "output": "savings_balance"},
                "target": {"frame_path": MAIN, "locators": [
                    {"locator": {"kind": "table_cell", "column": "Balance",
                                 "row": {"column": "Description", "equals": "Regular Savings"}},
                     "rationale": "Column header plus row key."}]},
                "effect": "none",
            },
        ],
        "success": [
            {"kind": "frame_url", "frame_path": MAIN, "path": "/teller/member/{{inputs.member_number}}"},
            {"kind": "text_visible", "text": "SHARES", "frame_path": MAIN},
        ],
        "provenance": {"method": "hand_authored", "recorded_at": "2026-09-13T00:00:00Z", "recorder_version": "test"},
    }


def balance_lookup() -> Capability:
    return Capability.model_validate(balance_lookup_dict())
