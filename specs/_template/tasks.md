# Tasks <NNN>: <Title>

Risk decides the reviewer and the gate:
- `high`: correctness-critical (data leakage, security, money, migrations, metrics, determinism). Opus review plus one adversarial test.
- `low`: helpers, config, wiring. Sonnet review.
- `human`: runs on real data or systems, interpretation, release, close-out. Prepared automatically, signed off by you.

Status: `[ ]` open, `[x]` done, `[?]` prepared and waiting for your sign-off.
Cite the requirement each task covers (REQ-<NNN>-xx) in the Task or Test column;
`sdd analyze` reports requirements no task covers.

| ID | Task | Test | Risk | Status |
|---|---|---|---|---|
| T-<NNN>-01 | | | low | [ ] |
