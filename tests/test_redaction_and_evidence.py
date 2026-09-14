import json

from cua.evidence.log import EvidenceLog
from cua.policy.redact import Redactor
from cua.surface.base import AXNode, FrameInfo, Observation, TableContext


def test_text_redaction_layers():
    r = Redactor(secrets=["hunter2-pass"], pii_values=["100234"])
    out = r.text("login hunter2-pass member 100234 ssn 123-45-6789 card 4111 1111 1111 1111")
    assert out == "login [secret] member [pii] ssn [ssn] card [card]"


def test_structured_redaction_masks_sensitive_keys_but_not_lookalikes():
    r = Redactor()
    data = r.value({"password": "x", "api_key": "y", "headers": {"Authorization": "Bearer z"},
                    "token_usage": {"input": 5}, "note": "fine"})
    assert data["password"] == data["api_key"] == data["headers"]["Authorization"] == "[redacted]"
    assert data["token_usage"] == {"input": 5} and data["note"] == "fine"


def cell(eid, row, col, name, table=1):
    return AXNode(eid=eid, frame_path=("main",), role="cell", name=name, name_source="native",
                  table=TableContext(table=table, row=row, col=col))


def test_observation_masks_values_next_to_sensitive_captions():
    obs = Observation(
        observation_id="o1", title="t", frames=[FrameInfo(path=("main",), url="http://x/teller")],
        nodes=[cell("e1", 0, 0, "Member Number:"), cell("e2", 0, 1, "100234"),
               cell("e3", 1, 0, "Name:"), cell("e4", 1, 1, "AVERY, JORDAN T"),
               cell("e5", 2, 0, "Status:"), cell("e6", 2, 1, "ACTIVE")],
    )
    masked = Redactor(pii_values=["100234"], sensitive_captions=["Name"]).observation(obs)
    names = [n.name for n in masked.nodes]
    assert names == ["Member Number:", "[pii]", "Name:", "[pii]", "Status:", "ACTIVE"]
    assert "AVERY" not in masked.render()


def test_evidence_log_redacts_every_write(tmp_path):
    r = Redactor(secrets=["s3cr3t-value"], pii_values=["100871"])
    with EvidenceLog(tmp_path, "run1", "replay", r) as log:
        log.event("step_started", step_id="s02", typed="s3cr3t-value", member="100871")
        log.write_json("result.json", {"inputs": {"member_number": "100871"}, "password": "abc"})
        log.write_text("note.txt", "member 100871")
        log.attach_masked_bytes("shot.png", b"\x89PNG", masking="playwright-mask:2")
        run_dir = log.dir
    content = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in run_dir.iterdir())
    assert "s3cr3t-value" not in content and "100871" not in content
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert [e["type"] for e in events] == ["step_started", "attachment"]
    assert events[0]["seq"] == 1 and events[1]["masking"] == "playwright-mask:2"
    assert json.loads((run_dir / "result.json").read_text())["password"] == "[redacted]"
