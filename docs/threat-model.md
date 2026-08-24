# Threat model

## Security posture

This repository contains an unsupported historical controller. It has no live
host backend and makes no claim that adapting its protocol will make a runner
secure. Persistent CI runners execute untrusted or semi-trusted build inputs;
their host boundary deserves the same care as any privileged multi-tenant
worker system.

## Assets

A real persistent-runner system may expose:

- repository source and checked-out workspaces;
- CI tokens, cloud credentials, signing material, and deployment authority;
- build artifacts and caches;
- Docker sockets, containers, images, volumes, and networks;
- other jobs sharing the host;
- host processes, filesystems, services, and kernel interfaces; and
- provider-side runner registration and job-assignment state.

## Trust boundaries

### CI provider to host

The provider assigns work to a listener. Provider-side status can race with a
host observation, so host-idle and provider-idle are separate proofs.

### Unprivileged controller to privileged backend

The lifecycle coordinator should not receive general root access. A production
design needs a narrow backend or forced-command boundary that validates exact
arguments, source identity, slot identity, and operation phase independently.

### One slot to another

Several listeners share a kernel, CPU, memory, and storage. A per-slot service
name is not isolation by itself. Processes, cgroups, workspaces, containers,
caches, and credentials must all remain attributable to the selected slot.

### Durable state to live processes

PIDs, cgroup paths, and service names can be reused. The journal therefore
binds multiple identity fields and rechecks them immediately before mutation.

## Threats and included mitigations

| Threat | Included mitigation |
|---|---|
| Acting on the wrong slot | Static slot set and exact unit template |
| PID reuse | PID plus process start time |
| Cgroup-path reuse | Cgroup path plus device and inode identity |
| Listener replacement after admission | Complete snapshot comparison across freeze and commit |
| Concurrent controllers | One non-blocking same-owner lock per state root |
| Crash between intent and mutation | Journal the intent before invoking the backend |
| Crash during recovery | Durable `recovery-intent`, original forward step, recovery substep, and exact snapshot retained |
| Token disclosure from state | Store only the token digest |
| Journal replacement or tampering | Physical same-owner directory, strict modes, no symlinks, atomic replacement |
| Configuration replacement or tampering | Same-owner, single-link regular file; no group/other write permission; opened identity rechecked |
| Configuration drift | Hash the exact slot set and unit template into the journal |
| Oversized configuration or journal | Bound the descriptor read and reject content over the declared limit |
| Busy runner mutation | Re-prove idle before admission, commit, finalization, and recovery |
| Stale operation authority | Enforce the journaled deadline before mutation or acceptance |
| Ambiguous evidence | Fail closed and retain active authority |

## Risks intentionally not solved here

- Pull-request trust policy and whether forked code may reach a persistent
  runner.
- GitHub or GitLab registration and authentication.
- Secret delivery and revocation.
- Rootless versus privileged container execution.
- Docker socket exposure.
- Kernel or container escape.
- Workspace and cache sanitization.
- Network isolation and egress policy.
- Supply-chain integrity for actions, images, packages, and tool downloads.
- Provider API pagination, rate limits, stale status, or identity reuse.
- Safe service stopping, cgroup killing, filesystem deletion, or Docker pruning.
- Portability beyond the historical Linux, systemd, cgroup-v2, and Docker
  topology.

The absence of those controls is why this project includes no production
backend or installer.

## Dangerous adaptation patterns

Do not:

- replace backend methods with unchecked `subprocess` calls;
- accept a service name, path, PID, or cgroup directly from an untrusted caller;
- infer idle state from an empty process list alone;
- treat provider `offline` status as proof that no job is queued or starting;
- kill a cgroup whose identity was not captured and re-proved;
- delete a workspace based on a glob or unresolved environment variable;
- clear an unfinished journal by hand;
- reuse one operation token for multiple slots; or
- claim portability from the in-memory tests.

## Historical limitations

The original system was deeply tested against one host shape and one set of
operational assumptions. It was eventually retired in favor of managed
ephemeral runners, which reduced cross-job state and host-ownership burden.
That decision should weigh heavily for anyone considering a new persistent
runner fleet.
