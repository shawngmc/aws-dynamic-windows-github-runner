# Ephemeral Runner Platform — Design & Roadmap

Status: **draft for discussion**. This document takes the working proof of concept in this
repository (Windows EC2 + GitHub + webhook Lambda) and lays out a design that can grow into a
multi-forge, multi-OS, multi-compute, multi-cloud platform without a rewrite at each step.

It is meant to be copied into the new repository as the seed of `docs/`, then split into ADRs
(architecture decision records) as each decision is confirmed.

---

## 0. Where the PoC stands (baseline and corrections)

What works well and should be kept:

- **One job, one fresh machine, then terminate.** This is the core security property; everything
  below preserves it.
- **Hard lifetime set first in user data**, before anything that can fail.
- **Launch-template-only `RunInstances`** in IAM, so the webhook Lambda can't launch arbitrary instances.
- **Digest/signature-checked downloads**, IMDSv2 only, encrypted volumes, no inbound.
- **Serial-console logging**, which survives a broken guest.

Things in the PoC that the new design must fix. Some of these differ from how the PoC is
usually described:

| # | Finding | Why it matters |
|---|---|---|
| F1 | **The instance *does* have an IAM role** (`RunnerRole`: `AmazonSSMManagedInstanceCore`, SSM `GetParameter/DeleteParameter` on `jit/*`, S3 `cache/*` + `logs/*`). The job runs as SYSTEM, so **the job has these credentials** through IMDS. | See F2–F4. "No IAM role" is a good target, but it isn't the current state. |
| F2 | The role can read **every** `jit/*` parameter, not just its own. | A malicious job in repo A that is running while repo B's runner is booting can read B's JIT config, register as B's runner, and take B's job **with B's secrets**. This is the most serious issue. |
| F3 | `cache/*` is shared and writable by every job from every repo. | Cross-repo cache poisoning: a build in one repo can plant artifacts consumed by another. |
| F4 | `logs/*` is writable by jobs. | A job can rewrite or delete the evidence of what it did. |
| F5 | `bootstrap.ps1` is fetched from S3 at every boot. | Anyone who can write `bootstrap/` owns every future runner. It also means boots aren't reproducible. |
| F6 | Auth to GitHub is a **fine-grained PAT** (not OIDC); one PAT, one webhook secret per stack. | PATs are tied to a person, expire silently, and can't be scoped per installation. |
| F7 | Dedupe and `MaxInstances` checks use `DescribeInstances` (eventually consistent) with no lock. | Concurrent deliveries can double-launch or exceed the cap. |
| F8 | No queueing: over-limit jobs are dropped. Missed webhooks are never recovered. | Jobs stay queued forever. |
| F9 | Public webhook Lambda holds the privileged permissions (Secrets, SSM, EC2 launch). | The internet-facing code path has the largest blast radius. |
| F10 | No fork-PR / public-repo policy. | If an allowed repo is public, a fork PR can run arbitrary code on these runners. |
| F11 | Runner, PowerShell and CLI are "latest" at each boot. | Slower boot (Windows already 5–8 min) and unpinned supply chain. |

---

## 1. Goals and non-goals

**Goals** (from the brief, plus a few added — marked ➕):

1. Strong guardrails against instances (and money) leaking.
2. Usage/run/cost reporting with SQL-style querying.
3. Multiple runner OSes: Windows, Linux (x64 + arm64), macOS later.
4. **Profiles**: short labels that expand into a full machine spec (OS, image, tools, disks, size, network tier, limits).
5. A small management dashboard.
6. Multiple forges: GitHub, Gitea, Forgejo, GitLab.
7. Adversarial security design, both platform-wide and per OS.
8. Fargate as a compute option, without being container-only. **Full VMs stay first-class** (Docker without DinD, KVM/QEMU, kernel modules, GUI tests).
9. Other clouds (Azure, GCP) later.
10. Many forge connections (apps, tokens, webhook secrets) and, where needed, many stacks.
11. ➕ Recover from dropped webhooks; queue instead of drop.
12. ➕ Faster Windows starts (baked images, warm pools).
13. ➕ Spot with on-demand fallback; multi-AZ capacity.

**Non-goals (for now):** Kubernetes-based runners (ARC already covers those well); long-lived or
reused runners; running untrusted public fork PRs by default.

---

## 2. Core principles

1. **Ephemeral, single-use, never reused.** A machine runs at most one job.
2. **Every state has a deadline.** Every lifecycle state has a deadline, and something outside the guest enforces it.
3. **Credential-less runners.** The machine starts with no cloud credentials. It proves who it is
   once, gets exactly its own one-time secrets, and loses that ability right after.
4. **Observe from outside.** Lifecycle comes from cloud events and forge webhooks, not from the guest.
   Guest reports are hints only, because the guest runs untrusted code.
5. **Least privilege per component.** The internet-facing handler can only enqueue.
6. **Demand-matching, not job-binding.** GitHub JIT, Gitea/Forgejo ephemeral and GitLab runners
   are *not* bound to the job that triggered them; any job with matching labels may land on them.
   The system matches **capacity to demand per label set** and records which job actually ran where
   (GitHub's `in_progress` event carries `runner_name`).
7. **Pluggable edges, stable core.** Forges, compute backends, OS agents and clouds are adapters
   behind small interfaces; the scheduler, policy, state machine and accounting are shared.
8. **Config as code.** Profiles and policies are versioned, schema-validated files; the deployed
   control plane loads a validated snapshot.

---

## 3. Architecture overview

```
           ┌──────────────── Forges ────────────────┐
           │ GitHub   Gitea   Forgejo   GitLab      │
           └──┬──────────┬───────────────▲──────────┘
     webhooks │          │ polling (reconciler)     │ register runner / JIT, cancel, status
              ▼          ▼                          │
   ┌───────────────────┐      ┌─────────────────────┴───────────────┐
   │ Ingest (public)   │      │ Forge adapters (per connection)     │
   │ verify sig,       │      │ github.py gitea.py forgejo.py       │
   │ dedupe delivery,  │      │ gitlab.py                           │
   │ normalize → SQS   │      └─────────────────────▲───────────────┘
   │ (no other perms)  │                            │
   └────────┬──────────┘                            │
            ▼                                       │
   ┌─────────────────────────────────────────────────┴──────────────┐
   │ Scheduler / Launcher (private)                                 │
   │  policy check → profile resolve → quota/budget (atomic DDB)    │
   │  → forge.register_runner → compute.launch → lease record       │
   └───────┬───────────────────────────────┬────────────────────────┘
           │                               │
           ▼                               ▼
   ┌──────────────┐  ┌──────────────┐  ┌───────────────────────────┐
   │ Compute:     │  │ Claim API    │  │ State store (DynamoDB)     │
   │ EC2 / Fargate│  │ (identity-doc│  │ leases, jobs, quotas,      │
   │ / Mac host / │  │  attested,   │  │ deliveries, connections    │
   │ Azure / GCP  │  │  one-shot)   │  └──────┬────────────────────┘
   └──────┬───────┘  └──────▲───────┘         │ streams
          │ boot            │                 ▼
          ▼                 │          ┌──────────────┐   ┌───────────────┐
   ┌──────────────────┐     │          │ Event log    │──▶│ S3 (Parquet)  │──▶ Athena / DuckDB
   │ Runner agent     │─────┘          │ (Firehose)   │   └───────────────┘
   │ (supervisor)     │                └──────────────┘
   │ runs ONE job     │
   └──────────────────┘
   Guardrails: per-lease deadline timers · reaper sweep · cloud-event watcher ·
               budget/quota circuit breaker · account vCPU quota · alarms
   Dashboard: authenticated API (read-only first) over DDB + rollups
```

### 3.1 Components

| Component | Runs as | Permissions | Notes |
|---|---|---|---|
| **Ingest** | Lambda, public URL / HTTP API route `/hook/{connection}` | read *that connection's* webhook secret; `sqs:SendMessage`; DDB conditional put on `delivery#` | Reserved concurrency cap; body size cap; replay dedupe by delivery ID with TTL. |
| **Scheduler/Launcher** | Lambda on SQS | forge creds, launch-from-template, DDB | Re-checks job is still queued before launching; requeues with backoff when at capacity (fixes F8). |
| **Reconciler** | Lambda, every 2–5 min | forge read, DDB | Lists queued jobs per connection; launches for unmet demand (fixes dropped webhooks); required for forges with poor webhooks. |
| **Claim API** | Lambda, private or public route | read pending runner secrets | See §7.2. The only way a machine gets its runner token. |
| **Lifecycle watcher** | Lambda on EventBridge (EC2 state change, ECS task state) + forge `completed` webhooks | DDB, terminate-own | Moves leases forward; terminates promptly when the job completes. |
| **Deadline enforcer** | EventBridge Scheduler one-shot per lease + reaper sweep | terminate-own (tag-conditioned) | §5. |
| **Rollup** | Lambda, nightly/hourly | DDB, S3, Athena | Pre-aggregates for the dashboard. |
| **Dashboard** | Lambda behind HTTP API + JWT authorizer | read DDB/S3; admin actions behind a second role | §9. |
| **Image pipeline** | EC2 Image Builder or Packer in CI | separate account/role | Bakes per-profile images; pins versions; §8. |

### 3.2 Data model (DynamoDB, single table)

| PK | SK | Purpose |
|---|---|---|
| `CONN#<id>` | `META` | Forge connection: type, base URL, auth reference, scope, enabled. |
| `DELIVERY#<conn>#<id>` | — | Webhook replay dedupe (TTL 3 days). |
| `JOB#<conn>#<job id>` | `META` / `EVT#<ts>` | Job as seen by the forge: repo, labels, queued/started/completed times, conclusion, actual runner. |
| `LEASE#<lease id>` | `META` / `EVT#<ts>` | One machine: profile, backend, instance/task ID, state, deadlines, cost estimate, the job it actually ran. |
| `QUOTA#<scope>` | `COUNTER` | Atomic concurrency counters (global, per profile, per connection, per repo), updated with conditional writes (fixes F7). |
| `BUDGET#<scope>#<day>` | — | Running spend estimate; circuit breaker reads this. |
| `FLAG#global` | — | Kill switch / pause launches. |

GSIs: `state + deadline` (for the reaper), `repo + day` (reporting), `instance id` (event lookup).

### 3.3 Lease state machine

```
REQUESTED → LAUNCHING → BOOTING → CLAIMED → RUNNER_ONLINE → JOB_RUNNING → FINISHING → TERMINATED
     │           │          │         │            │               │
     └──────── any state past its deadline → REAPING → TERMINATED (reason recorded)
Failure terminals: LAUNCH_FAILED, CLAIM_REFUSED, BOOT_TIMEOUT, IDLE_TIMEOUT, LIFETIME_EXCEEDED, KILLED
```

Default deadlines (per profile; overridable within policy bounds):

| State | Deadline | Source of the transition |
|---|---|---|
| LAUNCHING | 5 min | compute API / EC2 `running` event |
| BOOTING → CLAIMED | 15 min (Windows) / 5 min (Linux) | Claim API call |
| CLAIMED → RUNNER_ONLINE | 5 min | forge runner status / guest hint |
| RUNNER_ONLINE → JOB_RUNNING | idle timeout (20 min) | `in_progress` webhook with this runner name |
| JOB_RUNNING | profile `max_job_minutes` | `completed` webhook |
| FINISHING → TERMINATED | 10 min | EC2 `terminated` event |
| **Any** | profile `max_lifetime` (absolute) | wall clock |

---

## 4. Abstractions (the interfaces that make the rest pluggable)

Keep the control plane in **Python** (as now; standard library + cloud SDKs). Sketch:

```python
class Forge(Protocol):                  # github, gitea, forgejo, gitlab
    def verify_webhook(self, headers, body) -> bool
    def parse_event(self, headers, body) -> JobEvent | None     # normalized: queued/started/completed
    def list_queued_jobs(self) -> Iterable[JobDemand]          # reconciler
    def job_context(self, job) -> JobContext                   # event type, fork?, visibility, actor
    def create_runner(self, scope, labels, name) -> RunnerCredential   # JIT config / reg token
    def delete_runner(self, scope, runner_id) -> None
    def list_runners(self, scope) -> Iterable[RunnerInfo]      # orphan cleanup

class ComputeBackend(Protocol):         # aws_ec2, aws_fargate, aws_mac, azure_vm, gcp_gce
    def launch(self, spec: MachineSpec, lease: Lease) -> ComputeHandle
    def terminate(self, handle) -> None
    def list_owned(self) -> Iterable[ComputeHandle]            # tag/label filtered
    def verify_identity(self, attestation) -> ComputeHandle    # for the Claim API
    def lock_down_after_claim(self, handle) -> None            # e.g. disable IMDS
    def price_per_hour(self, spec) -> Decimal

class RunnerAgentDriver(Protocol):      # inside the guest: actions-runner, act_runner/forgejo-runner, gitlab-runner
    def configure(self, credential) -> None
    def run_one_job(self, idle_timeout) -> Outcome
```

`JobDemand → Profile → MachineSpec` is where profiles turn into concrete instance types, images,
disks and network.

---

## 5. Guardrails: instances that stay up, and runaway spend

Defense in depth. Every layer assumes the ones above it have failed.

| Layer | Mechanism | Catches |
|---|---|---|
| **L0 guest** | Lifetime shutdown timer set first (Windows `shutdown /s /t`, Linux `systemd-run --on-active` / `shutdown -h +N`); idle watchdog; `InstanceInitiatedShutdownBehavior=terminate`. | Normal case. **Untrusted**: the job can cancel it (it's admin/root). |
| **L1 per-lease timer** | At launch, create an **EventBridge Scheduler one-shot** at `launch + max_lifetime` that invokes terminate. Deleted on clean termination. | Guest timers cancelled or wedged. Precise, unlike a 15-min sweep. |
| **L2 state deadlines** | Reaper sweep (every 5 min) queries the `state+deadline` GSI and reaps anything past its state deadline. | Stuck in BOOTING, never claimed, idle runner, job finished but machine still up. |
| **L3 fast termination** | `completed` webhook → terminate the lease whose runner ran it after a short grace (e.g. 3 min for log upload). | Saves minutes per job, every job. |
| **L4 inventory reconciliation** | Sweep lists **all** owned resources by tag (instances incl. `stopped`, volumes, ENIs, EIPs, ECS tasks, snapshots) and compares them with leases. Unknown owned resource → terminate + alert. | Lost DDB writes, manual launches, bugs. |
| **L5 admission control** | Atomic DDB counters: global / per profile / per connection / per repo concurrency; per-day budget estimate per scope; per-job max size. | Runaway demand (e.g. a matrix of 200 jobs). |
| **L6 circuit breaker** | CloudWatch alarms (running count, launch rate, reaper actions > 0, estimated hourly spend) + **AWS Budgets** action → set `FLAG#global=paused` (and optionally terminate all). | Bug loops, abuse, mining. |
| **L7 account hard ceiling** | Run in a **dedicated AWS account**; lower its **EC2 On-Demand/Spot vCPU service quotas** to what you'd ever want; optionally an SCP limiting regions and instance families and denying `RunInstances` without required tags. | Everything above failing at once. AWS itself refuses. |
| **Alerting** | SNS → email/Slack/ntfy on: any reaper action, launch failures, claim refusals, breaker trips, forge auth failures (expiring tokens). | A reaper that has to act means something is wrong; surface it. |

Reaper actions are expected to be **rare**. Each one is logged as an incident event, and the reaper
counts toward the dashboard's health score.

---

## 6. Profiles

A profile is a named, versioned spec. Jobs ask for a profile by label; the label is the only input
the job controls, and it can only narrow within what the profile and policy allow.

### 6.1 Label grammar

```yaml
runs-on: [self-hosted, rp:win2025-vs]                 # profile only
runs-on: [self-hosted, rp:linux-build, size:xl]        # + size modifier from the profile's allowed sizes
runs-on: [self-hosted, rp:linux-kvm, spot:no, disk:500]
```

- A prefix (`rp:` here, configurable) avoids collisions with other runners' labels.
- Modifiers (`size:`, `arch:`, `spot:`, `disk:`) are accepted only if the profile declares them; anything unknown is rejected (as today with instance types).
- The runner is registered with **exactly the job's labels** (as the PoC does), so the match stays exact.

### 6.2 Schema (sketch)

```yaml
apiVersion: runners/v1
kind: Profile
metadata: { name: linux-kvm, extends: linux-base }
spec:
  backend: aws-ec2                     # aws-ec2 | aws-fargate | aws-mac | azure-vm | gcp-gce
  os: { family: linux, distro: ubuntu-24.04, arch: [x64, arm64] }
  image: { ref: image://linux-kvm/2026.11.1 }   # resolved by the image catalog to an AMI per region/arch
  sizes:
    default: l
    l:  { instance_types: [c8i.2xlarge, m8i.2xlarge] }   # first available wins (multi-AZ, fleet)
    xl: { instance_types: [c8i.4xlarge, m8i.4xlarge] }
  purchase: { spot: preferred, fallback: on-demand, max_spot_price_ratio: 0.7 }
  capabilities: [docker, kvm]          # kvm → nested-virt families or .metal only (validated)
  storage:
    root: { size_gib: 100, type: gp3, iops: 6000 }
    instance_store: work                # format & mount local NVMe as the work dir
    extra:
      - { name: cache, from_snapshot: snap://linux-kvm-cache/latest, size_gib: 200 }  # warm cache disk
  tools:                                # light, boot-time modules; heavy tools are baked into the image
    - { module: node, version: "22" }
  network: { tier: egress-restricted }  # see §7.4
  identity: none                        # or: oidc-role:<name> (scoped, see §7.3)
  user: { run_as: runner, sudo: true }  # Linux/macOS; Windows: system | runner-admin | runner
  limits: { max_lifetime_min: 240, idle_min: 15, max_job_min: 180, max_concurrent: 4, daily_budget_usd: 25 }
  policy: { allow: ["org:shawngmc/*"], deny_fork_prs: true, deny_public_repos: true }
  warm_pool: { size: 0 }               # pre-initialized, never-used stopped instances (Windows!)
```

- **Inheritance** (`extends`) keeps profiles short: `base → windows-base → win2025-vs`.
- **Validation** happens in CI against a JSON Schema plus semantic checks: KVM only on families that support it, arm64 image for arm64 types, Windows images not on Fargate Linux, and so on.
- **Tools: bake vs. boot.** Anything big or slow (Visual Studio Build Tools, Xcode, Android SDK, CUDA) goes in the **image**. Small, frequently changing tools can be boot-time **modules** (pinned version + digest). A boot-time module that shows up in most runs is a signal to bake it.
- **Disks:** instance store as the work dir (already done on Windows); extra EBS volumes, optionally **created from a snapshot** to give a warm dependency cache without a shared writable bucket.

---

## 7. Security design (adversarial)

### 7.1 Threat model

**Assets:** forge credentials (App keys, tokens), webhook secrets, other jobs' secrets and
artifacts, cache integrity, AWS account, money, evidence (logs).

| Adversary | Capability | Main goals |
|---|---|---|
| **A1 Malicious/compromised job** (bad commit, dependency, compromised maintainer) | Arbitrary code as admin/root on the runner | Steal other jobs' secrets, persist, pivot into AWS/VPC, poison caches, mine, erase traces |
| **A2 Fork PR author** (public repos) | Same as A1, without being a collaborator | Same |
| **A3 Internet** | Can hit the webhook/claim/dashboard URLs | Forge launches (cost), DoS, exploit parsers |
| **A4 Stolen forge credential** | Forge API as the platform | Register rogue runners, read code |
| **A5 Compromised control-plane dependency / insider** | Code in Lambdas, image pipeline | Everything |

Two things this design deliberately does **not** fully defend against, as with any self-hosted runner: (a) a job abusing **its own** repo's secrets, which is the repo's trust decision; (b) A5 beyond pinning, review, separate accounts and audit.

### 7.2 Credential-less bootstrap (fixes F1, F2)

1. The launcher creates the runner credential (JIT config / registration token) and stores it in DDB
   **encrypted with KMS**, keyed by lease ID and bound to the expected instance ID.
2. The machine boots with **no IAM role** (no instance profile at all). Its agent fetches the
   **signed instance identity document** (EC2: PKCS7/RSA-2048 from IMDS; Azure: attested data; GCP:
   instance identity JWT) and POSTs it to the **Claim API**.
3. The Claim API verifies the signature against the cloud's published cert, checks the instance ID
   belongs to a lease in `BOOTING`, that the instance launched recently and is in the right
   account/region, and that the lease is **not already claimed** (conditional write). It returns the
   credential **once** and deletes it.
4. The control plane then **disables IMDS** on that instance (`ModifyInstanceMetadataOptions HttpEndpoint=disabled`)
   so the job can't re-attest or read user data. Belt and braces: in-guest firewall rule blocking
   169.254.169.254 for the job user before the runner starts.
5. A second claim for the same instance means a replay or a race. Refuse it, terminate the instance, and alert.

Result: a job has no AWS credentials, can't read anyone else's runner credential, and can't get a
new one. Fargate works the same way (task metadata + `ecs:DescribeTasks` check, no task role).

Logs move off instance credentials too: the claim response includes a **presigned S3 PUT URL**
for exactly one key (`logs/<lease>/<file>`, short expiry). In addition, the control plane captures the
**serial console** (`GetConsoleOutput`) at termination, which is evidence the guest can't
rewrite after the fact (fixes F4).

### 7.3 When a job *needs* cloud access

Don't reintroduce instance roles. Use the forge's **OIDC token** (GitHub `id-token: write`;
GitLab `id_tokens`; Gitea/Forgejo support is newer, so check it) → `AssumeRoleWithWebIdentity` into a
role whose trust policy is scoped to `repo:owner/name:ref:refs/heads/main` etc. The *repo* gets
access, not the runner, and that works the same on GitHub-hosted runners. Caches use this, or
presigned URLs scoped per repo (fixes F3):

- `cache/<connection>/<repo>/…` — only that repo can read or write it.
- Optional rule: only default-branch runs may **write** the cache; PRs read only.

### 7.4 Network

- **Dedicated VPC** for runners. No peering, no TGW, no VPC endpoints into anything else.
  Separate from anything with data.
- SG: no inbound. NACL: deny egress to RFC1918 / link-local except what's required (DNS resolver;
  the claim endpoint if private).
- **Egress tiers** per profile:
  - `open` — internet via IGW/NAT (today's behavior).
  - `egress-restricted` — **Route 53 Resolver DNS Firewall** allowlist (forge, package registries,
    language mirrors) + block direct-IP egress through an egress proxy or NAT rules. Cheap.
  - `locked` — AWS Network Firewall with SNI allowlist. Strong, but roughly $0.40/h per endpoint
    (around $290/month), so make it optional.
- VPC Flow Logs (to S3) for forensics; GuardDuty (including EC2 Runtime Monitoring) for crypto-mining
  and C2 detection.
- No SSM Session Manager by default. Break-glass debug is a **profile flag** that only applies to
  allowlisted repos and adds the role at launch for that lease.

### 7.5 Control plane

- Ingest: signature check before parsing; per-connection secret; delivery-ID replay dedupe; body
  size cap; reserved concurrency; strict schema validation of labels and names; no AWS permissions
  except enqueue (fixes F9).
- **Policy engine** runs before any launch: repo/org allowlist, **fork-PR and public-repo denial by
  default** (GitHub: fetch the run, check `event` and `head_repository.fork`; GitLab: `source` /
  MR from fork), actor allowlist optional, profile allowlist per repo (fixes F10).
- Forge credentials:
  - GitHub: **GitHub App** per org/user installation instead of a PAT (fixes F6). Short-lived
    installation tokens, scoped to `administration:write` (repo) or `organization_self_hosted_runners:write`
    (org runner groups). Keep the App private key in Secrets Manager, or import it into **KMS** as an
    asymmetric key and sign the App JWT with `kms:Sign` so the key can never be read.
  - GitLab: group/project access token or runner-management token; Gitea/Forgejo: scoped token.
    Rotation reminders from token expiry metadata.
- Runner groups (GitHub org): put these runners in a **dedicated runner group** restricted to
  selected repos/workflows instead of the default group `1` (hard-coded in the PoC).
- Separate accounts: `runners-prod` (control plane + runners), `images` (pipeline), and optionally
  `runners-public` for anything that touches public repos.
- CloudTrail (org trail), AWS Config rules for "instance without lease tag", IAM Access Analyzer.

### 7.6 Per-OS hardening

| | Windows | Linux | macOS |
|---|---|---|---|
| Job identity | Option: SYSTEM (compat) / local admin `runner` / standard user. Default local admin, not SYSTEM, so per-user firewall rules apply. | Unprivileged `runner`; `sudo` per profile; not in `docker` group unless profile (docker group = root). Rootless Docker option. | Job runs in an ephemeral **VM** (Tart/Virtualization.framework) on the host, never on the host itself. |
| IMDS block | Windows Firewall outbound block 169.254.169.254 for non-SYSTEM + IMDS disabled after claim. | `iptables -m owner ! --uid-owner root -d 169.254.169.254 -j REJECT` + IMDS disabled after claim. | Host IMDS unreachable from the guest VM's NAT. |
| Remote access | WinRM, RDP, SMB server off; no SSM agent unless debug. | No sshd (or sshd off); no SSM agent unless debug. | No SSH into guest; host SSH only via break-glass. |
| Base | Defender on with tamper protection; LSA protection; PowerShell script block logging to serial/log. | Minimal image; AppArmor/SELinux enforcing; auditd → log upload; unattended security updates baked at image time. | SIP on; image from a pinned IPSW/Xcode set. |
| Integrity | Image signed/pinned; no fetching scripts from S3 at boot (fixes F5). | Same. | Same. |

---

## 8. Images, OSes and compute backends

### 8.1 Image pipeline

- **EC2 Image Builder** (AWS-native, cross-account sharing) or **Packer** (works for Azure/GCP
  too). Packer fits the multi-cloud goal better; recommended.
- Each image contains: the **runner supervisor** (§8.2), the forge agents (actions-runner,
  forgejo-runner/act_runner, gitlab-runner) at **pinned versions + digests**, PowerShell 7 /
  CLI tools, profile-specific toolchains.
- Build weekly + on demand; publish to an **image catalog** (DDB or SSM params:
  `image://linux-kvm/2026.11.1 → {us-east-1/x64: ami-…, us-east-1/arm64: ami-…}`).
  Profiles reference catalog names; the catalog lets you roll forward or back without editing profiles.
- Smoke-test every image with a real canary job before promoting it.
- Windows boot time: baking removes the 2–3 min of installs. **Warm pools** (launch, initialize,
  sysprep-free stop, never used; start on demand in ~1 min) remove most of the rest. A warm instance is
  still single-use.

### 8.2 Runner supervisor (in-guest agent)

The PoC duplicates logic in user data and `bootstrap.ps1`; adding bash and macOS versions would triple it.
Replace it with **one small Go binary** cross-compiled for windows/linux/darwin × amd64/arm64,
baked into images:

1. Set the lifetime timer (OS-native) — first.
2. Attest → Claim API → receive runner credential, labels, log upload URLs, idle timeout.
3. Prepare disks (instance store, snapshot volumes), apply boot-time tool modules.
4. Apply in-guest network/IMDS rules; drop privileges per profile.
5. Run the forge agent via a driver in single-job mode (`--jitconfig`, `--ephemeral`/one-job, `gitlab-runner run-single --max-builds 1`).
6. Idle watchdog; stream progress to the serial console.
7. Upload logs (presigned), shut down.

User data shrinks to "start the supervisor with this lease ID and claim URL", which is the same for every OS.
Contract doc + golden tests for the supervisor are the integration boundary between the
control plane and every OS.

### 8.3 OS / backend matrix

| | Backend | Notes |
|---|---|---|
| **Windows Server 2025/2022** | EC2 | Current. Bake images, warm pools. Windows license is in the hourly price. |
| **Linux x64/arm64** | EC2 | Ubuntu 24.04 / AL2023. Graviton is often ~20–40% cheaper per build. Native Docker (no DinD). |
| **Linux + KVM/QEMU** | EC2 | **Nested virtualization** is now supported on some *virtual* instance families (AWS announced C8i/M8i/R8i in Feb 2026 and more Intel platforms in June 2026; KVM and Hyper-V). Otherwise `.metal`. The profile validator enforces this. |
| **Windows + Hyper-V/WSL2** | EC2 | Same nested-virt families. |
| **Linux light** | **Fargate** | Fast start (~30–60 s), per-second billing, no OS to patch. **No privileged mode → no Docker daemon, no KVM**, ≤16 vCPU/120 GiB, ≤200 GiB ephemeral storage. Good for lint/unit tests/scripting. Fargate Windows exists but starts slowly and costs more; deprioritize it. |
| **macOS** | EC2 Mac dedicated hosts | **24-hour minimum host allocation** and slow host scrubbing between instances make one-instance-per-job impractical. Model: a **host pool** (`aws-mac` backend) with a long-lived mac instance whose supervisor runs each job in a **fresh Tart VM** cloned from a golden image, then deletes it. The host is "infrastructure"; the VM is the ephemeral runner. Cost floor is roughly one host-day per day kept, so plan scheduled pool sizes (e.g. weekdays only). |

Capacity: launch via **EC2 Fleet (`instant`)** with the profile's instance type list across
multiple subnets/AZs, and spot-then-on-demand. Spot interruption (2-min warning) → the supervisor
reports it, the job fails/retries, and the lease is recorded as `SPOT_INTERRUPTED` for reporting.

---

## 9. Reporting and dashboard

### 9.1 Data flow

- **Live state:** DynamoDB (leases, jobs, counters).
- **History:** every lease/job event → DDB Streams → Firehose → **S3 Parquet**, partitioned by
  `dt=` (and connection). Cheap, append-only, and the forensic record.
- **Query:** **Athena** for ad-hoc SQL. (Don't plan around **S3 Select**: AWS closed it to new
  customers in 2024.) For a small deployment, **DuckDB inside a Lambda** reading the Parquet
  directly is cheaper and fast enough for the dashboard's queries.
- **Cost:** two sources, reconciled:
  1. *Estimated*, real time: lease duration × price (Pricing API, cached daily per type/OS/region/purchase option) + EBS + snapshot volume.
  2. *Actual*, daily: **activate cost allocation tags** (`runner-lease`, `runner-profile`,
     `runner-repo`, `runner-connection`) and export **CUR 2.0 / Data Exports** to S3. Join in
     Athena to check the estimates.
- **Rollups:** an hourly job writes small aggregates (per day × repo × profile: jobs, minutes,
  queue wait, boot time, cost, failures by reason) so dashboard pages never scan raw history.

### 9.2 Metrics worth having from day one

Queue wait (queued → in_progress), boot time (launch → claimed → online), job duration, idle
waste (online → job start, and job end → terminated), cost per job / repo / profile / day, spot
savings and interruption rate, reaper actions, launch/claim failures by reason, demand vs. capacity
refusals.

### 9.3 Dashboard

- **Hosting:** API Gateway HTTP API + Lambda (server-rendered HTML, htmx for refresh; no SPA build).
  Auth: **JWT authorizer with Cognito** (hosted UI, MFA, one user is fine) or any OIDC IdP you already use. A bare Lambda function URL with `AuthType NONE` is not acceptable for this.
- **v1 (read-only):** live leases (state, age, deadline countdown, cost so far, link to job),
  recent jobs, per-day cost chart, failures, connection health (token expiry, last webhook seen).
- **v2 (actions, separate role, audited):** terminate a lease, pause/resume a profile or connection,
  global kill switch, warm-pool size, re-run reconciler, download logs.
- ➕ Optional: post a cost/boot-time summary back to the forge (GitHub check run annotation /
  job summary via the App) so developers see what their job cost.

---

## 10. Multiple forges

| Forge | Trigger | Single-use runner | Notes |
|---|---|---|---|
| **GitHub** | `workflow_job` webhook (repo, org or App) | JIT config (`generate-jitconfig`) | Use a GitHub App; org runner groups; `in_progress` gives `runner_name`. |
| **Gitea** | `workflow_job` webhook (added in 2025, built for exactly this use) + API polling | act_runner **ephemeral** registration | Gitea's status names differ from GitHub's (their "waiting"/"queued"). Check the minimum Gitea/act_runner versions when implementing. |
| **Forgejo** | Polling first (check webhook support at implementation time) | forgejo-runner **ephemeral** registration (enforced server-side) + one-job mode | The ephemeral flag is also in the HTTP API. Similar to Gitea but diverging: separate adapter, shared helpers. |
| **GitLab** | "Job events" webhook (`pending`) + API polling | Create runner via API (`glrt-` token) with exact tags; `gitlab-runner run-single --max-builds 1` | Pull model: a runner picks any job whose tags ⊆ its tags. That's acceptable under demand-matching (§2.6). Alternative: GitLab's own fleeting/instance executor, but it doesn't fit the shared control plane. |

The **reconciler** (polling queued jobs) is required for forges with weak webhooks and doubles as
dropped-webhook recovery for all of them.

---

## 11. Many connections, many stacks

- **Connection** = (forge type, base URL, credential reference, webhook secret, scope, enabled
  profiles, quotas). Each has its own ingest path `/hook/<connection-id>` and secret, so one leaked
  secret affects one connection and can be rotated alone.
- **One control plane handles many connections.** That covers several GitHub Apps/orgs, personal
  accounts, a self-hosted Gitea, etc.
- **Separate stacks (accounts) for hard trust boundaries**, not for scale: e.g. `private` vs.
  `public-oss`, or `personal` vs. `work`. The same IaC is deployed per environment (Terraform workspaces
  / CloudFormation StackSets), each with its own account, VPC, budget and vCPU quota. A thin
  "fleet" view can aggregate the dashboards' rollup data later.
- Deploy the platform from its own CI with **GitHub OIDC → AWS role** (no long-lived deploy keys).
  This may be the "OIDC" the brief has in mind; it's separate from runner auth.

---

## 12. Other clouds

Two possible approaches:

1. **One control plane (AWS), many compute backends.** `azure_vm` / `gcp_gce` adapters launch VMs in
   other clouds. Credentials come from **workload identity federation** (AWS role → Azure
   federated credential / GCP WIF), with no stored secrets. Claim API verifies Azure attested data or
   the GCP identity JWT. This is cheap to add once the interfaces exist. **Recommended first.**
2. **Mirror the control plane per cloud** (Functions/Cloud Run + Cosmos/Firestore). Only worth it
   if a cloud must work when AWS is unavailable, or for data residency. Keep the core library
   cloud-agnostic (storage, queue, scheduler behind interfaces) so this stays possible, but don't build it early.

IaC: if multi-cloud is a real goal, **OpenTofu/Terraform** modules (`infra/aws`, `infra/azure`,
`infra/gcp`) beat CloudFormation. If AWS-only for the foreseeable future, AWS CDK (Python) is a
reasonable alternative. This is a decision to make before Phase 1 (see §15).

---

## 13. Repository layout (new repo)

```
control-plane/            Python package
  core/                   models, lease state machine, policy, profile resolver, quotas, accounting
  forges/                 github/ gitea/ forgejo/ gitlab/
  compute/                aws_ec2/ aws_fargate/ aws_mac/ azure_vm/ gcp_gce/
  handlers/               ingest, scheduler, reconciler, claim, lifecycle, reaper, rollup, dashboard
  tests/                  unit + recorded webhook fixtures per forge
agent/                    Go runner supervisor (+ drivers per forge agent)
images/                   packer templates per OS family
profiles/                 *.yaml + schema.json
infra/aws/                IaC modules: control-plane, network, image-pipeline, account-guardrails
docs/                     DESIGN.md (this), THREAT-MODEL.md, adr/NNNN-*.md, runbooks/
.github/workflows/        lint, test, validate profiles, plan IaC, build agent, build images
```

---

## 14. Roadmap (task decomposition)

Ordered so each phase leaves a working system, and so that **the security-relevant agent contract
is fixed before a second OS is written against it.**

### Phase 0 — New repo, foundations
- [ ] Copy the PoC into the new repo (history optional); rename; licence; CI (ruff, unit tests, cfn-lint/IaC validate).
- [ ] Decide IaC (§15 Q1) and supervisor language (Q2); write ADR-0001…0003.
- [ ] Write `THREAT-MODEL.md` from §7.1.
- [ ] **Quick PoC fixes worth doing immediately** (small, high value, carry forward):
  scope the instance role's `ssm:GetParameter` to its own name via `ec2:SourceInstanceARN`/tag
  condition, or move the JIT config to user data for now (F2); per-repo cache prefixes (F3);
  logs write-only by prefix (F4); dedicated GitHub runner group (F10 partial).

### Phase 1 — Stateful control plane (still Windows + GitHub + EC2)
- [ ] DynamoDB table; lease state machine; event recording.
- [ ] Split **ingest → SQS → scheduler**; delivery dedupe; atomic quotas; requeue on capacity.
- [ ] Reconciler (poll queued jobs).
- [ ] Lifecycle watcher (EC2 state events, `in_progress`/`completed` webhooks, fast terminate).
- [ ] Per-lease EventBridge Scheduler deadline; reaper over the deadline GSI; inventory sweep.
- [ ] Alarms + SNS; kill switch flag.
- [ ] Forge/Compute interfaces with the GitHub/EC2 implementations moved behind them.

### Phase 2 — Security model + agent contract
- [ ] Claim API (identity-document attestation, one-shot, KMS-encrypted pending credentials).
- [ ] **Remove the instance profile**; disable IMDS after claim; presigned log upload; console capture.
- [ ] Policy engine: fork-PR/public-repo denial, per-repo profile allowlist.
- [ ] GitHub App auth (multiple installations = multiple connections).
- [ ] Runner supervisor v1 (Go) for Windows, replacing user data + `bootstrap.ps1`.
- [ ] Dedicated account, vCPU quota ceiling, Budgets action, GuardDuty, flow logs.

### Phase 3 — Profiles, images, Linux
- [ ] Profile schema + validator + resolver + label grammar; image catalog.
- [ ] Packer pipeline: `windows-base`, `linux-base` (x64/arm64); pinned agents; canary promotion.
- [ ] Linux supervisor build + hardening; Graviton profile; KVM profile on nested-virt families.
- [ ] EC2 Fleet launch with type lists, multi-AZ, spot/on-demand fallback.
- [ ] Egress tiers (DNS Firewall first).

### Phase 4 — Reporting + dashboard
- [ ] Firehose → Parquet; Glue table; Athena workgroup; cost allocation tags + CUR export.
- [ ] Price cache; per-lease cost estimate; hourly rollups.
- [ ] Dashboard v1 (read-only, Cognito/JWT); later v2 actions with audit.

### Phase 5 — More forges
- [ ] Gitea adapter (webhook + ephemeral act_runner driver).
- [ ] Forgejo adapter (polling + ephemeral/one-job driver).
- [ ] GitLab adapter (job webhook + `run-single` driver).
- [ ] Recorded-fixture test suites per forge.

### Phase 6 — Speed and alternative compute
- [ ] Windows warm pools; snapshot-backed cache volumes.
- [ ] Fargate backend (Linux light profiles).
- [ ] Forge-side cost/boot summaries.

### Phase 7 — macOS
- [ ] `aws-mac` host-pool backend, scheduled pool sizing, Tart VM-per-job supervisor mode.

### Phase 8 — Other clouds
- [ ] `azure_vm`, `gcp_gce` backends via workload identity federation; attestation verifiers; `infra/azure`, `infra/gcp`.

---

## 15. Open decisions (need your input)

1. **IaC:** OpenTofu/Terraform (multi-cloud, recommended) vs. CDK (AWS-only, Python) vs. stay on raw CloudFormation.
2. **Supervisor language:** Go single binary (recommended) vs. keep per-OS scripts (PowerShell/bash/zsh).
3. **Scale and budget:** expected jobs/day and peak concurrency; monthly budget ceiling. These set quotas and decide whether Network Firewall or warm pools are worth paying for.
4. **Public repos:** will any connected repo ever be public or accept fork PRs? If yes, give them a separate account/stack and the `locked` egress tier.
5. **GitHub scope:** personal account repos, an org, or both? (Org enables runner groups and org-level App installs.)
6. **Accounts:** is AWS Organizations available, for SCPs and dedicated accounts?
7. **Who uses the dashboard:** just you, or others? This decides Cognito vs. an existing IdP.
8. **Build or adopt:** [philips-labs/terraform-aws-github-runner](https://github.com/github-aws-runners/terraform-aws-github-runner)
   already covers GitHub + EC2 (Linux/Windows, ephemeral, JIT, pools, multi-runner config). The
   multi-forge, multi-cloud, profile and security goals here go beyond it, so building is
   reasonable, but borrow from its design (and from runs-on.com's label conventions) freely.
