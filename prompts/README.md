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

**1. Templates are static instructions; data arrives as a separate user message.**

These templates contain *no* placeholders. The agent's system prompt is the fixed instruction
block and the task data — the diff, the changed files, tool output, retrieved knowledge — is
supplied as a user message. Three reasons:

* The instruction block is identical across runs, so a provider's prompt cache can hit.
* Repository-derived text never lands inside the instruction block, which removes the most
  obvious prompt-injection path: a pull request cannot "append instructions" to the system
  prompt because it is not interpolated into it.
* It removes a class of runtime failures. The gateway's renderer is strict — it rejects a
  template that references an undefined variable *and* one that ignores a variable the caller
  supplied — so a template/data naming mismatch would be a hard failure rather than a silent
  omission. Keeping them separate means there is no naming contract to break.

**2. Every claim an agent makes must be traceable to evidence.**

Each template below states this explicitly and states the required output contract, because
spec section 32 forbids free-form text as an agent interface and section 34 requires evidence
to identify its source. Prompts restate the contract for the model's benefit; the Pydantic
models in `scout_domain.models` are what actually enforce it.

## Changing a prompt

Add a new version file. Never edit an accepted one: an edited prompt silently invalidates the
recorded provenance of every run that used it, which is precisely what versioning exists to
prevent. The agent's code selects the version explicitly, so promoting `v2` is a code change
with a review.
