# Recovery model

## Principle

The journal records authority before mutation. Recovery never guesses that a
particular service, process, or generation belongs to the interrupted
transaction merely because it looks similar.

An operation token owns one active transaction. The journal stores only the
token's SHA-256 digest. The token is required for caller-requested commit,
finalize, or abort operations; an operator recovery process may resume an
unfinished journal without recovering the original token.

## Durable boundaries

| Journal step | Mutation that may have happened | Recovery direction |
|---|---|---|
| `freeze-intent` | Listener may or may not be frozen | Restore a running, thawed original generation |
| `frozen` | Listener is frozen; no generation change is authorized | Thaw the exact original generation |
| `stop-intent` | Original listener may be running, stopping, or stopped | Restore and start the original generation |
| `stopped` | Original listener is stopped | Restore and start the original generation |
| `apply-intent` | Desired generation may or may not have been applied | Reapply and start the original generation |
| `applied` | Desired generation is installed but not accepted | Reapply and start the original generation |
| `start-intent` | Desired generation may be stopped or running | Restore it when the exact stopped boundary is proved; refuse a running identity that was never journaled |
| `awaiting-verification` | The exact desired listener is journaled as running but not externally accepted | Stop that exact identity, restore the original generation, and start it |
| `recovery-intent` | A prior recovery may itself have been interrupted | Resume only from the recorded recovery substep and exact snapshot |

The controller always records rollback as the direction for an unfinished
transaction. It completes that rollback only when each phase-appropriate
identity is provable and otherwise retains durable authority. A different
system might have a forward-recovery boundary, but that boundary must be
explicit and durable rather than inferred from whichever state happens to be
visible.

## Recovery algorithm

Recovery runs under the same non-blocking host lock as normal mutation:

1. Validate the complete journal and configuration identity.
2. Publish `recovery-intent` with the interrupted step retained as
   `recover_from` and the recovery substep set to `observe`.
3. Prove the selected slot idle.
4. Compare the observation with the journaled snapshot. A running listener must
   match the complete process and cgroup identity. A stopped listener is
   accepted only at a forward phase that authorized stopping or applying, and
   must match the static unit and cgroup identity.
5. Before each recovery mutation, publish its durable substep: `thaw-intent`,
   `stop-intent`, `apply-intent`, or `start-intent`.
6. Re-observe and journal the exact boundary after each completed mutation.
7. Apply the original generation only from an exact stopped boundary.
8. Start and prove the original generation, then journal the newly established
   process identity as `started`.
9. Re-observe that exact running, thawed identity and clear active authority
   atomically.

If any identity, idle, journal, or backend proof fails, recovery retains durable
authority and stops. It does not clear the journal to make an alert disappear.
In particular, a crash after a start changed live state but before the returned
process identity was journaled is ambiguous. The controller refuses to stop or
adopt that process merely because it has the expected unit and generation.

## Deadlines

`deadline_epoch` bounds each forward or recovery attempt. Commit, host
mutations, finalization, and recovery completion check the deadline immediately
before acting. Expiry raises a safety error and retains durable authority.
Recovery may later create a fresh bounded attempt, but it never revives an
expired commit or finalization decision.

## Crash cases covered by the test suite

The focused tests inject a crash immediately after each fake backend mutation:

- after freeze, while the journal still says `freeze-intent`;
- after stop, while the journal still says `stop-intent`;
- after generation application, while the journal still says `apply-intent`;
  and
- after restart, while the journal still says `start-intent`.

Each case reconstructs the controller and journal store around the same durable
state. The freeze, stop, and apply cases prove that recovery returns the
selected slot to the original running generation. The post-start case proves
that an unjournaled running identity is rejected and authority is retained.
Tests also cover an interruption during recovery: a busy proof leaves
`recovery-intent` intact, and a later retry completes only after the busy
condition clears. Injected crashes during recovery thaw, stop, and generation
apply resume from their durable substeps. A crash after recovery start but
before its returned identity is journaled is deliberately refused as ambiguous.
Separate cases prove that running-identity drift and expired commit or
finalization deadlines fail closed.

## What the journal cannot prove

The host journal does not prove remote provider state. A real deployment still
needs independent evidence that:

- the provider registration is the intended runner;
- the runner has no assigned, queued, or starting work;
- the registration identity did not change across the maintenance window; and
- the restarted runner became the same intended provider-side identity.

It also does not prove that a production backend correctly implements systemd,
cgroup, process, filesystem, or container isolation. Those are target-specific
contracts outside this archived snapshot.

## Operator rule

Never delete, rewrite, or bypass an unfinished journal merely to make the
controller run again. Diagnose the failed proof, restore the evidence boundary,
and let recovery complete. Manual journal removal destroys the record that says
which slot and generation may have been mutated.
