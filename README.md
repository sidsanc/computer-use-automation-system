# Computer-Use Automation System

[![ci](https://github.com/sidsanc/computer-use-automation-system/actions/workflows/ci.yml/badge.svg)](https://github.com/sidsanc/computer-use-automation-system/actions/workflows/ci.yml)

An LLM discovers how to do a task in a legacy back-office UI **once**. The run becomes a typed,
versioned **capability artifact**. After that the artifact **replays deterministically with no model
in the loop**, behind a policy gate, with a real path to hand the live session to a human.

```
goal + contract ──► discovery (Claude drives the UI) ──► capability artifact ──► replay (no model)
                         │                                      │                      ▲      │
                    policy gate                      locators verified live            │      │
                    human approval                   no run values kept                │      ▼
                                                                          an AI agent calls   typed result:
                                                                          it by name from     success | business
                                                                          the catalogue       outcome | needs_human
                                                                                              | policy_blocked | failure
```

The target is a deliberately legacy app included in this repo (`mockbank/`): framesets, table
layouts, captions in neighbouring cells, no ids or test hooks, switchable runtime faults, and a
second rebranded variant standing in for a second tenant. All data is fictional.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync
uv run playwright install chromium
cp .env.example .env          # then edit .env
```

`.env` (gitignored):

```
ANTHROPIC_API_KEY=sk-ant-...            # discovery only; replay never reads it
CUA_SECRET_TELLER_USERNAME=teller01     # fake app credentials, referenced by name, never stored
CUA_SECRET_TELLER_PASSWORD=change-me-local-only
```

**Running without live services:** everything except `cua discover` runs offline — the whole test
suite, all of `scripts/make_evidence.py`, and every `cua replay` command below. Only discovery calls
a model.

## The quickest look

```bash
uv sync && uv run playwright install chromium
uv run cua demo --headed          # no API key needed
```

One command starts the mock app and walks every replay behaviour in turn — success on a member the
flow was not recorded with, both business outcomes, a dismissed interstitial, session expiry,
a slow screen, a hard failure, an escalation, an operator taking over the live session, an
irreversible commit refused and then approved, and one artifact running on a second tenant with and
without its overlay. `--headed` shows it happening in a real browser.
`evidence/handoff_operator_takes_control/handoff_operator_takes_control.webm` records the handoff.

## Demo path

```bash
# 1. the target app (leave running); --variant b on 5002 is the second tenant
uv run cua serve-mock --variant a --port 5001

# 2. discovery: Claude drives the live app, records a capability, then verifies it by replaying
#    the result for a DIFFERENT member, with no model involved
uv run cua discover goals/member_savings_balance.yaml --tenant cu_alpha \
    -p member_number=100234 --verify-param member_number=100871

# 3. replay the saved artifact deterministically (exit code 0)
uv run cua replay member.savings_balance.lookup@1.0.0 --tenant cu_alpha -p member_number=100871

# 4. a business outcome, not a crash (exit code 10)
uv run cua replay member.savings_balance.lookup@1.0.0 --tenant cu_alpha -p member_number=999999

# 5. inject a runtime fault, then replay again: recovered (0), hard failure (40)
uv run cua faults set maintenance_notice --port 5001
uv run cua replay member.savings_balance.lookup@1.0.0 --tenant cu_alpha -p member_number=100234
uv run cua faults set server_error --port 5001
uv run cua replay member.savings_balance.lookup@1.0.0 --tenant cu_alpha -p member_number=100234 --trace
uv run cua faults clear --port 5001

# 6. an irreversible capability is refused unattended (exit code 30)
uv run cua replay member.share_account.open@1.0.0 --tenant cu_alpha \
    -p member_number=100871 -p "share_type=05 Holiday Club" -p initial_deposit=25.00

# 7. human handoff: an unmodelled dialog pauses the run and opens an operator console
uv run cua faults set unknown_dialog --port 5001
uv run cua replay member.savings_balance.lookup@1.0.0 --tenant cu_alpha -p member_number=100234 \
    --attended --headed          # prints the console URL; take control, clear the dialog, hand back

# 8. the same artifact on a second tenant: fails loudly without its overlay, succeeds with it
uv run cua serve-mock --variant b --port 5002
uv run cua replay member.savings_balance.lookup@1.0.0 --tenant cu_beta -p member_number=100234 --no-overlay
uv run cua replay member.savings_balance.lookup@1.0.0 --tenant cu_beta -p member_number=100234

# 9. the production shape: an AI agent reads the catalogue and calls a capability by name
uv run cua catalog --tools
uv run cua ask "What is the current savings balance for member 100871?" --tenant cu_alpha
uv run cua ask "Please read out the savings balance for member 999999." --tenant cu_alpha
```

Exit codes: `0` success · `10` business outcome · `20` needs human · `30` blocked by policy · `40` failure.

Test members: `100234`, `100871` (active), `100555` (restricted), `999999` (not on file).

## Commands

| Command | Purpose |
| --- | --- |
| `cua serve-mock --variant a\|b --port N` | Run the mock legacy app. |
| `cua faults set\|clear\|show` | Inject runtime faults: `session_expired`, `maintenance_notice`, `unknown_dialog`, `slow_load`, `server_error`. |
| `cua discover GOAL.yaml --tenant T -p k=v` | LLM discovery, then a verification replay. `--attended` opens the operator console. |
| `cua replay ID@VERSION --tenant T -p k=v` | Deterministic replay. `--attended`, `--allow-irreversible`, `--trace`, `--video`, `--no-overlay`. |
| `cua catalog [--tools]` | The capabilities an agent can call, as typed contracts or tool definitions. |
| `cua ask "..." --tenant T` | Act as the calling agent: choose a capability for the request, invoke it, answer. |
| `cua demo` | The narrated tour of every replay behaviour. `--headed`, `--video`. |
| `cua schema` | Regenerate the JSON Schemas in `schemas/`. |

## Layout

```
mockbank/          legacy target app (variant a/b, fault injection)
goals/             goal + typed contract handed to discovery
capabilities/      recorded artifacts (from the real runs)
config/            app profile (product policy, login, known states), tenants, overlays
schemas/           published JSON Schema for the artifact and the run result
src/cua/
  actions.py       the one bounded action vocabulary
  schema/          artifact, conditions, targets, result contract
  surface/         accessibility-tree observation and locator resolution
  policy/          pre-action gate, redaction chokepoint
  replay/          deterministic engine
  discovery/       agent, model client, recorder
  handoff/         control lease, capture, operator console
  tenancy/         overlays
evidence/          curated runs (see evidence/README.md)
```

Read `REPORT.md` for the design, the trade-offs and what was deliberately left out.

## Tests

```bash
uv run pytest            # 94 tests; browser tests drive real Chromium against the mock app
uv run ruff check .
```

Tests worth knowing about: replay never imports a model client (enforced by walking the import
graph), an artifact must not contain values from the run that recorded it, tenant policy can only
narrow, and automation cannot act while a human holds the session.
