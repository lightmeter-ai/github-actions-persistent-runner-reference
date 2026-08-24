# Retrospective

## The use case

Lightmeter was a small engineering team with a high CI workload. One Hetzner
CCX33 ran eight persistent listener slots, providing a predictable fixed-cost
pool with reusable tool and container caches.

During the measured 31 days from 21 July through 20 August 2026, the fixed-host
cohort processed 30,361 GitHub Actions job runs and 110,709 runner-minutes.
Excluding Dependabot-authored changes, the team opened 522 pull requests and
merged 489 during the same period. Across the four consecutive seven-day
intervals from 21 July through 17 August, opened PRs averaged 124.5 per interval
and ranged from 83 to 188.

Those measurements show that the host was doing real work. They do not prove
that the topology is right for another team or that every minute represented
maximum CPU utilization.

## Lineage

The persistent-runner history included months of GitLab Runner and GitHub
Actions operating experience. The later GitHub Actions lifecycle control plane
went through an intensive multi-week implementation and hardening period.

The code in this repository represents the GitHub Actions lifecycle model. It
does not include a GitLab adapter or claim one implementation supported both
providers unchanged.

## What worked well

- A single host could process a large number of jobs at predictable cost.
- Multiple listener slots kept the host busy and provided concurrency.
- Persistent tool and image caches avoided repeated cold downloads.
- systemd and cgroup identities made per-listener process ownership observable.
- A journal-first controller made crash boundaries explicit.
- Exact snapshots caught service, process, listener, and cgroup identity drift.
- Changing one slot at a time retained capacity and limited the mutation scope.
- Focused failure-injection tests made recovery behavior reviewable.

## What became expensive

Persistent workers accumulate state. The operational burden included:

- workspaces and temporary files left by jobs;
- containers, images, volumes, and networks;
- shared disk pressure and cache growth;
- processes that could outlive the job that created them;
- privileged cleanup with a potentially large blast radius;
- ambiguity between provider registration state and host process state;
- safe host upgrades without interrupting assigned work; and
- crash recovery across several durable and live-system boundaries.

One disk-pressure investigation found that work directories and shared caches
occupied substantial portions of the host filesystem. Cleaning them safely was
not simply a matter of running a periodic prune: several listeners shared the
host, and a cleanup had to distinguish abandoned state from another active
job's state.

The resulting implementation became large because the safety contract was
large. The complexity was not primarily in calling systemd or Docker. It was in
proving that each call targeted the intended idle slot and could be recovered
after interruption.

## Why managed ephemeral runners won

Managed ephemeral runners changed the ownership boundary. Each job received a
fresh machine, so many cross-job cleanup and isolation responsibilities no
longer belonged to the shared host controller. Capacity could follow demand
without maintaining a fixed listener fleet.

The migration to Ubicloud was therefore an operational trade-off, not a claim
that the persistent system never worked. The fixed host had good throughput;
the managed model required less bespoke privileged infrastructure and reduced
the consequences of state left by a previous job.

Provider performance remained workload-dependent. A managed runner could be
faster for some jobs and slower for others, especially when caches or large
container layers were involved. The decisive benefit was the ownership model,
not a universal benchmark result.

## Lessons worth preserving

1. Treat provider idle state and host idle state as separate evidence.
2. Journal intent before mutation, not after a command succeeds.
3. Bind names to process start times and cgroup filesystem identities.
4. Re-prove the exact identity immediately before the destructive boundary.
5. Mutate one worker at a time when other workers share the host.
6. Make recovery direction explicit for every durable step.
7. Do not clear ambiguous state to restore apparent availability.
8. Keep the unprivileged coordinator separate from the privileged backend.
9. Test interruption after the mutation but before the next journal update.
10. Reconsider whether maintaining the host is still the right product decision.

## Why this repository is narrow

The historical privileged backend, cleanup scripts, cloud wiring, admission
routes, and provider teardown were coupled to one private deployment. Releasing
them as an unsupported tool would create false confidence and disclosure risk.

This archive instead preserves the reusable reasoning: exact identity,
journal-first mutation, one-slot admission, post-change verification, and
deterministic rollback direction with durable refusal at ambiguous boundaries.
Engineers can study those ideas without mistaking the former deployment for a
maintained runner platform.
