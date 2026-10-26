# Architecture diagrams

Eight views of the same system, from the outside in. Each one names the files it describes, so a
diagram and the code it claims to document can be checked against each other.

---

## 1. The whole system

Discovery is paid for once; replay is the production path; the catalogue is how an agent reaches it.
Policy, redaction and evidence are not a layer you pass through once — every runner uses them on
every action.

```mermaid
flowchart LR
    subgraph Authoring["Authoring — once per flow"]
        G["Goal + typed contract<br/><code>goals/*.yaml</code>"]
        D["Discovery agent<br/><code>discovery/agent.py</code>"]
        R["Recorder<br/><code>discovery/recorder.py</code>"]
        A[("Capability artifact<br/><code>capabilities/*.json</code>")]
        G --> D --> R --> A
    end

    subgraph Production["Production — every invocation"]
        C["Catalogue<br/><code>catalog.py</code>"]
        E["Replay engine<br/><code>replay/engine.py</code>"]
        AG(["Calling AI agent"]) --> C --> E
        A -.loads.-> C
        A -.executes.-> E
    end

    subgraph Shared["Used by both runners"]
        S["Surface<br/><code>surface/web/surface.py</code>"]
        P["Policy gate<br/><code>policy/gate.py</code>"]
        X["Redaction<br/><code>policy/redact.py</code>"]
        V["Evidence log<br/><code>evidence/log.py</code>"]
        H["Handoff<br/><code>handoff/*</code>"]
    end

    D --> S
    E --> S
    D & E --> P
    D & E --> V
    D & E --> H
    V --> X
    S --> APP[["Legacy app<br/>(mockbank)"]]
    H --> OP(["Human operator"])
```

**The claim to defend:** the model appears in exactly one box. Nothing in `replay/` can import a
model client, and a test walks the import graph to prove it.

---

## 2. Discovery: one turn of observe → decide → act

The model never writes a selector. It points at an element id that is valid only for this
observation; the surface turns that into ranked locators, verified against the live page before the
action happens.

```mermaid
sequenceDiagram
    autonumber
    participant M as Claude
    participant AG as Discovery agent
    participant SU as Surface
    participant PO as Policy gate
    participant RE as Recorder

    AG->>SU: observe()
    SU->>SU: accessibility tree per frame (CDP)<br/>+ neighbour-label naming
    SU-->>AG: Observation (e12 textbox "Member Number", …)
    AG->>AG: redact (PII, financial columns)
    AG->>M: trimmed view + masked screenshot + tools
    M-->>AG: fill(e12, {input: member_number}, intent)
    AG->>SU: target_for(e12)
    SU-->>AG: ranked locators, each verified unique
    AG->>PO: evaluate(action, where it leads)
    alt blocked
        PO-->>AG: block → tell the model, do not act
    else irreversible
        PO-->>AG: require_approval → pause for a human
    else allowed
        PO-->>AG: allow (risk: read/navigation/write)
        AG->>SU: re-resolve via locators, then perform once
        SU-->>AG: new state
        AG->>RE: record step (intent, locators, postcondition from the diff)
    end
```

**Why re-resolve before acting** (step 10): an approval can take minutes, and a handle captured
before the pause may be stale. Re-resolving also proves at record time that the locators we are
about to store actually work.

---

## 3. Replay: the lifecycle of one step

The interesting part is the wait loop: known states are checked *before* the postcondition, so an
error screen is recognised rather than waited out.

```mermaid
flowchart TD
    START([Step begins]) --> PRE["Settle known states<br/>wait for preconditions"]
    PRE --> RES{"Resolve target<br/>through the ladder"}
    RES -->|0 matches, budget spent| LNF[["failure: locator_not_found"]]
    RES -->|more than 1 match| AMB[["failure: ambiguous_locator"]]
    RES -->|exactly 1| GATE{"Policy gate"}
    GATE -->|block| PB[["policy_blocked"]]
    GATE -->|require approval| HUM["Ask a human"]
    HUM -->|denied| PB
    HUM -->|approved| GUARD
    GATE -->|allow| GUARD{"Write with a<br/>duplicate guard?"}
    GUARD -->|evidence already on screen| SKIP["Skip dispatch<br/>record as committed"]
    GUARD -->|no| ACT["Perform once<br/>(never re-dispatched)"]
    SKIP --> DONE([Step complete])
    ACT --> WAIT{"Poll: handler fired<br/>or postcondition met?"}
    WAIT -->|handler| PREC["Precedence:<br/>escalate &gt; hard_failure &gt;<br/>business_outcome &gt; recoverable"]
    WAIT -->|postcondition| DONE
    WAIT -->|timeout, write + guard| RECON{"Guard evidence<br/>appears?"}
    WAIT -->|timeout, otherwise| PCT[["failure: postcondition_timeout"]]
    RECON -->|yes| DONE
    RECON -->|no| AW[["failure: ambiguous_write<br/>caller must reconcile"]]
    PREC --> OUT[["business_outcome"]]
    PREC --> HF[["failure: app_error"]]
    PREC --> ESC[["needs_human"]]
    PREC --> REC["Recovery within budget<br/>dismiss / re-auth / recheck"]
    REC --> WAIT
```

---

## 4. Classifying what the screen is telling us

The brief calls conflating these the most common design mistake, so the taxonomy is data in the
artifact and the app profile, not branches in code.

```mermaid
flowchart TD
    OBS["Observed state"] --> Q1{"Did the caller<br/>break the contract?"}
    Q1 -->|yes| INV[["failure: invalid_input<br/>exit 40 — never reaches the UI"]]
    Q1 -->|no| Q2{"Does a declared<br/>handler match?"}
    Q2 -->|"escalate"| NH[["needs_human — exit 20"]]
    Q2 -->|"hard_failure"| AE[["failure: app_error — exit 40"]]
    Q2 -->|"business_outcome"| BO[["business_outcome — exit 10<br/>'no such member' is an answer"]]
    Q2 -->|"recoverable"| RC{"Budget left?"}
    RC -->|yes| FIX["Dismiss / re-auth / recheck<br/>then keep waiting"]
    RC -->|no| RE[["failure: recovery_exhausted"]]
    Q2 -->|no match| Q3{"Unmodelled dialog?"}
    Q3 -->|yes| NH
    Q3 -->|no| Q4{"Postcondition met<br/>in budget?"}
    Q4 -->|yes| OK[["success — exit 0<br/>with typed outputs"]]
    Q4 -->|no| PCT[["failure: postcondition_timeout"]]
```

Recovery is deliberately **not** a terminal status: it is reported alongside whichever status the run
reached, so "we dismissed a notice" can never be mistaken for an answer.

---

## 5. Who may act on the live session

One holder at a time, enforced in `Surface.perform` rather than merely recorded. Illegal transitions
raise; every transition is logged with the actor.

```mermaid
stateDiagram-v2
    [*] --> automation
    automation --> paused: intervention raised<br/>(escalation, approval, unknown dialog)
    paused --> human: operator takes control
    paused --> automation: approved / denied / timed out
    human --> resuming: operator hands back
    resuming --> automation: state re-verified<br/>continue after the furthest<br/>satisfied step
    resuming --> paused: verification failed
    automation --> closed
    paused --> closed
    human --> closed: aborted
    closed --> [*]

    note right of human
        Human actions captured,
        values masked in the page.
        Automation cannot act:
        lease.require("automation") raises.
    end note
```

---

## 6. What a capability actually is

```mermaid
classDiagram
    class Capability {
        schema_version "1.0"
        id, version (semver)
        status draft|approved
        title, description
        contract() for agents
        risk read|write|irreversible
    }
    class AppBinding { product, versions }
    class InputSpec { type, pattern, enum, sensitivity }
    class OutputSpec { type, currency, sensitivity, parse() }
    class Step {
        id, intent
        effect none|navigation|write
        irreversible, timeout_s
        performed_by automation|human
    }
    class Action { click fill select press_key wait_for extract request_human }
    class Target { frame_path }
    class LocatorCandidate { rationale }
    class Locator { role_name | label_neighbor | table_cell | text | css }
    class Condition { text_visible | element_present | field_value | frame_url | dialog_present }
    class Handler { code, kind, message, max_attempts }
    class Idempotency { evidence, timeout_s }
    class Provenance { method, model_id, prompt_hash, step_count, tokens, notes }

    Capability --> AppBinding : bound to a product, not a tenant
    Capability --> InputSpec : inputs
    Capability --> OutputSpec : outputs
    Capability --> Step : ordered
    Capability --> Condition : success
    Capability --> Handler : known states
    Capability --> Provenance
    Step --> Action
    Step --> Target
    Step --> Condition : pre / post
    Step --> Handler : step-scoped
    Step --> Idempotency : writes only
    Target --> LocatorCandidate : ranked
    LocatorCandidate --> Locator
```

---

## 7. One capability, many institutions

```mermaid
flowchart TD
    CAP[("Capability<br/>product: cu_teller<br/>versions: &gt;=7.2,&lt;8")]
    PROF["App profile<br/><code>config/apps/cu_teller.yaml</code><br/>login · known states · risky routes<br/>sensitive captions and columns"]

    subgraph Alpha["Tenant: Alpha CU (7.2.4)"]
        TA["base_url<br/>policy narrowing"]
        RA["Resolved capability"]
    end
    subgraph Beta["Tenant: Riverbend FCU (7.3.1)"]
        TB["base_url<br/>policy narrowing"]
        OV["Overlay — additions only<br/>'Member Inquiry' → 'Account Inquiry'<br/>'Member Number' → 'Account No.'"]
        RB["Resolved capability"]
    end

    CAP --> RA
    CAP --> RB
    PROF --> RA
    PROF --> RB
    TA --> RA
    TB --> RB
    OV -->|prepends locator rungs<br/>adds known states| RB

    RA --> RUN1["Run · locator_ranks all 0"]
    RB --> RUN2["Run · locator_ranks all 0<br/>capability.overlay = cu_beta"]
    CAP -.->|no overlay on a reworded tenant| FAIL[["Fails loudly:<br/>fallback matched the wrong control<br/>rank 1, postcondition caught it"]]
```

An overlay may only **add** locator candidates and known states. It cannot change inputs, outputs,
step order, effects or risk, and tenant policy may only *narrow* the product policy — widening
raises `PolicyWideningError`.

---

## 8. How an AI agent calls a capability

```mermaid
sequenceDiagram
    autonumber
    actor U as Member-facing conversation
    participant AG as Calling agent (LLM)
    participant CA as Catalogue
    participant EN as Replay engine
    participant APP as Legacy app

    CA->>AG: tool definitions from contract()<br/>typed inputs, outputs, risk, business outcomes
    U->>AG: "What's the savings balance for member 100871?"
    AG->>CA: call member_savings_balance_lookup{member_number}
    CA->>EN: run(capability, tenant, inputs)
    EN->>APP: sign on, drive the recorded flow (no model)
    APP-->>EN: screens
    EN-->>CA: RunResult
    CA-->>AG: {"status":"success","outputs":{"savings_balance":{"amount":"12940.00"}}}
    AG-->>U: "The Regular Savings balance is $12,940.00."

    note over AG,EN: For member 999999 the same path returns<br/>{"status":"business_outcome","outcome":{"code":"member_not_found"}}<br/>and the agent reports it instead of retrying.
```

---

## Where each diagram lives in code

| Diagram | Primary files |
|---|---|
| 1 Whole system | `src/cua/` top level |
| 2 Discovery turn | `discovery/agent.py`, `surface/web/surface.py`, `discovery/recorder.py` |
| 3 Step lifecycle | `replay/engine.py` (`_run_step`, `_await`, `_react`) |
| 4 Classification | `config/apps/cu_teller.yaml`, `schema/artifact.py` (`Handler`), `schema/results.py` |
| 5 Control | `handoff/control.py`, `handoff/capture.py`, `handoff/console.py` |
| 6 Artifact | `schema/artifact.py`, `schema/targets.py`, `schema/conditions.py` |
| 7 Multi-tenant | `tenancy/overlay.py`, `config/` |
| 8 Agent call | `catalog.py`, `cli.py` (`cua ask`) |
