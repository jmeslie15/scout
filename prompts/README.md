# Prompt templates

Prompts are version-controlled artifacts, not strings embedded in agent code (spec section 39).
Every agent run records the exact `role` + `version` it used, so a historical result stays
reproducible after a prompt is improved.

## Layout

```
prompts/<agent_role>/v<N>.md
```

`<agent_role>` must be one of the members of `scout_domain.enums.AgentRole`. The version
filename is restricted to `v?<digits>[.<digits>...]` or an ISO date, so a stray `README.md`
can never be treated as a prompt version.

## Two conventions that matter

**1. A template declares exactly the variables its call site supplies — no more, no fewer.**

The gateway's renderer is strict: it rejects a template that references an *undefined* variable
**and** one that ignores a variable the caller *did* supply. That second rule is the important
one. A silent `str.format` would drop an unused value without complaint, and the first anyone
would know is a prompt that quietly stopped mentioning the diff. Failing loudly is the point.

The consequence is that a template and its call site form a contract. Renaming a variable in an
agent means changing both in the same commit, and the mismatch fails on the next run rather than
degrading output invisibly.

| Role | Supplied by | Variables |
|---|---|---|
| `repository_analyzer` | `scout_orchestrator.llm_analysis._prompt_variables` | `repository_full_name`, `commit_sha`, `file_count`, `languages`, `frameworks`, `test_frameworks`, `services`, `changed_paths`, `changed_file_count`, `entry_points`, `api_surfaces`, `notes`, `deterministic_summary` |

The remaining six roles have templates but no call site yet — the AI review, security, test,
feature-verification, investigation, and report stages are not wired into the orchestrator. Their
templates therefore declare no variables, and whoever wires each one must add the variables it
supplies **and** list them here.

**2. Untrusted text is passed as a variable, never baked into the instruction block.**

Repository-derived content (paths, diffs, notes, tool output) reaches a prompt only through a
declared variable in a clearly delimited section, never as free-floating instruction text. That
keeps the trust boundary legible: an instruction block ends where a variable begins. It is not a
complete defence against prompt injection — nothing is — which is why every template also tells
the agent that repository content is data and that text directing its behaviour is a finding
rather than an order.


**3. Every claim an agent makes must be traceable to evidence.**

Each template below states this explicitly and states the required output contract, because
spec section 32 forbids free-form text as an agent interface and section 34 requires evidence
to identify its source. Prompts restate the contract for the model's benefit; the Pydantic
models in `scout_domain.models` are what actually enforce it.

## Changing a prompt

Add a new version file. Never edit an accepted one: an edited prompt silently invalidates the
recorded provenance of every run that used it, which is precisely what versioning exists to
prevent. The agent's code selects the version explicitly, so promoting `v2` is a code change
with a review.
