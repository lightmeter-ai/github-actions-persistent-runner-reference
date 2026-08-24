# Persistent GitHub Actions runners on one Hetzner host

> **Archived reference:** This repository preserves a historical system for
> study. It is not maintained or supported. It may contain assumptions that are
> unsafe or incorrect for your environment. Review and test every operation,
> especially privileged cleanup or service-control operations, before adapting
> it.

This project distills the lifecycle and crash-recovery model used to operate
eight persistent GitHub Actions runner listeners on one Hetzner CCX33 server.
It was built for a small team that wanted high CI throughput from one
predictably priced Linux host.

The source is a reference, not a drop-in runner installer. It contains the
journaled controller, a backend contract, an in-memory demonstration backend,
and focused recovery tests. It intentionally does **not** contain a privileged
systemd, cgroup, Docker-cleanup, cloud, credential, or GitHub API backend.

## Workload served

During one measured 31-day period, from 21 July through 20 August 2026, the
system served a team of two to three engineers at this scale:

| Measurement | Result |
|---|---:|
| Pull requests opened, excluding Dependabot | 522 |
| Pull requests merged, excluding Dependabot | 489 |
| Average opened PRs across four consecutive seven-day intervals | 124.5 per interval |
| Seven-day opened-PR range | 83–188 |
| Fixed-runner GitHub Actions job runs | 30,361 |
| Fixed-runner runtime | 110,709 runner-minutes |

The pull-request totals came from GitHub organization search with
Dependabot-authored pull requests excluded. The four full seven-day intervals
run consecutively from 21 July through 17 August; the remaining three days are
included in the 31-day totals but not the interval average.

The job totals came from GitHub organization Actions usage metrics. GitHub
classifies managed Ubicloud runners under the broad `self-hosted` runner type,
so the fixed-host cohort was calculated by excluding Ubicloud-labelled job
rows as well as GitHub-hosted rows. Repository, workflow, job, and service
breakdowns are intentionally omitted.

These numbers describe one workload and one period. They are not a capacity
guarantee, cost comparison, or claim that persistent runners are preferable for
other teams.

## Why the system existed

Persistent runners are attractive when a small team has enough CI work to keep
one host busy:

- the VM has a fixed monthly cost;
- toolchains, containers, and caches can stay warm;
- several listener slots can share CPU, memory, and storage; and
- the team controls the operating system and installed tools.

Those same properties create a difficult ownership boundary. A job can leave
processes, containers, workspace files, caches, credentials, or disk pressure
for the next job. Several listeners on one host can also interfere with each
other. A cleanup or upgrade that acts on the wrong process, cgroup, service, or
slot can interrupt unrelated work.

The reference controller concentrates on that boundary:

1. admit only one exact idle slot;
2. record mutation intent durably before acting;
3. freeze and re-prove the same listener identity;
4. stop, change, and restart only that slot;
5. wait for independent post-change evidence; and
6. preserve a deterministic rollback direction after a crash or failed
   verification, completing it only when the journaled identity is provable.

See [Architecture](docs/architecture.md),
[Recovery model](docs/recovery-model.md), and
[Threat model](docs/threat-model.md) for the complete contract.

## What is included

- [`reference/lifecycle_controller.py`](reference/lifecycle_controller.py):
  standard-library Python state machine, atomic journal, exact identity model,
  backend protocol, and in-memory backend.
- [`tests/test_lifecycle_controller.py`](tests/test_lifecycle_controller.py):
  focused success, refusal, interruption, and recovery cases.
- [`examples/run_demo.py`](examples/run_demo.py): a complete transaction that
  writes only to a temporary directory and uses only the in-memory backend.
- [`examples/config.example.json`](examples/config.example.json): the historical
  eight-slot shape with synthetic organization and repository names.
- Architecture, recovery, threat-model, and retirement documentation.

## What is not included

- Runner registration or GitHub authentication.
- A systemd or cgroup mutation backend.
- Docker cleanup, workspace deletion, cache pruning, or process killing.
- Host provisioning, firewall rules, SSH routes, secrets, or cloud resources.
- GitHub or GitLab CI workflows.
- Packaging, installation automation, or compatibility promises.
- Any current Lightmeter infrastructure dependency.

The omitted implementation was tightly coupled to one retired deployment and
included destructive operations. Publishing it as an unsupported tool would
create more risk than reusable value. The backend protocol shows where those
operations belonged without pretending that one deployment's safety checks are
portable.

## GitHub and GitLab history

Earlier versions of the persistent host were used with GitLab Runner. The
reference code in this repository is the later GitHub Actions implementation
and does not provide a GitLab adapter. It distills months of operating
experience, including an intensive multi-week implementation and hardening
period for the GitHub Actions version. This is a source-only snapshot; it does
not contain the private infrastructure repository's history.

## Why it was retired

The fixed host was ultimately replaced with managed ephemeral Ubicloud runners.
The shared server delivered substantial throughput, but disk management,
cross-job isolation, privileged cleanup, crash recovery, host maintenance, and
queue ownership imposed ongoing operational costs. Ephemeral runners gave each
job a fresh machine and moved more of that responsibility to a managed service.

Retirement does not mean the lifecycle model was valueless. The journal,
identity, and recovery techniques remain useful to engineers who deliberately
operate persistent or other stateful worker fleets. The full reasoning is in
the [retrospective](docs/retrospective.md).

## Run the safe demonstration

The module requires a POSIX system because it uses `fcntl` for its host lock.
Release checks exercise Python 3.11 through 3.14; native Windows is not
supported. The project has no third-party runtime or test dependencies.

```sh
python3 -B -m examples.run_demo
python3 -B -m unittest discover -s tests -v
```

The demonstration creates a temporary mode-0700 state directory, runs one
fake lifecycle transaction, prints its final journal and event sequence, and
removes the directory. It does not call systemd, Docker, GitHub, Hetzner, or any
network service.

The controller's only command-line operation is read-only inspection:

```sh
python3 reference/lifecycle_controller.py \
  inspect examples/config.example.json /path/to/state-root
```

If the state root does not exist, inspection reports `{"initialized": false}`
without creating it.

## Adapting the backend contract

A real backend would have to implement the `Backend` protocol and independently
prove every identity and idle-state assertion. At minimum, it would need to
bind:

- the configured slot to one exact service unit;
- the service to a PID and process start time;
- the listener to a PID and process start time;
- the unit to one exact cgroup filesystem identity;
- provider-side idle evidence to the same runner registration; and
- the installed source and configuration to a reviewed version.

Do not turn the in-memory backend into a production backend by replacing its
methods with unguarded shell commands. The hard part is the proof around each
operation, not invoking `systemctl`.

## Project status and support

The repository is intentionally archived after its first reference release.
There is no support, vulnerability-fix, compatibility, or future-release
commitment. See [SUPPORT.md](SUPPORT.md) and [SECURITY.md](SECURITY.md).

Forks may adapt the material under the Apache License 2.0. Attribution and
historical scope are recorded in [NOTICE](NOTICE).
