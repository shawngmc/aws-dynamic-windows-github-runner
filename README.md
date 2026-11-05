# aws-dynamic-github-runner

Ephemeral **Windows EC2 runners for GitHub Actions**, started on demand by a webhook. Each queued job that asks for one gets its own fresh instance; the instance runs that one job and terminates. **No GitHub-hosted minutes are used**, and nothing runs in AWS while there are no jobs, apart from a cleanup Lambda every 15 minutes.

```
GitHub ──workflow_job "queued"──▶ Lambda function URL (webhook)
                                   ├─ checks the signature, the repository, the labels and the instance type
                                   ├─ registers a one-time (JIT) runner for the job's labels
                                   ├─ stores its config in SSM Parameter Store (SecureString)
                                   └─ launches an instance from the launch template
EC2 instance (Windows Server 2025) ── user data: lifetime timer, then bootstrap.ps1 as a SYSTEM task
   ├─ formats local NVMe, installs AWS CLI v2 and the latest Actions runner (digest-checked)
   ├─ reads and deletes its JIT config, runs ONE job (gives up if none starts within IdleMinutes)
   └─ uploads its log to S3, shuts down → terminated
Cleanup Lambda (every 15 min): overdue instances, stale JIT configs, orphaned runner registrations
```

## Use it in a workflow

```yaml
jobs:
  build:
    runs-on: [self-hosted, aws-windows]                 # default instance type
    # runs-on: [self-hosted, aws-windows, c6id.2xlarge] # or pick an allowed type
```

- **The job runs as SYSTEM**, which has administrator rights.
- **Installed at boot:** PowerShell 7 (`shell: pwsh` works) and AWS CLI v2. Git isn't installed; `actions/checkout` then downloads the repository through the GitHub API instead.
- **Local NVMe** (`d` instance types) is formatted as an extra NTFS drive. Use the biggest drive for work files.
- **`RUNNER_CACHE_BUCKET`** and `AWS_REGION` are set for the job, and the instance can read and write `s3://$RUNNER_CACHE_BUCKET/cache/`. For example, to keep downloads between runs:

```yaml
      - name: Restore cache
        run: aws s3 sync "s3://$env:RUNNER_CACHE_BUCKET/cache/my-build/" D:\cache --only-show-errors
      # ... build ...
      - name: Save cache
        if: always()
        run: aws s3 sync D:\cache "s3://$env:RUNNER_CACHE_BUCKET/cache/my-build/" --only-show-errors
```

Objects under `cache/` expire after 120 days, and logs after 30.

## Set up

### 1. A GitHub token

Create a **fine-grained personal access token**: GitHub → Settings → Developer settings → Fine-grained tokens.
- **Repository access:** only the repositories that will use these runners.
- **Permissions:** **Administration: Read and write**. This lets the Lambda register and remove runners. Metadata: Read is added automatically.
- **Expiry:** the longest you're comfortable with. When the token expires, jobs stay queued and the webhook Lambda logs `GitHub API 401`.

### 2. Deploy the stack

The simplest place is **AWS CloudShell**, which has the AWS CLI and bash:

```bash
git clone https://github.com/shawngmc/aws-dynamic-github-runner
cd aws-dynamic-github-runner
./deploy.sh VpcId=vpc-xxxxxxxx SubnetId=subnet-xxxxxxxx \
  AllowedRepositories=shawngmc/windows-offline-install-media-builder
```

- **Subnet:** the subnet needs outbound internet. A public subnet of the default VPC works with the default `AssignPublicIp=true`. The instances accept no inbound traffic.
- **What `deploy.sh` does:** creates an artifacts bucket for the Lambda code (if missing), packages and deploys `template.yaml` (stack name `gh-runner`, or `STACK_NAME`), uploads `runner/bootstrap.ps1` to the runner bucket, and prints the next steps.
- **Re-running:** run it again after any change. Parameters you don't pass keep their values.

### 3. Store the token

Use the command `deploy.sh` printed:

```bash
aws secretsmanager put-secret-value --secret-id <GitHubTokenSecretArn> --secret-string 'github_pat_...'
```

### 4. Add the webhook to each repository

In the repository: Settings → Webhooks → Add webhook.
- **Payload URL:** the `WebhookUrl` that `deploy.sh` printed.
- **Content type:** `application/json`.
- **Secret:** the output of the `get-secret-value` command that `deploy.sh` printed.
- **Events:** "Let me select individual events", then **Workflow jobs** only.

GitHub sends a `ping`, which should show a green tick under Recent Deliveries. Also add the repository to `AllowedRepositories`.

## Parameters

| Parameter | Default | |
|---|---|---|
| `VpcId`, `SubnetId` | (required) | Where instances run |
| `AssignPublicIp` | `true` | Needed without a NAT gateway |
| `AllowedRepositories` | (required) | Jobs from other repositories are ignored |
| `RunnerLabel` | `aws-windows` | The label a job must request |
| `AllowedInstanceTypes` | `m6id.2xlarge,c6id.2xlarge,m6id.4xlarge,m6i.2xlarge` | Types a job may name as a label; any other type label is refused |
| `DefaultInstanceType` | `m6id.2xlarge` | When the job names no type |
| `MaxInstances` | `2` | Most instances at once. A job over the limit isn't launched; re-run it later. |
| `MaxRunMinutes` | `420` | Hard lifetime; the instance powers off whatever it's doing |
| `IdleMinutes` | `20` | Shut down if no job starts this long after the runner is up |
| `RootVolumeSizeGiB` | `100` | `C:` size (gp3, encrypted) |
| `WindowsAmi` | Windows Server 2025 Full Base (SSM public parameter) | Resolved to the latest image on each stack update |
| `InstallAwsCli` | `true` | AWS CLI v2, signature-checked (signed by Amazon) |

## Cost

You pay for the instance while it runs (boot, about 2–3 minutes of setup, the job, shutdown), its gp3 volume, and S3 storage. These are approximate on-demand Windows prices in `us-east-1`; check current pricing for your region:

| Type | vCPU / RAM / local NVMe | Per hour | 2½-hour job |
|---|---|---|---|
| `c6id.2xlarge` | 8 / 16 GiB / 474 GB | about $0.77 | about $1.90 |
| `m6id.2xlarge` | 8 / 32 GiB / 474 GB | about $0.84 | about $2.10 |
| `m6id.4xlarge` | 16 / 64 GiB / 950 GB | about $1.70 | about $4.25 |

Lambda, Secrets Manager (two secrets, about $0.80/month) and the cleanup schedule cost cents a month.

## Security

- **Webhook requests:** every request must carry a valid `X-Hub-Signature-256` for the generated webhook secret; anything else gets a 401.
- **What can launch an instance:** only allowed repositories, only allowed instance types, at most `MaxInstances` at once, and only from the stack's launch template. The Lambda's IAM permissions can't launch anything else.
- **The one-time runner config:** passed through SSM as a SecureString. The instance deletes it on reading, and anything unread is deleted after 2 hours.
- **Instances:** no inbound access, IMDSv2 only, encrypted volumes. The instance role can only read its own config, read the bootstrap script and use `cache/` and `logs/` in the runner bucket. Session Manager is allowed, for debugging.
- **Downloads:** the runner zip must match the SHA-256 GitHub publishes, and the AWS CLI installer must be validly signed by Amazon.
- **Lifetime limits:** every instance has a hard lifetime set by its user data before anything else runs. The cleanup Lambda terminates any that outlive it.
- **Rejected:** the function URL is public (GitHub can't sign AWS requests), so anything without the right signature is rejected.

## Changing what runs at boot

The launch template only holds a short user-data script. It sets the lifetime timer, writes `C:\gh-runner\config.json` and downloads **`runner/bootstrap.ps1` from S3 at every boot**. To change the bootstrap, edit `runner/bootstrap.ps1` and upload it again; no stack update is needed:

```bash
./deploy.sh        # no parameters: the stack is left as it is, and bootstrap.ps1 is re-uploaded
```

Only changes to `template.yaml` (parameters, IAM, the launch template) need a stack update.

## Troubleshooting

| Symptom | Where to look |
|---|---|
| Job stays queued, no instance | Repository → Settings → Webhooks → Recent Deliveries (response body), and CloudWatch log group `/aws/lambda/<stack>-webhook` |
| Instance starts, job never runs | `s3://<RunnerBucket>/logs/<runner name>/bootstrap-*.log`. While it's up: Session Manager, then `C:\gh-runner\*.log` |
| Leftover instance or runner registration | `/aws/lambda/<stack>-cleanup` logs (it runs every 15 minutes) |

## Limitations

- **Boot time:** Windows boots slowly. Expect about 5–8 minutes from queueing to the job starting.
- **No queueing:** a job queued while `MaxInstances` instances are running isn't retried; re-run it.
- **Windows only:** x64 Windows only, for now.

## Development

```bash
python3 -m unittest discover tests       # Lambda logic: signatures, label and type checks
cfn-lint template.yaml                   # W3002 (local Code path) is expected: deploy.sh packages it
```
