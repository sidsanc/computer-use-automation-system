# Design write-up

## 1. Architecture

Five parts, with one idea holding them together: **a single bounded action vocabulary**
(`click`, `fill`, `select`, `press_key`, `wait_for`, `extract`, `request_human`, `finish`) is
simultaneously the model's tool schema, the unit the policy gate checks, the step kind stored in an
artifact, and what a surface knows how to perform. Adding an action means touching one enum, and
"what the model may do" is by construction "what we can record, replay and police".

- **Surface** — observation and action on a live UI. The web implementation reads Chromium's own
  accessibility tree per frame (CDP), because that is the analogue of UIA/AX on desktop; the DOM is
  consulted only for what legacy markup fails to expose. The model addresses elements by ephemeral
  ids that never leave the turn.
- **Discovery** — an observe → decide → act loop, one action per turn, each action gated by policy.
- **Recorder** — turns the run into an artifact. It never trusts the transcript for *how* to find
  things: locators are derived and verified against the live element before acting, postconditions
  from what actually changed after.
- **Replay** — the production path. No model, no network to a provider; a test walks the import
  graph to prove `cua.replay` cannot even import a model client.
- **Catalogue** — capabilities as tool definitions an agent can read, plus the loop that lets it call
  one and answer from the typed result. No model code lives here, so the same catalogue serves a
  Claude tool loop, an MCP server or a plain HTTP caller.
- **Policy, redaction, evidence, handoff** — cross-cutting, and used identically by both runners.

Key decisions and trade-offs:

- **Accessibility tree over screenshots-with-coordinates.** Coordinates cannot become locators that
  survive another tenant's branding or a different resolution. Screenshots are still sent to the
  model as a second channel, and kept as evidence, but nothing is ever decided from pixels. In this
  app it was load-bearing: Chromium exposes these legacy tables with internal layout roles, and the
  member-number field has *no accessible name at all* — we derive it from the caption in the
  neighbouring cell, which is exactly what a human reads.
- **A hand-written agent loop** rather than an SDK tool runner, because every action must pass the
  gate and be recorded, and because a stale handle after a human pause has to be re-resolved.
- **Sync Playwright, single process.** The operator console runs in a thread and never touches
  Playwright; the run thread pushes it screenshots. Simple, and it keeps browser objects on one thread.
- **The app profile is the unit of vendor knowledge** — login flow, known runtime states, risky
  routes, sensitive captions and columns, version fingerprint — authored once per product and shared
  by every tenant and every capability. This is what makes a happy-path recording safe in production.

## 2. Artifact schema

A capability is a **contract**, not a script (`schemas/capability.schema.json`):

```jsonc
{ "schema_version": "1.0", "id": "member.savings_balance.lookup", "version": "1.0.0",
  "status": "draft",
  "app": { "product": "cu_teller", "versions": ">=7.2,<8" },      // bound to the vendor product
  "entry": { "path": "/teller" },
  "inputs":  { "member_number": { "type": "string", "pattern": "^\\d{6}$", "sensitivity": "pii" } },
  "outputs": { "savings_balance": { "type": "money", "currency": "USD", "sensitivity": "financial" } },
  "steps": [ { "id": "s03_submit_the_member_search", "intent": "Submit the member search",
               "action": { "kind": "click" }, "effect": "navigation", "irreversible": false,
               "target": { "frame_path": ["main"], "locators": [ /* ranked, each with a rationale */ ] },
               "post": [ { "kind": "frame_url", "frame_path": ["main"],
                           "path": "/teller/member/{{inputs.member_number}}" } ],
               "handlers": [ /* step-scoped known states */ ] } ],
  "success": [ /* conditions */ ], "handlers": [ /* capability-scoped */ ],
  "provenance": { "method": "llm_discovery", "model_id": "claude-opus-5", "step_count": 4,
                  "token_usage": {...}, "prompt_template_hash": "...", "transcript_ref": "events.jsonl" } }
```

Why it is shaped this way:

- **Bound to the product, not the tenant.** Hundreds of institutions run the same vendor software;
  re-recording per tenant does not scale. Tenants specialise through overlays (§4).
- **Locators are a ranked list, each with a rationale, and every candidate resolved to exactly one
  element at record time.** Order encodes robustness: role+name → caption in the neighbouring cell →
  a table cell addressed by column header plus a key cell in its row → text → CSS (flagged brittle).
  A table value is never addressed by its own text, which is the single most common way an artifact
  silently becomes member-specific.
- **No value from the recording run may appear anywhere in the flow.** The model types inputs *by
  reference* (`{"input": "member_number"}`) and never sees their values, literals equal to an input
  are refused mid-run, value-dependent locators are dropped, routes are canonicalised to
  `{{inputs.member_number}}`, and `literal_leaks()` rejects the save if anything slipped through. The
  input/output declarations are exempt — they are the caller's contract — except that a PII input may
  not carry its own value as an example.
- **Typed inputs and outputs** (money parses to `{amount, currency}`), and `contract()` renders the
  agent-facing view: JSON Schema in and out, risk (`read` / `write` / `irreversible`), whether a human
  is required, and the business-outcome codes the caller may receive. `cua catalog` turns that into
  tool definitions and `cua ask` shows a model choosing a capability and answering from its typed
  result — the other half of the through-line, with evidence in `evidence/agent_invocation_*`.
- **A write step may declare a duplicate guard** (`idempotency.evidence`): observable proof that the
  work already landed. It is the schema's answer to the ambiguous write (§3).
- **Validators make bad artifacts unrepresentable**: unknown input references, an output not
  extracted exactly once, a write step without a postcondition, an irreversible step that is not a
  write, a recovery on a non-recoverable handler.
- **Reviewability.** Every step has a human-readable `intent`, every locator a rationale, and
  provenance records the model, prompt hash, step count and token usage.

## 3. Determinism & error handling

Each step: settle known states → wait for preconditions → resolve the target (**unique match only**;
several matches is a hard failure, never "take the first") → policy gate → perform **once** → wait
until the postcondition holds or a handler fires.

- **Waits are condition-based.** There is no `wait(ms)` in the vocabulary; a slow screen is "not
  ready yet", bounded by a timeout, so a 4-second load is a success, not a flake.
- **Writes are never re-dispatched, and ambiguity is resolved rather than guessed.** A click that
  timed out may still have landed — in a core banking system that is *the* dangerous case, because a
  retry double-posts. Recoveries therefore go back to waiting, and the result separates
  `side_effects.committed` from `possible`. Beyond that, a write step can declare what evidence would
  prove the work landed; replay checks it before dispatching (so an already-completed write is
  skipped, not repeated) and again when the postcondition times out, converting "maybe" into
  committed, or into an explicit `ambiguous_write` failure telling the caller to reconcile before
  retrying. Both sides are in evidence: the unguarded artifact fails and reports a possible write
  that really did land; the hardened version returns the confirmation number, posting once.
- **Known states are declared data, not code.** Handlers (`when` conditions → kind) live in the
  artifact and in the app profile, and fire in fixed precedence: escalate > hard_failure >
  business_outcome > recoverable. Recoveries have attempt budgets and pass the same policy gate.
  Session expiry re-authenticates and restarts only when nothing has been written; otherwise it asks
  a human.
- **The result contract is the point.** Exactly one terminal status with its matching payload:
  `success` (+ typed outputs) · `business_outcome` (a code the caller must handle: "no such member",
  "access denied") · `needs_human` (+ intervention) · `policy_blocked` (+ rule) · `failure`
  (+ category, step, expected vs observed, evidence). **Recovery is not a status** — it is reported
  alongside, so "we dismissed a notice" can never be mistaken for an answer. A malformed caller input
  is `failure/invalid_input` before the browser is touched; the app's own validation message is a
  business outcome. Exit codes mirror this (0/10/20/30/40).
- **Drift, secondarily.** Replay records which rung of the locator ladder matched per step
  (`locator_ranks`); rank > 0 is the drift signal, and `evidence/tenant_beta_without_overlay` shows
  the mechanism working: the preferred locator misses, the brittle CSS fallback matches the *wrong*
  link, and the step's own postcondition catches it instead of proceeding.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is: a flow is expressed in roles, names, captions, table
coordinates and frame paths — never in DOM selectors. `Surface` is the only thing that knows how to
observe and act. A desktop implementation over UIA/AX would produce the same `AXNode` shape (roles
and names are the native vocabulary there) and implement the same eight actions; `frame_path`
generalises to a window/pane path. A legacy web app needs nothing new: framesets, layout tables and
unlabelled fields are what this one already is. The honest gap is a surface with no accessibility
tree at all (Citrix, canvas): that needs OCR plus visual grounding, and should be treated as
low-confidence — recorded with an explicit human step rather than pretending to be deterministic.

**Multi-tenant reuse.** A capability is bound to `{product, version range}`. A tenant may supply an
**overlay** that only *adds*: alternative locators (from a wording map, prepended so the base still
works) and extra known states. It cannot change inputs, outputs, steps, order, effects or risk — so a
reviewer reads a small diff, and a tenant cannot quietly make a write look safe. The run records
`capability.overlay` and a `resolved_hash`. Tenant *policy* is likewise intersection-only; widening
raises an error. This is built and demonstrated: one artifact serves the base tenant and a rebranded
7.3.x tenant.

**Drift management at scale.** Three signals, all already emitted: the product version fingerprint
checked against the capability's range; `locator_ranks` per run (a tenant whose rank-0 hit rate falls
is drifting); and postcondition failures grouped by step. At scale you would aggregate these per
`(product, version, tenant, capability)`, flag the artifacts whose fallbacks are carrying the run, and
re-record only those — rather than re-recording per tenant on a schedule.

## 5. Escalation & handoff

**Detect.** Replay escalates on: a handler marked `escalate`, a dialog nobody modelled, a recovery
budget exhausted, session expiry after a write, and any irreversible step without prior approval.
Discovery adds: the model calling `request_human`, no progress across turns, and repeated action
errors. The model used this for real — when a frame failed to load it waited, then escalated with a
written reason instead of guessing (that bug is fixed, but the behaviour is the point).

**Route.** An intervention request carries capability, goal, step id and intent, reason code and
reason, frame URLs, a masked screenshot and a redacted observation. Unattended runs never block:
they return `needs_human` with the request saved.

**Take control of the live session.** Control is an explicit state machine —
`automation → paused → human → resuming → automation`, illegal transitions rejected — and it is
enforced where actions happen: `Surface.perform` requires the lease, so automation physically cannot
act while a person holds it (there is a test). The operator works in *that* browser window; a banner
shows who holds control; an injected script reports what the human does, masking values in the page
before they leave the browser, and suppresses events that merely echo our own dispatched actions.

**Hand back.** On resume, replay **trusts the screen, not the step counter**: it skips any steps whose
postconditions already hold, so work the human completed is not repeated, and anything they touched is
recorded as `side_effects.committed` with `actor: human`. In discovery the manual part is recorded as a
step the capability declares a human performs, so the flow stays honest on later replays.

**Limits.** In a browser both parties can reach, a banner cannot physically lock a human out; the
guarantees are that automation never acts without the lease and that stray input is recorded. A hard
lock needs a proxied co-browse surface (CDP screencast or noVNC), which is also what remote operators
and queueing/assignment would require.

## 6. Safety

- **Allowlist, default deny.** Origins, route patterns and action types, checked before every action
  on both the current location and the destination, plus a network-level guard on the browser context
  and popups closed. The mock app's fault-control endpoint is unreachable by the agent.
- **Risk is decided by policy from where an action leads — never by the artifact.** The product
  profile lists irreversible routes and read-only POSTs (this app, like most legacy software, POSTs
  even for searches, so "POST means write" would be wrong); an unlisted POST is treated as a write,
  and a commit-like control name is treated as irreversible even on an unlisted route.
- **Irreversible actions:** always a human in discovery; approval in attended replay; **blocked**
  unattended unless the capability is `approved` *and* the caller opts in for that invocation.
  Demonstrated in evidence, blocked and approved.
- **Data.** Credentials are referenced by name, resolved from the environment at typing time, and
  never enter a model's context, an artifact or a log. One redaction chokepoint covers logs,
  artifacts, intervention requests, results and the model's own view: exact secrets, PII input values,
  values next to sensitive captions, amounts under sensitive columns, and patterns. Screenshots are
  blacked out in the page before capture; stored results keep output shapes but not values.
- **Limits, stated plainly.** Pattern redaction is best-effort; the exact-value layers are what make
  guarantees. Video, if enabled, cannot be masked — it is off by default and used only on fake data.
  A Playwright trace contains raw DOM, so it is opt-in (`--trace`). Free-text the model writes may
  contain values it legitimately read (a confirmation number is a declared public output). And the
  model does see the fake app's screen: in production this argues for a tenant-approved model,
  tokenisation of identifiers, or masking before the observation is rendered — the masking hook is
  already there.

## 7. Cuts

**Deliberately not built.** An approval/confidence workflow beyond the `draft`/`approved` flag and its
enforcement point; multi-run flakiness scoring; code generation from an artifact; assisted LLM
recovery on a failed step; queueing, multi-tenant plumbing or any horizontal scaling infrastructure —
the brief explicitly does not reward it, and the abstractions that would need it are in place.

I also considered and rejected a mutation-testing harness for locator robustness. It is the kind of
thing that looks impressive, but the brief is explicit that drift is the *secondary* concern, and it
would have graded our locator strategy against mutations we invented ourselves. The second tenant is
a more honest test of the same property, and the effort went into the ambiguous write instead —
which is where money is actually lost.

**Mocked at a clean seam, deliberately.** The operator console is a plain local page (real HTTP, real
token, real control transfer — but no auth, no assignment, no live video); evidence runs script the
operator so they are reproducible, while the human path is the same code and was exercised by hand
for the share-opening discovery. Business-outcome detectors are authored once per product rather than
discovered — a happy-path run cannot observe "not found", and pretending otherwise would be the
common mistake this brief warns about; a negative-probe discovery pass is the natural next step.

**What I would build next, in order:** (1) aggregate the drift signals we already emit into a
per-tenant health view, and let a capability reach `approved` by evidence — N clean replays — so
unattended execution is earned rather than declared; (2) negative-probe discovery to learn business
outcomes instead of authoring them; (3) a proxied co-browse surface so control transfer is enforced
rather than advisory, which also unlocks remote operators; (4) a desktop surface over UIA to prove
the abstraction, starting with a read-only flow.

**Known rough edges.** The recorder derives step ids from the model's intent text, so ids can be
truncated oddly; postcondition inference prefers a frame URL change and falls back to a newly
appeared element, which is weaker on single-page screens; and the `draft` → `approved` transition is
enforced but has no workflow behind it.
