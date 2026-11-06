# Ephemeral Runner Platform — Design & Roadmap

Status: **draft, decisions round 1 applied** (see §16). This document takes the working proof of
concept in this repository (Windows EC2 + GitHub + webhook Lambda) and lays out a design that can
grow into a multi-forge, multi-OS, multi-compute, multi-cloud platform without a rewrite at each step.

It is meant to be copied into the new repository as the seed of `docs/`, then split into ADRs
as decisions are confirmed.

---

## 0. Where the PoC stands (baseline and corrections)

What works well and is kept:

- **One job, one fresh machine, then terminate.** This is the core security property; everything
  below preserves it.
- **Hard lifetime set first in user data**, before anything that can fail.
- **Launch-template-only `RunInstances`** in IAM, so the webhook Lambda can't launch arbitrary instances.
- **Digest/signature-checked downloads**, IMDSv2 only, encrypted volumes, no inbound.
- **Serial-console logging**, which survives a broken guest.
- **No NAT gateway, nothing running when idle.** That's already the right cost shape.

Findings the new design must fix:

| # | Finding | Why it matters |
|---|---|---|
| F1 | **The instance *does* have an IAM role** (`RunnerRole`: `AmazonSSMManagedInstanceCore`, SSM `GetParameter/DeleteParameter` on `jit/*`, S3 `cache/*` + `logs/*`). The job runs as SYSTEM, so **the job has these credentials** through IMDS. | See F2–F4. |
| F2 | The role can read **every** `jit/*` parameter, not just its own. | A job in repo A running while repo B's runner boots can read B's JIT config, register as B's runner, and take B's job **with B's secrets**. With public fork PRs in scope (§16), this is critical. |
| F3 | `cache/*` is shared and writable by every job from every repo. | Cross-repo cache poisoning. |
| F4 | `logs/*` is writable by jobs. | A job can rewrite or delete the evidence. |
| F5 | `bootstrap.ps1` is fetched from S3 at every boot, unpinned. | Whoever can write `bootstrap/` owns every future runner. |
| F6 | Auth to GitHub is a **fine-grained PAT** (not OIDC); one PAT, one webhook secret per stack. | PATs are tied to a person, expire silently, can't be scoped per installation. |
| F7 | Dedupe and `MaxInstances` use `DescribeInstances` (eventually consistent) with no lock. | Concurrent deliveries can double-launch or exceed the cap. |
| F8 | No queueing: over-limit jobs are dropped; missed webhooks are never recovered. | Jobs stay queued forever. |
| F9 | The public webhook Lambda holds the privileged permissions. | The internet-facing code has the largest blast radius. |
| F10 | No fork-PR / public-repo policy. | Fork PRs can run arbitrary code on these runners with whatever the runner can reach. |
| F11 | Runner, PowerShell and CLI are "latest" at each boot. | Unpinned supply chain; slower boots. |
| F12 | Secrets Manager for two secrets. | ~$0.80/month idle; SSM SecureString (standard tier) does the same for free. |

---

## 1. Goals, constraints, non-goals

**Top constraints, which win most trade-offs:**

- **C1 — Near-zero idle cost.** A month with no jobs should cost cents, not dollars. No NAT
  gateways, no always-on instances, no warm pools, no paid network firewalls, no per-hour managed
  services by default. Every always-billed resource must be listed in the idle-cost budget (§3) and
  justified. Paid extras are **opt-in modules**.
- **C2 — Security against hostile jobs.** Public, forkable repos are in scope, so a job may be
  written by an anonymous attacker.
- **C3 — Small-scale first, scalable second.** It must be easy and cheap for an individual developer
  or small team; it should still scale out (more connections, concurrency, accounts) without redesign.
- **C4 — No hard dependency on AWS Organizations** or other single-cloud governance features.
  They're supported as optional hardening.

**Goals:**

1. Layered guardrails against instances (and money) leaking.
2. Usage/run/cost reporting with SQL querying.
3. Runner OSes: Windows, Linux (x64 + arm64), macOS later.
4. **Profiles**: short labels that expand into a full machine spec.
5. A management dashboard, **multi-user from the start** (roles, per-connection scoping).
6. Forges: GitHub (personal accounts **and** orgs), Gitea, Forgejo, GitLab.
7. Adversarial security, platform-wide and per OS.
8. Fargate as one compute option. **Full VMs stay first-class** (Docker without DinD, KVM/QEMU, kernel modules, GUI tests).
9. Other clouds (Azure, GCP) later.
10. Many forge connections (apps, tokens, webhook secrets); multiple deployments where trust boundaries need them.
11. Queue instead of drop; recover from dropped webhooks.
12. Spot with on-demand fallback; multi-AZ capacity.

**Non-goals (for now):** Kubernetes runners (ARC covers those); long-lived or reused runners;
strict egress allowlisting by default (it needs a paid firewall; it's an opt-in module, §7.4).

---

## 2. Core principles

1. **Ephemeral, single-use, never reused.** A machine runs at most one job.
2. **Pay per job, not per hour.** Everything in the control plane is request-priced and serverless (Lambda, DynamoDB on-demand,
   SQS, EventBridge Scheduler, S3). Idle = no requests = no bill.
3. **Every state has a deadline**, and something outside the guest enforces it.
4. **Credential-less runners.** The machine starts with no cloud credentials. It proves who it is
   once, gets exactly its own one-time secrets, and loses that ability before any job code runs.
5. **Observe from outside.** Lifecycle comes from cloud events and forge webhooks. Guest reports
   are hints, because the guest runs untrusted code.
6. **Classify trust per job, not per repo.** Each job is classified *trusted* or *untrusted* before launch, and the class
   decides which profiles, network tier, caches and limits it gets (§7.2).
7. **Least privilege per component.** The internet-facing handler can only enqueue.
8. **Demand-matching, not job-binding.** GitHub JIT, Gitea/Forgejo ephemeral and GitLab runners
   take *any* job whose labels match. The system matches **capacity to demand per label set** and
   records which job actually ran where.
9. **Pluggable edges, stable core.** Forges, compute backends, OS agents and clouds are adapters.
10. **Config as code.** Profiles, policies and connections are versioned, schema-validated files.

---

## 3. Cost model

### 3.1 Idle-cost budget (no jobs for a month)

| Resource | Default | Idle cost | Notes |
|---|---|---|---|
| Lambda (ingest, scheduler, claim, reaper, reconciler, dashboard) | on | ~$0 | Free tier covers the sweeps; the reconciler backs off when idle (§4.1). |
| DynamoDB on-demand | on | ~$0 | Tiny storage; no requests. |
| SQS, EventBridge rules + Scheduler | on | ~$0 | Request-priced. |
| S3 (logs 30 d, cache by lifecycle, history Parquet) | on | cents | Lifecycle rules keep it bounded. |
| SSM Parameter Store, standard tier, `aws/ssm` key | on | $0 | Webhook secrets, App keys, tokens (fixes F12). |
| CloudWatch Logs (14-day retention) | on | cents | No ingestion when idle. |
| API Gateway HTTP API / Lambda URLs | on | $0 | Request-priced. |
| Cognito (dashboard users) | on | $0 | Free tier MAU. |
| AWS Budgets (breaker) | on | $0 | First budgets are free. |
| VPC, subnets, IGW, SGs, NACLs | on | $0 | **No NAT gateway**: public subnets, no inbound. |
| Route 53 Resolver DNS Firewall (untrusted tier) | on | ~$0 | Billed per query; no queries when idle. Check current pricing. |
| CloudWatch alarms | **off** | $0.10/alarm | Alerting is done in Lambda code (reaper/scheduler send SNS) instead. Alarms are opt-in. |
| Baked images (AMI snapshots) | **off** | ~$0.05/GB-month of used blocks per region | Opt-in per profile (§8.1). Example: a 40 GB Windows image ≈ $2/month. |
| Snapshot-backed cache volumes | **off** | snapshot storage | Opt-in per profile. |
| Customer-managed KMS keys | **off** | $1/key-month | Opt-in (e.g. for signing the GitHub App JWT without exposing the key). |
| GuardDuty, VPC Flow Logs | **off** | small, usage-based | Recommended opt-in for public-repo deployments. |
| Network Firewall, NAT gateway, warm pools, Mac hosts | **off** | $30–300+/month | Opt-in modules only; Mac hosts are allocated per demand (§8.3). |

**Target:** with defaults, an idle month costs **under $0.50** (S3/Logs storage). A CI check runs
`tofu plan` through a cost estimator (e.g. Infracost) and fails if an always-billed resource
appears that isn't on this list.

### 3.2 Per-job overhead

Instance-seconds (boot + job + shutdown), its EBS volume, a public IPv4 address ($0.005/h while
attached), and fractions of a cent in Lambda/DDB/SQS/S3. Spot is the default where the profile allows it.

---

## 4. Architecture overview

```
           ┌──────────────── Forges ────────────────┐
           │ GitHub   Gitea   Forgejo   GitLab      │
           └──┬──────────┬───────────────▲──────────┘
     webhooks │          │ polling (reconciler)     │ register runner / JIT, job context, status
              ▼          ▼                          │
   ┌───────────────────┐      ┌─────────────────────┴───────────────┐
   │ Ingest (public)   │      │ Forge adapters (per connection)     │
   │ verify sig,       │      └─────────────────────▲───────────────┘
   │ dedupe delivery,  │                            │
   │ normalize → SQS   │                            │
   └────────┬──────────┘                            │
            ▼                                       │
   ┌─────────────────────────────────────────────────┴──────────────┐
   │ Scheduler (private): trust classify → policy → profile resolve │
   │ → quota/budget (atomic DDB) → forge.create_runner             │
   │ → compute.launch → lease record + deadline timer             │
   └───────┬───────────────────────────────┬────────────────────────┘
           ▼                               ▼
   ┌──────────────┐  ┌──────────────┐  ┌───────────────────────────┐
   │ Compute:     │  │ Claim API    │  │ DynamoDB                   │
   │ EC2/Fargate/ │  │ (attested,   │  │ leases, jobs, quotas,      │
   │ Mac/Azure/GCP│  │  one-shot)   │  │ deliveries, connections,   │
   └──────┬───────┘  └──────▲───────┘  │ users, audit               │
          ▼ boot            │          └──────┬────────────────────┘
   ┌──────────────────┐     │                 ▼ streams
   │ Runner supervisor│─────┘          Firehose → S3 Parquet → DuckDB-in-Lambda / Athena
   │ (Go), ONE job    │
   └──────────────────┘
   Guardrails: per-lease deadline timers · reaper · cloud-event watcher · inventory sweep ·
               admission quotas · budget circuit breaker
   Dashboard: OIDC login, roles, per-connection scoping
```

### 4.1 Components

| Component | Runs as | Permissions | Notes |
|---|---|---|---|
| **Ingest** | Lambda, `/hook/{connection}` | read *that connection's* webhook secret; `sqs:SendMessage`; DDB conditional put on `delivery#` | Reserved concurrency cap; body size cap; replay dedupe. |
| **Scheduler** | Lambda on SQS | forge creds, launch-from-template, DDB, SSM put on `pending/*` | Re-checks the job is still queued before launch; requeues with backoff when at capacity (fixes F8). |
| **Reconciler** | Lambda, scheduled | forge read, DDB | Lists queued jobs; launches for unmet demand. **Adaptive cadence**: every 2 min while any lease or recent webhook exists, otherwise every 30–60 min. Required for forges with weak webhooks. |
| **Claim API** | Lambda, public route | SSM get/delete on `pending/*`, `ec2:DescribeInstances`, `ec2:ModifyInstanceMetadataOptions` (tag-conditioned) | §7.3. Runners can get their token only this way. |
| **Lifecycle watcher** | Lambda on EventBridge (EC2/ECS state) + forge `in_progress`/`completed` | DDB, terminate-own | Moves leases forward; terminates when the job completes. |
| **Deadline enforcer** | EventBridge Scheduler one-shot per lease + reaper sweep | terminate-own (tag-conditioned) | §5. |
| **Rollup** | Lambda, hourly when there was activity | DDB, S3 | Pre-aggregates for the dashboard; skips when nothing happened. |
| **Dashboard** | Lambda behind HTTP API | read DDB/S3; admin actions via a separate role | §9. |
| **Image pipeline** | Packer, run from CI on demand | separate deploy role | Optional (§8.1). |

No Lambda runs inside the VPC (no NAT needed, no ENI cost or cold-start penalty).

### 4.2 Data model (DynamoDB, single table)

| PK | SK | Purpose |
|---|---|---|
| `CONN#<id>` | `META` / `SCOPE#<installation>` | Forge connection and its installations/scopes (§10). |
| `DELIVERY#<conn>#<id>` | — | Webhook replay dedupe (TTL 3 days). |
| `JOB#<conn>#<job id>` | `META` / `EVT#<ts>` | Job as seen by the forge, with trust class and actual runner. |
| `LEASE#<lease id>` | `META` / `EVT#<ts>` | One machine: profile, backend, handle, state, deadlines, cost estimate, the job it ran. |
| `QUOTA#<scope>` | `COUNTER` | Atomic concurrency counters (global / trust class / profile / connection / repo / actor) via conditional writes (fixes F7). |
| `BUDGET#<scope>#<day>` | — | Running spend estimate; read by admission control. |
| `FLAG#global` | — | Kill switch / pause. |
| `USER#<sub>` | `GRANT#<conn or *>` | Dashboard users, roles, connection grants. |
| `AUDIT#<day>` | `<ts>#<user>` | Dashboard actions. |

GSIs: `state + deadline` (reaper), `repo + day` (reporting), `instance id` (event lookup). TTL on
deliveries, events and old jobs; history lives in S3.

### 4.3 Lease state machine

```
REQUESTED → LAUNCHING → BOOTING → CLAIMED → LOCKED → RUNNER_ONLINE → JOB_RUNNING → FINISHING → TERMINATED
any state past its deadline → REAPING → TERMINATED (reason recorded)
Failure terminals: LAUNCH_FAILED, CLAIM_REFUSED, BOOT_TIMEOUT, IDLE_TIMEOUT, LIFETIME_EXCEEDED,
                   SPOT_INTERRUPTED, KILLED
```

`LOCKED` means the control plane has disabled IMDS and the supervisor has confirmed it can no longer reach
it. Job code never runs before this point (§7.3).

| State | Default deadline | Transition source |
|---|---|---|
| LAUNCHING | 5 min | EC2 `running` event |
| BOOTING → CLAIMED | 15 min Windows / 5 min Linux | Claim API |
| CLAIMED → LOCKED | 2 min | Claim API + supervisor confirmation |
| LOCKED → RUNNER_ONLINE | 5 min | forge runner status |
| RUNNER_ONLINE → JOB_RUNNING | idle timeout (profile, e.g. 15 min) | `in_progress` webhook naming this runner |
| JOB_RUNNING | profile `max_job_min` | `completed` webhook |
| FINISHING → TERMINATED | 10 min | EC2 `terminated` event |
| **Any** | profile `max_lifetime_min` (absolute) | wall clock |

---

## 5. Abstractions

The control plane stays in **Python** (standard library + cloud SDKs); the in-guest supervisor is
**Go** (§8.2).

```python
class Forge(Protocol):                  # github, gitea, forgejo, gitlab
    def verify_webhook(self, headers, body) -> bool
    def parse_event(self, headers, body) -> JobEvent | None     # normalized queued/started/completed
    def list_queued_jobs(self, scope) -> Iterable[JobDemand]    # reconciler
    def job_context(self, job) -> JobContext                    # event, fork?, visibility, actor, actor permission
    def create_runner(self, scope, labels, name) -> RunnerCredential
    def delete_runner(self, scope, runner_id) -> None
    def list_runners(self, scope) -> Iterable[RunnerInfo]

class ComputeBackend(Protocol):         # aws_ec2, aws_fargate, aws_mac, azure_vm, gcp_gce
    def launch(self, spec: MachineSpec, lease: Lease) -> ComputeHandle
    def terminate(self, handle) -> None
    def list_owned(self) -> Iterable[ComputeHandle]
    def verify_identity(self, attestation) -> ComputeHandle
    def lock_down(self, handle) -> None                         # e.g. disable IMDS
    def price_per_hour(self, spec) -> Decimal

class StateStore, Queue, Timer, SecretStore, EventSink: ...     # AWS implementations first;
                                                                # keeps a non-AWS control plane possible (§12)
```

---

## 6. Guardrails: instances that stay up, runaway spend

Each layer assumes the ones above it have failed. None of them needs AWS Organizations.

| Layer | Mechanism | Catches |
|---|---|---|
| **L0 guest** | Lifetime timer set first; idle watchdog; `InstanceInitiatedShutdownBehavior=terminate`. | Normal case. **Untrusted**: the job is admin/root and can cancel it. |
| **L1 per-lease timer** | EventBridge Scheduler one-shot at `launch + max_lifetime` → terminate. Deleted on clean termination. | Guest timers cancelled or wedged. |
| **L2 state deadlines** | Reaper (every 5 min **only while leases exist**; hourly otherwise) reaps anything past its state deadline. | Stuck boots, never-claimed, idle runners. |
| **L3 fast termination** | `completed` webhook → terminate after a short grace (log upload). | Saves minutes on every job. |
| **L4 inventory sweep** | Lists all owned resources by tag (instances incl. `stopped`, volumes, ENIs, public IPs, ECS tasks, snapshots, Mac hosts) and compares them with leases. Unknown → terminate + alert. Also runs hourly when idle. | Lost writes, manual launches, bugs. |
| **L5 admission control** | Atomic DDB counters: global / trust class / profile / connection / repo / **actor** concurrency; per-day budget per scope; per-job size caps. | Big matrices, abuse, loops. |
| **L6 circuit breaker** | Spend estimate past threshold, or reaper actions above N/day → `FLAG#global=paused` + SNS. **AWS Budgets action** as an independent backstop: it attaches a deny policy to the scheduler role, so launches stop even if the control plane is buggy. | Bug loops, abuse, mining. |
| **L7 account boundary** | Recommended: a **dedicated standalone AWS account** (no Organizations needed). Optional module for Organizations users: SCP restricting regions/families/required tags. All platform roles carry an **IAM permissions boundary**, which works without Organizations. | Everything above failing at once. |
| **Alerting** | SNS (email; webhook to Slack/ntfy/Discord) on: reaper actions, launch/claim failures, breaker trips, forge auth failures, token expiry warnings. | Reaper action means a bug or an attack; surface it. |

---

## 7. Security design (adversarial)

### 7.1 Threat model

**Assets:** forge credentials (App keys, tokens), webhook secrets, other jobs' secrets and artifacts,
cache integrity, the AWS account, money, logs/evidence, the account's IP reputation.

| Adversary | Capability | Main goals |
|---|---|---|
| **A1 Malicious/compromised trusted job** (bad dependency, compromised maintainer) | Code as admin/root on the runner | Steal others' secrets, persist, pivot into AWS, poison caches, erase traces |
| **A2 Fork PR author / anonymous on a public repo** | Same as A1, no write access | Same, plus **free compute** (mining, proxying, attacking third parties) under your account's IPs |
| **A3 Internet** | Hits webhook/claim/dashboard URLs | Forged launches (cost), DoS, parser exploits, credential stuffing on the dashboard |
| **A4 Stolen forge credential** | Forge API as the platform | Register rogue runners, read code |
| **A5 Compromised dependency of the control plane / supply chain** | Code in Lambdas, images, supervisor | Everything |

Out of scope by design: a job misusing **its own** repo's secrets (the repo's trust decision);
full defence against A5 beyond pinning, review and audit.

### 7.2 Trust classification (per job)

Public, forkable repos are in scope, so trust can't be decided per repo. Before launch the scheduler
calls `forge.job_context()` and classifies the job:

- **trusted** if the repo is private/internal, **or** the run's event is one of
  `push`, `schedule`, `workflow_dispatch`, `release`, `merge_group` (configurable) **and** the actor
  has write permission on the repo.
- **untrusted** otherwise: `pull_request` from a fork, `pull_request_target`, `issue_comment`,
  `workflow_run` triggered by untrusted runs, actors without write access, unknown events.
  (Conservative default: unknown means untrusted.)

GitHub specifics: fetch the run (`event`, `head_repository`, `actor`), check
`collaborators/{actor}/permission`. Recommend, and show in the dashboard's connection health,
GitHub's **"Require approval for all external contributors"** setting on public repos, so a
maintainer approves fork runs before any job is queued.

What the class controls (defaults):

| | trusted | untrusted |
|---|---|---|
| Profiles | any allowed for the repo | only profiles marked `untrusted_ok` (small Linux by default) |
| Network tier | `standard` | `restricted` (§7.5) |
| Cache | read/write own repo prefix | **read-only**, own repo prefix |
| Limits | profile limits | lifetime ≤ 60 min, size ≤ `m`, 1 concurrent per actor, small daily budget |
| Debug / break-glass | allowed if the profile allows it | never |
| Separate subnet + SG | — | yes (flow logs on if enabled) |

Each class can also be pointed at a **different deployment** (another standalone account) for
people who want an account boundary between public and private work. This is optional: the
credential-less design (§7.3) is what lets a single account be acceptable.

### 7.3 Credential-less bootstrap (fixes F1, F2, F4, F5)

1. The scheduler creates the runner credential (JIT config / registration token) and stores it as an
   **SSM SecureString** at `pending/<lease>`, readable **only by the Claim API role**. No instance
   can read it.
2. The machine boots with **no instance profile**. User data contains only: lease ID, claim URL and the
   **SHA-256 of the supervisor binary**, which is verified before it runs (the bucket is only transport, fixing F5;
   baked images carry the supervisor already).
3. The supervisor sets the lifetime timer, reads the **signed instance identity document** (EC2
   PKCS7; Azure attested data; GCP identity JWT) and POSTs it to the Claim API.
4. The Claim API verifies the signature against the published cloud certificate, checks the instance ID
   matches a lease in `BOOTING`, the account/region match, the launch is recent, and the lease is not
   already claimed (conditional write). It returns the credential **once**, deletes the parameter, and
   returns presigned **S3 PUT URLs** for this lease's log keys only.
5. The Claim API then **disables IMDS** on the instance (`ModifyInstanceMetadataOptions
   HttpEndpoint=disabled`), an API-side change that root in the guest can't undo. The supervisor polls
   until IMDS is unreachable and reports it (`LOCKED`); only then does it start the forge agent. If lock-down
   isn't confirmed by its deadline, the lease is reaped.
6. A second claim for the same instance is treated as an attack: refuse, terminate, alert.
7. At termination the lifecycle watcher saves the **serial console output** (`GetConsoleOutput`) to S3,
   which is evidence the job can't rewrite.

Result: job code never sees cloud credentials, other runners' tokens, or user data, and can't get any.
Fargate is equivalent: no task role; attestation is checked by matching the task's ARN and its
one-time nonce against `ecs:DescribeTasks`.

### 7.4 When a job needs cloud access or caches

- **Cloud access:** the forge's OIDC token (GitHub `id-token: write`, GitLab `id_tokens`;
  Gitea/Forgejo support is newer, so check it) → `AssumeRoleWithWebIdentity` into a role scoped by repo
  and ref. The *repo* gets access, not the runner. Untrusted fork PRs don't get OIDC tokens with write
  scopes by default on GitHub. Keep it that way.
- **Caches (fixes F3):** presigned URLs from a small cache endpoint, scoped to
  `cache/<connection>/<repo>/…`. Trusted jobs get read/write; untrusted get read-only. A job proves its
  identity to the cache endpoint with its forge OIDC token, or with a per-lease cache token issued at
  claim time.

### 7.5 Network (no NAT, no paid firewall by default)

- **Dedicated VPC**, public subnets in 2–3 AZs, public IPv4 per instance (charged only while
  running), **no inbound** rules, no peering, no VPC endpoints into anything else.
- **Security group egress** (SGs can only allow, so this is an allowlist of ports):
  - `standard` tier: TCP 80/443 + TCP 22 (git over SSH, profile-optional) to anywhere.
  - `restricted` tier: TCP 80/443 only. No SMTP, IRC, or odd ports used by mining pools.
  - DNS: Amazon-provided DNS (VPC+2) and NTP (169.254.169.123) aren't filtered by SGs and remain
    available. **External DNS on 53 is blocked**, which forces lookups through the Resolver.
- **Route 53 Resolver DNS Firewall** on the restricted tier (optional on standard): AWS-managed
  threat lists (malware, botnet C2, and similar) + your own block list. Per-query pricing, so ~$0 idle.
- NACL: deny egress to RFC1918 and other private ranges on both tiers.
- **Honest limitation:** without a paid egress firewall, an untrusted job can still reach arbitrary
  HTTPS endpoints (and DoH bypasses DNS filtering). The design limits the *damage* instead:
  no credentials, isolated VPC, short lifetimes, small sizes, per-actor quotas, maintainer approval
  for fork runs, and optional GuardDuty to detect mining and C2. A strict allowlist via AWS Network
  Firewall is an **opt-in module** for those who want to pay for it.
- Optional: VPC Flow Logs to S3 (usage-priced) for forensics; GuardDuty with EC2 Runtime Monitoring.

### 7.6 Control plane

- Ingest: signature before parse; per-connection secret; delivery-ID dedupe; body cap; reserved
  concurrency; strict validation of labels/names; no permissions beyond enqueue (fixes F9).
- **Policy engine** before any launch: owner/repo allowlist, trust classification, per-repo profile
  allowlist, per-actor quotas (fixes F10).
- Forge credentials (fixes F6): **GitHub Apps** with short-lived installation tokens (§10).
  App private keys live in SSM SecureString (free); an opt-in module imports the key into KMS and
  signs App JWTs with `kms:Sign`, so the key can't be read ($1/month).
- All IAM roles carry a **permissions boundary**; launch permissions are restricted to the launch
  template, subnets and tagged resources; terminate permissions require the platform's tag.
- CloudTrail (the free management-event trail in a standalone account) for audit.

### 7.7 Per-OS hardening

| | Windows | Linux | macOS |
|---|---|---|---|
| Job identity | Local admin `runner` by default (SYSTEM available for compatibility). Admin inside a disposable, credential-less VM is acceptable. | `runner` user; `sudo` per profile (default on: the VM is disposable); `docker` group per profile. Rootless Docker option. | Job runs in an ephemeral **VM** on the host, never on the host. |
| IMDS | Disabled via API before the job (§7.3) + firewall rule as belt and braces. | Same + `iptables` reject to 169.254.169.254. | Host IMDS unreachable from the guest's NAT. |
| Remote access | WinRM, RDP, SMB server off; no SSM agent unless trusted-only debug. | No sshd; no SSM agent unless trusted-only debug. | No SSH into guest. |
| Base | Defender on; script block logging; pinned tools. | Minimal image; AppArmor/SELinux enforcing; pinned tools. | SIP on; pinned macOS/Xcode images. |
| Integrity | Supervisor digest-pinned in user data; no scripts fetched unpinned. | Same. | Same. |

---

## 8. Images, OSes and compute backends

### 8.1 Images: stock by default, baked opt-in

Because of C1, **stock vendor images are the default**: Windows Server / Ubuntu / AL2023 resolved
from public SSM parameters at launch, with the supervisor and pinned tool modules installed at boot.
That costs nothing at rest, but Windows boots take 5–8 minutes.

**Baked images are opt-in per profile**, for when boot minutes cost more than snapshot storage:

- **Packer** (works for Azure/GCP too) run from CI on demand; contains the supervisor, forge agents and
  toolchains at **pinned versions + digests**.
- An **image catalog** in DDB maps `image://win2025-vs/2026.11.1` to per-region/arch AMI IDs; profiles
  reference catalog names, so you can roll forward or back without editing profiles.
- Retention: keep the current + previous image only; deregister and delete snapshots beyond that
  (a scheduled Lambda, no AWS Backup/DLM dependency).
- Canary job before promotion.

Warm pools are **not** part of the default design. If added later, they'd be a paid opt-in module.

### 8.2 Runner supervisor (Go)

One static binary for windows/linux/darwin × amd64/arm64. User data is the same small stub on every OS.

1. Set the lifetime timer (OS-native) — first.
2. Attest → Claim API → credential, labels, log upload URLs, idle timeout, tool modules.
3. Wait for the IMDS lock-down and confirm it (`LOCKED`).
4. Prepare disks (instance store as the work dir; optional snapshot volumes); install pinned tool modules.
5. Apply in-guest firewall rules; create/drop to the job user.
6. Run the forge agent in single-job mode through a driver:
   `actions-runner --jitconfig`, `act_runner`/`forgejo-runner` ephemeral one-job,
   `gitlab-runner run-single --max-builds 1`.
7. Idle watchdog; progress to the serial console.
8. Upload logs via presigned URLs; shut down.

The supervisor's contract (claim request/response, states, exit codes) is versioned and has golden
tests; it's the integration boundary between the control plane and every OS.

### 8.3 OS / backend matrix

| | Backend | Notes |
|---|---|---|
| **Windows Server 2025/2022** | EC2 | Current. Stock image by default; baked opt-in. |
| **Linux x64/arm64** | EC2 | Ubuntu 24.04 / AL2023. Graviton is often cheaper per build. Native Docker. Boots in about 1 minute even on stock images. |
| **Linux + KVM/QEMU** | EC2 | Nested virtualization on supported virtual families (AWS announced C8i/M8i/R8i in Feb 2026 and more Intel platforms in June 2026; KVM and Hyper-V) or `.metal`. The profile validator enforces this. |
| **Windows + Hyper-V/WSL2** | EC2 | Same nested-virt families. |
| **Linux light** | Fargate | Fast start, per-second billing. No privileged mode → no Docker daemon, no KVM; ≤16 vCPU/120 GiB. For lint/test/scripting jobs. |
| **macOS** | EC2 Mac dedicated hosts | Hosts have a **24-hour minimum allocation**. Model: the `aws-mac` backend **allocates a host on demand** when a mac job arrives, runs every mac job in a fresh **Tart VM** cloned from a golden image, and **releases the host** after it has been idle past a configurable period (≥24 h). Idle months cost $0; a mac day costs roughly one host-day. Untrusted jobs are never allowed on mac by default. |

Capacity: **EC2 Fleet (`instant`)** across the profile's instance-type list and multiple AZs, spot
first with on-demand fallback. Spot interruption → recorded as `SPOT_INTERRUPTED`; the job fails
or retries per profile.

---

## 9. Reporting and dashboard

### 9.1 Data

- **Live:** DynamoDB.
- **History:** lease/job events → DDB Streams → Firehose → **S3 Parquet** (`dt=` partitions).
  Firehose is usage-priced, so ~$0 idle.
- **Query:** **DuckDB inside a Lambda** reading the Parquet directly, for the dashboard and quick
  reports (no idle cost, no Glue/Athena required). **Athena** stays available for ad-hoc SQL
  ($5/TB scanned, also ~$0 idle). Don't plan around S3 Select (closed to new customers since 2024).
- **Cost:**
  1. *Estimated* in real time: lease seconds × cached price (Pricing API, refreshed daily) + EBS + public IPv4.
  2. *Actual*, optional: activate cost allocation tags (`runner-lease`, `runner-profile`,
     `runner-repo`, `runner-connection`) + CUR 2.0 / Data Exports to S3, then reconcile. Off by default to keep setup small.
- **Rollups** hourly, only when there was activity.

### 9.2 Metrics from day one

Queue wait, boot time (launch → claimed → online), job duration, idle waste (before and after the job),
cost per job/repo/profile/actor/day, trusted vs untrusted share, spot savings and interruption rate,
reaper actions, launch/claim failures by reason, refused jobs by reason.

### 9.3 Dashboard (multi-user from the start)

- **Hosting:** API Gateway HTTP API + Lambda, server-rendered HTML + htmx. No SPA build, ~$0 idle.
- **Auth:** generic **OIDC code flow with PKCE implemented in the app** (session in an HttpOnly,
  SameSite cookie; CSRF tokens on actions). **Cognito** is the default IdP on AWS (free tier, MFA);
  any other OIDC IdP (Google, Entra ID, Authentik, Keycloak, GitLab, Gitea/Forgejo) can be swapped
  in. Keeping auth in the app keeps it portable across clouds.
- **Authorization:** roles `viewer`, `operator` (terminate lease, pause profile/connection, re-run
  reconciler), `admin` (connections, users, kill switch, budgets), each granted **per connection** or
  globally. The first admin is created by `tofu apply` output / CLI bootstrap.
- **Audit:** every action is written to `AUDIT#` and to the event stream.
- **v1:** live leases (state, deadline countdown, cost so far, job link), recent jobs, cost by day/repo,
  failures, connection health (token/key expiry, last webhook, fork-approval setting).
- **v2:** actions, log download, budget editing.
- Optional: post cost and boot time to the forge (GitHub check run / job summary) through the App.

---

## 10. Forges and connections

### 10.1 Connection model

A **connection** is a forge credential (forge type, base URL, auth, webhook secret). It has one or more
**scopes** (installations/owners/projects), each with an owner/repo allowlist, enabled profiles,
quotas and trust overrides. Each connection has its own ingest path `/hook/<connection>` and
secret, so one leaked secret affects one connection and can be rotated alone.

### 10.2 GitHub: personal accounts and orgs, dynamic

- Use a **GitHub App** (permissions: Actions read, Administration read/write on repos, Metadata; for
  org-level runners, Self-hosted runners read/write on orgs). Subscribe it to `workflow_job` and
  `installation*` events. **One App = one webhook URL and secret for every account it's installed on.**
- **Runners are registered at the repo level by default** via JIT. That works the same for
  personal accounts (which have no runner groups) and orgs. For orgs, an option registers JIT
  runners at the org level in a **dedicated runner group** restricted to selected repos.
  Either way allocation is dynamic, one runner per queued job.
- **App visibility:**
  - *Private App per owner*: simplest and safest. A private App can only be installed on the account
    that owns it, so you'd create one App for your personal account and one per org, i.e. one connection each.
  - *One public App*: one App for everything, but anyone can install it. The scope allowlist then
    becomes mandatory: installations from owners not on it are ignored and alerted on, and nothing launches.
  - Both are supported by the same connection model. The default docs use the private-App-per-owner pattern.
- Repo selection on the App installation is the first allowlist; the platform's scope allowlist is the second.

### 10.3 Other forges

| Forge | Trigger | Single-use runner | Notes |
|---|---|---|---|
| **Gitea** | `workflow_job` webhook (added in 2025 for exactly this use) + polling | act_runner **ephemeral** registration | Status names differ from GitHub's; check minimum Gitea/act_runner versions when implementing. |
| **Forgejo** | Polling first (check webhook support at implementation time) | forgejo-runner **ephemeral** (enforced server-side) + one-job mode | Ephemeral is also in the HTTP API. Separate adapter from Gitea; shared helpers. |
| **GitLab** | "Job events" webhook (`pending`) + polling | Create a runner via API (`glrt-` token) with the exact tags; `gitlab-runner run-single --max-builds 1` | Pull model: a runner takes any job whose tags ⊆ its tags. Fine under demand-matching. Trust: MR from fork / protected refs. |

The reconciler covers forges with weak webhooks and recovers dropped webhooks for all of them.

---

## 11. Deployments and scaling out

- **One deployment** (one OpenTofu root module instance = one account + region) handles many
  connections. That's the default for an individual or small team.
- **More deployments** when you need a hard boundary or more capacity: a second standalone account
  for public-repo work, another region for capacity or latency, a team's own account. Same modules,
  separate state per deployment (`tofu` workspaces or separate root configs). No Organizations or
  StackSets required.
- An optional **fleet view** later aggregates rollups from several deployments (read-only,
  cross-account role or shared S3 prefix).
- The platform deploys itself from CI with **GitHub Actions OIDC → AWS role** (no long-lived keys).

---

## 12. Other clouds

1. **First: one control plane (AWS), many compute backends.** `azure_vm` and `gcp_gce` adapters launch VMs in other
   clouds using **workload identity federation** (AWS role → Azure federated credential / GCP WIF),
   with no stored cloud secrets. The Claim API verifies Azure attested data or the GCP identity JWT. Each
   cloud gets its own cheap network module (no NAT, no inbound) and its own inventory sweep.
2. **Later, if ever: a control plane per cloud** (Functions/Cloud Run + Cosmos/Firestore). Only for
   AWS-independence or data residency. The `StateStore/Queue/Timer/SecretStore/EventSink` interfaces
   (§5) keep this possible without building it now.

**IaC: OpenTofu** (decided). Module layout: `infra/modules/aws/{control-plane,network,compute-ec2,compute-fargate,compute-mac,images,dashboard,optional/*}`,
later `infra/modules/{azure,gcp}/…`, plus example root configurations under `infra/examples/`.

---

## 13. Repository layout (new repo)

```
control-plane/            Python package
  core/                   models, lease state machine, trust classifier, policy, profiles, quotas, accounting
  forges/                 github/ gitea/ forgejo/ gitlab/
  compute/                aws_ec2/ aws_fargate/ aws_mac/ azure_vm/ gcp_gce/
  platform/aws/           StateStore/Queue/Timer/SecretStore/EventSink implementations
  handlers/               ingest, scheduler, reconciler, claim, lifecycle, reaper, rollup, cache, dashboard
  tests/                  unit + recorded webhook fixtures per forge
agent/                    Go supervisor + drivers (actions-runner, act_runner, forgejo-runner, gitlab-runner)
images/                   Packer templates (optional baked images)
profiles/                 *.yaml + schema.json
infra/modules/            OpenTofu modules (aws/ first)
infra/examples/           minimal (single account), public-repos (two deployments), with-optional-modules
docs/                     DESIGN.md, THREAT-MODEL.md, COST.md, adr/, runbooks/
.github/workflows/        lint, test, validate profiles, tofu validate/plan, idle-cost check, build agent, (images)
```

---

## 14. Profiles

### 14.1 Label grammar

```yaml
runs-on: [self-hosted, rp:win2025]                    # profile only
runs-on: [self-hosted, rp:linux, size:xl, arch:arm64]  # modifiers the profile declares
runs-on: [self-hosted, rp:linux-kvm, spot:no]
```

The prefix (`rp:`, configurable) avoids collisions; unknown modifiers are rejected; the runner is
registered with exactly the job's labels.

### 14.2 Schema (sketch)

```yaml
apiVersion: runners/v1
kind: Profile
metadata: { name: linux-kvm, extends: linux-base }
spec:
  backend: aws-ec2
  os: { family: linux, distro: ubuntu-24.04, arch: [x64, arm64] }
  image: { stock: ubuntu-24.04 }          # or { baked: image://linux-kvm/2026.11.1 }
  sizes:
    default: l
    l:  { instance_types: [c8i.2xlarge, m8i.2xlarge] }
    xl: { instance_types: [c8i.4xlarge, m8i.4xlarge] }
  purchase: { spot: preferred, fallback: on-demand }
  capabilities: [docker, kvm]             # kvm → validated against nested-virt families / .metal
  storage:
    root: { size_gib: 100, type: gp3 }
    instance_store: work
    extra: []                             # e.g. { from_snapshot: snap://…, size_gib: 200 } (opt-in, costs at rest)
  tools: [ { module: node, version: "22", sha256: "…" } ]
  user: { name: runner, sudo: true, docker_group: true }
  trust: { untrusted_ok: false }          # §7.2
  network: { tier: auto }                 # auto = by trust class; or force restricted
  limits: { max_lifetime_min: 240, idle_min: 15, max_job_min: 180, max_concurrent: 4, daily_budget_usd: 25 }
  debug: { session_manager: false }       # trusted-only when true
```

Inheritance (`extends`) keeps profiles short. CI validates schema and semantics (KVM families,
arch/image match, Fargate limits, untrusted-ok profiles within the untrusted caps). Shipped defaults:
`linux` (untrusted_ok, small), `linux-build`, `linux-kvm`, `win2025`, `fargate-light`.

---

## 15. Roadmap (task decomposition)

Each phase leaves a working system. The security contract (claim + supervisor) is fixed **before**
a second OS is written against it, and public repos aren't enabled until trust classification exists.

### Phase 0 — New repo and foundations
- [ ] New repo; copy the PoC as `legacy/` for reference; licence; CI (ruff, pytest, `tofu fmt/validate`, tflint, Go build/test).
- [ ] ADRs: 0001 OpenTofu, 0002 Go supervisor, 0003 near-zero idle cost (§3) as a hard constraint, 0004 credential-less runners, 0005 per-job trust classification.
- [ ] `THREAT-MODEL.md`, `COST.md` (idle budget table + CI cost check).
- [ ] **If the PoC stays in use meanwhile:** move secrets to SSM, scope the JIT read (or pass it in user data), per-repo cache prefixes, logs write-only, and **don't connect public repos to it**.

### Phase 1 — Stateful control plane (Linux + GitHub + EC2, private repos)
Linux first: it boots in about a minute, which makes iteration fast; Windows follows in Phase 3.
- [ ] OpenTofu modules: network (no NAT), control plane, compute-ec2.
- [ ] DynamoDB; lease state machine; ingest → SQS → scheduler; delivery dedupe; atomic quotas; requeue.
- [ ] Reconciler (adaptive cadence); lifecycle watcher; fast terminate on `completed`.
- [ ] Per-lease Scheduler deadline; reaper; inventory sweep; SNS alerting; kill switch; Budgets action.
- [ ] GitHub App connection model (private App per owner; personal + org; repo-level JIT).
- [ ] Forge/Compute/platform interfaces with the GitHub/EC2/AWS implementations.

### Phase 2 — Security contract
- [ ] Claim API (attestation, one-shot, SSM `pending/`), IMDS lock-down + `LOCKED` state, presigned logs, console capture.
- [ ] Go supervisor v1 (Linux) + `actions-runner` driver; digest-pinned in user data.
- [ ] Trust classifier + policy engine; per-actor quotas; untrusted network tier (SG + NACL + DNS Firewall).
- [ ] Cache endpoint (presigned, per-repo, read-only for untrusted).
- [ ] **Enable public repos** once the above passes an adversarial test suite (a job that tries to read IMDS, reach RFC1918, claim twice, write cache, survive past its deadline, etc.).

### Phase 3 — Windows, profiles, reporting basics
- [ ] Supervisor for Windows (replaces user data script + `bootstrap.ps1`); Windows hardening.
- [ ] Profile schema, validator, resolver, label grammar; EC2 Fleet with type lists, spot fallback, multi-AZ.
- [ ] Event stream → Parquet; cost estimate per lease; rollups.

### Phase 4 — Dashboard
- [ ] OIDC login (Cognito default), roles, per-connection grants, audit; v1 read-only pages.
- [ ] v2 operator/admin actions.

### Phase 5 — More forges
- [ ] Gitea (webhook + ephemeral act_runner driver), Forgejo (polling + ephemeral one-job driver), GitLab (job webhook + `run-single`).
- [ ] Trust classification per forge; recorded-fixture test suites.

### Phase 6 — Optional modules and alternative compute
- [ ] Baked images (Packer + catalog + retention); KVM profile on nested-virt families.
- [ ] Fargate backend (Linux light).
- [ ] Optional modules: GuardDuty, flow logs, Network Firewall egress allowlist, KMS-held App key, CUR reconciliation, SCPs for Organizations users.

### Phase 7 — macOS
- [ ] `aws-mac` backend: on-demand host allocation, Tart VM per job, idle release ≥24 h.

### Phase 8 — Other clouds
- [ ] `azure_vm`, `gcp_gce` via workload identity federation; attestation verifiers; network modules; inventory sweeps.

---

## 16. Decisions

| # | Decision | Status |
|---|---|---|
| D1 | IaC: **OpenTofu** | Decided |
| D2 | In-guest supervisor: **Go** single binary | Decided |
| D3 | **Near-zero idle cost** is a hard constraint alongside security; no NAT, warm pools, paid firewalls, or always-on instances by default; paid features are opt-in modules | Decided |
| D4 | **Public/forkable and private repos** are both in scope → per-job trust classification, untrusted tier | Decided |
| D5 | GitHub **personal accounts and orgs**, runners allocated dynamically per job (repo-level JIT by default; org runner groups optional) | Decided |
| D6 | **AWS Organizations not required**; standalone accounts + permissions boundaries + Budgets; SCPs as an optional module | Decided |
| D7 | Dashboard **multi-user from the start**: in-app OIDC, roles, per-connection grants, audit | Decided |
| D8 | **Build**, borrowing ideas from [github-aws-runners/terraform-aws-github-runner](https://github.com/github-aws-runners/terraform-aws-github-runner) and others | Decided |

Still open (not blocking Phase 0–1):

- **GitHub App pattern default:** private App per owner (current recommendation) or one public App with an allowlist.
- **Untrusted defaults:** should untrusted jobs be Linux-only, or is a small Windows profile acceptable for public repos?
- **Alert channel:** email via SNS only, or a webhook target (Slack/Discord/ntfy) in v1?
- **Region(s)** for the first deployment, and whether Graviton should be the default Linux arch.
- **New repo name / licence.**
