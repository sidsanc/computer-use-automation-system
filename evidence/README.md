# Evidence

Every directory is one run, copied verbatim from its run directory: `events.jsonl` (redacted,
append-only), `result.json` (the caller's contract, with output values masked), plus screenshots,
accessibility snapshots and traces where they were produced. `index.json` is the machine-readable
summary of the replay runs.

All data is fictional. Redaction is applied at one chokepoint before anything is written: exact
secrets, personal-data input values, values next to sensitive captions (`Name:`), amounts under
sensitive columns (`Balance`), and pattern matches (SSN, card, API key).

## Discovery: the model discovers, once

| Run | What it shows |
| --- | --- |
| `discovery_savings_balance/` | A real `claude-opus-5` run: 5 turns, ~11.5k input / 458 output tokens, 4 recorded steps, then saved as `member.savings_balance.lookup@1.0.0`. |
| `discovery_open_share_with_approval/` | A real `claude-sonnet-5` run of the irreversible flow: 10 turns, 9 recorded steps. The commit step paused for a human, who approved it in the operator console (`intervention_requested` → `intervention_resolved`), and the run was saved as `member.share_account.open@1.0.0`. |

Useful lines in `events.jsonl`: `model_turn` (what the model asked for), `policy_decision` (what the
gate allowed), `step_recorded` (what the recorder kept, and how many locators survived verification),
`capability_recorded`. Per-turn masked screenshots are in `steps/`.

```
uv run cua serve-mock --variant a --port 5001
uv run cua discover goals/member_savings_balance.yaml --tenant cu_alpha \
    -p member_number=100234 --verify-param member_number=100871
uv run cua discover goals/member_open_share.yaml --tenant cu_alpha --attended \
    -p member_number=100871 -p "share_type=05 Holiday Club" -p initial_deposit=25.00
```

## Replay: the artifact runs without a model

Regenerate all of these (they are deterministic, no API key needed):

```
uv run python scripts/make_evidence.py
```

| Run | Status (exit) | What it shows |
| --- | --- | --- |
| `replay_success_other_member/` | success (0) | Replayed for a **different member** than the recording, so no run value is baked into the artifact. Every locator matched at rank 0. |
| `replay_business_not_found/` | business_outcome (10) | "No member on file" is an answer the caller needs, with a code — not a crash. |
| `replay_business_access_denied/` | business_outcome (10) | A restricted record is a business outcome too. |
| `replay_invalid_input/` | failure/invalid_input (40) | A malformed input is the caller's contract violation, caught before the browser is touched (no `navigated` event). |
| `replay_recovered_interstitial/` | success (0) | A system-notice interstitial is dismissed within budget; `recoveries_applied` reports it, the status stays success. |
| `replay_recovered_session_expiry/` | success (0) | Session expiry re-authenticates and restarts the flow — allowed only because nothing had been written yet. |
| `replay_slow_load/` | success (0) | A 4-second screen is waited for by condition, not failed as a flake. |
| `replay_hard_failure_app_error/` | failure/app_error (40) | The core error page stops the run with step, expected vs observed, a masked screenshot, a redacted observation and `trace.zip`. |
| `replay_needs_human_unknown_dialog/` | needs_human (20) | An unmodelled dialog escalates. Unattended, the run returns the intervention request (`interventions/*.json` with a masked screenshot) instead of blocking. |
| `handoff_operator_takes_control/` | success (0) | The same session is handed to a person: `control_transitions` shows automation → paused → human → resuming → automation, `human_actions` records what they did (values masked in the page), and `resumed_after_human` shows replay continuing from the step the screen actually supports. |
| `replay_policy_blocked_irreversible/` | policy_blocked (30) | Unattended replay refuses the irreversible commit: `irreversible_unattended`. |
| `replay_irreversible_after_approval/` | success (0) | The same capability, attended and approved by an operator, commits and returns `confirmation_id`. |
| `tenant_beta_without_overlay/` | failure (40) | A second tenant running the same product, reworded. The preferred locator misses, the brittle CSS fallback matches the *wrong* link (`locator_ranks` = 1, the drift signal), and the step's own postcondition catches it instead of proceeding. |
| `tenant_beta_with_overlay/` | success (0) | The same artifact plus a tenant overlay: all locators back to rank 0, `capability.overlay = cu_beta`. |

## Reading a result

`result.json` is the contract returned to the calling agent: exactly one `status`, with
`outcome` / `failure` / `policy` / `intervention` set accordingly, plus `recoveries_applied`,
`locator_ranks` (rank > 0 means drift), `side_effects` (`committed` vs `possible`), `human_actions`
and `control_transitions`. Output values are masked in the stored copy; the caller receives them.
