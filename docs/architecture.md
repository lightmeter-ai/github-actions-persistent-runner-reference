# Architecture

## Scope

The published controller changes one persistent runner slot while the other
slots on the host continue serving work. It preserves the durable
coordination and recovery logic but ships no privileged host backend.

The historical topology was one Linux Hetzner CCX33 server with eight
GitHub Actions listener units. Each listener had a distinct systemd service,
process identity, cgroup, workspace, and container boundary. The public example
uses the same slot count with synthetic identities.

## Components

### Configuration

`Config` declares an ordered slot set and a unit-name template. The controller
hashes that configuration into the journal. A later invocation with a different
slot set or unit template refuses the existing journal instead of guessing how
the topology changed. The JSON loader accepts only a same-owner, single-link
regular file that is not writable by its group or other users. It verifies the
opened file identity and enforces the byte limit against the actual read.

### Controller

`LifecycleController` owns transaction sequencing. It never discovers slots
implicitly. The caller selects one configured slot and supplies independently
collected pre-change and post-change evidence hashes.

The controller:

- admits only an idle slot in its journaled generation;
- saves intent before each host mutation;
- records only a hash of the operation token;
- freezes and re-observes the exact listener identity;
- changes one slot at a time;
- holds durable authority while awaiting post-change verification before
  committing the new generation; and
- rolls an unfinished transaction back only when every phase-appropriate
  identity can be proved, otherwise retaining the journal for diagnosis.

### Journal store

`JournalStore` creates a mode-0700, same-owner state directory and writes a
mode-0600 JSON journal. Each update is written to a temporary file, flushed,
atomically renamed, and followed by a directory flush.

The store rejects:

- a symlinked or non-directory state root;
- a state root with unexpected owner or mode;
- a symlinked, linked, oversized, world-readable, or malformed journal;
- a journal that changes identity while it is opened; and
- a second controller holding the non-blocking host lock.

Read-only inspection does not create the state root, lock, or journal.

### Backend protocol

`Backend` separates durable coordination from privileged observations and
mutations. It requires these operations:

- snapshot one slot;
- prove the slot idle;
- freeze and thaw the listener;
- stop and prove the exact snapshot;
- apply one named generation; and
- start and prove the requested generation.

The published `FakeBackend` implements those operations only in memory. It is a
test oracle, not a host-control example. A real backend would be responsible for
provider admission, systemd, process, cgroup, filesystem, container, and source
identity checks.

## Identity model

A `SlotSnapshot` binds one logical slot to all of these observations:

| Field | Purpose |
|---|---|
| Slot | Caller-selected logical identity |
| Generation | Named version currently installed for the slot |
| Unit | Exact service derived from the static configuration |
| Running and frozen state | Listener availability boundary |
| Main PID and start time | Detect PID reuse or service replacement |
| Listener PID and start time | Detect listener replacement inside the service |
| Cgroup path | Bind the process tree to the exact unit |
| Cgroup device and inode | Detect cgroup-path reuse |

A name or PID by itself is insufficient. The controller compares the complete
snapshot across the freeze and pre-commit boundaries.

## State machine

The durable happy path is:

```text
idle
  |
  v
freeze-intent -- freeze exact slot --> frozen
  |
  v
stop-intent -- stop exact identity --> stopped
  |
  v
apply-intent -- apply generation --> applied
  |
  v
start-intent -- start and prove --> awaiting-verification
  |
  v
post-change evidence accepted --> idle with new generation
```

Every arrow that invokes the backend has a durable intent state immediately
before it. A process can therefore determine what may have happened after a
crash without inferring intent from incomplete host state.

An unfinished state moves to `recovery-intent`, which retains both the forward
step and a durable recovery substep. Thaw, stop, generation apply, and restart
each receive their own intent boundary. Recovery clears active authority only
after the exact journaled original listener is observed running and not frozen.
If a restart succeeded but crashed before its new process identity was saved,
the controller refuses to infer ownership from the unit and generation alone.

Each forward or recovery attempt also has a deadline. The controller checks it
before every mutation and before final acceptance. Expiry retains the journal;
a later recovery call establishes a new bounded recovery attempt rather than
extending the expired forward operation.

## Admission and provider evidence

The controller accepts pre-change and post-change evidence as SHA-256 values. It
does not implement a provider API client. In the historical system, the
controller was surrounded by an independent admission layer that proved runner
identity, availability, and absence of assigned work before and after host
mutation.

That separation is deliberate. A host can prove its processes and cgroups but
cannot, by itself, prove that a remote CI provider has not just assigned a job.
A production adaptation needs both sides of the boundary.

## Why one slot at a time

Changing one slot preserves capacity for other work and reduces the number of
ambiguous states after interruption. It also gives each transaction one exact
listener, process tree, cgroup, token, and journal owner.

Fleet-wide cleanup may be simpler to invoke, but it couples unrelated jobs and
turns any mistaken identity or recovery decision into a host-wide incident.

## Deliberately absent production backend

The historical backend depended on one retired systemd, cgroup-v2, Docker, and
filesystem topology. Publishing those privileged operations in an unsupported
archive would imply portability and safety evidence that does not exist.

The protocol is included because the interface and invariants are reusable.
The implementation is omitted because its safe adaptation requires fresh
analysis for the target host, runner provider, container runtime, privilege
model, and failure policy.
