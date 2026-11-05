<#
.SYNOPSIS
    Runs on each runner instance (started by the launch template's user data as a SYSTEM scheduled
    task). Prepares the machine, runs exactly one GitHub Actions job, then shuts the instance down,
    which terminates it.

.DESCRIPTION
    1. Reads its runner name from the instance tag gh-runner (instance metadata, IMDSv2).
    2. Formats any raw local NVMe disks as NTFS work drives.
    3. Optionally installs AWS CLI v2 (Authenticode-checked: signed by Amazon) and PowerShell 7
       (GitHub SHA256 and Microsoft signature checked), which workflows use for 'shell: pwsh'.
    4. Downloads the latest GitHub Actions runner, checked against the SHA256 digest GitHub publishes.
    5. Reads the one-time runner config from SSM (SecureString) and deletes it.
    6. Runs the runner. If no job starts within idleMinutes (the job was cancelled, or another
       runner took it), stops and shuts down instead of waiting for the hard lifetime.
    7. Uploads this log to s3://<bucket>/logs/<runner name>/ and shuts down.

    Settings come from C:\gh-runner\config.json, written by the user data. Edit this file and
    re-upload it with deploy.sh; no stack update is needed.
#>
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$log = 'C:\gh-runner\bootstrap.log'
Start-Transcript -Path $log -Append | Out-Null
$cfg = Get-Content -LiteralPath C:\gh-runner\config.json -Raw | ConvertFrom-Json
$name = 'unknown'

function Write-Step([string]$Message) { Write-Host ('{0:HH:mm:ss} {1}' -f (Get-Date), $Message) }

try {
    # 1. Runner name from the instance's tags.
    $token = Invoke-RestMethod -Method Put -Uri 'http://169.254.169.254/latest/api/token' `
        -Headers @{ 'X-aws-ec2-metadata-token-ttl-seconds' = '21600' }
    $md = @{ 'X-aws-ec2-metadata-token' = $token }
    $name = Invoke-RestMethod -Uri 'http://169.254.169.254/latest/meta-data/tags/instance/gh-runner' -Headers $md
    $instanceType = Invoke-RestMethod -Uri 'http://169.254.169.254/latest/meta-data/instance-type' -Headers $md
    Write-Step "Runner $name on $instanceType"

    # 2. Local NVMe (instance store) arrives raw: format it for the job's work files.
    foreach ($disk in Get-Disk | Where-Object PartitionStyle -eq 'RAW') {
        $vol = Initialize-Disk -Number $disk.Number -PartitionStyle GPT -PassThru |
            New-Partition -UseMaximumSize -AssignDriveLetter |
            Format-Volume -FileSystem NTFS -NewFileSystemLabel Work -Confirm:$false
        Write-Step ("Formatted disk {0} as {1}: ({2:N0} GB)" -f $disk.Number, $vol.DriveLetter, ($vol.Size / 1GB))
    }

    # 3. AWS CLI v2.
    if ($cfg.installAwsCli) {
        $msi = 'C:\gh-runner\AWSCLIV2.msi'
        Invoke-WebRequest -Uri 'https://awscli.amazonaws.com/AWSCLIV2.msi' -OutFile $msi -UseBasicParsing
        $sig = Get-AuthenticodeSignature -LiteralPath $msi
        if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'O=Amazon') {
            throw "AWS CLI installer signature: $($sig.Status) $($sig.SignerCertificate.Subject)"
        }
        $p = Start-Process msiexec.exe -ArgumentList '/i', $msi, '/qn', '/norestart' -Wait -PassThru
        if ($p.ExitCode -ne 0) { throw "AWS CLI install failed: msiexec exit $($p.ExitCode)" }
        $env:Path += ';C:\Program Files\Amazon\AWSCLIV2'
        Write-Step 'AWS CLI v2 installed'
    }

    # 3b. PowerShell 7 (workflows' 'shell: pwsh'; Windows Server only has Windows PowerShell 5.1).
    # On unless config.json says installPowerShell = false, so it needs no stack update.
    if ($null -eq $cfg.installPowerShell -or $cfg.installPowerShell) {
        $ps = Invoke-RestMethod -Uri 'https://api.github.com/repos/PowerShell/PowerShell/releases/latest' -Headers @{ 'User-Agent' = 'aws-dynamic-github-runner' }
        $psAsset = $ps.assets | Where-Object { $_.name -like 'PowerShell-*-win-x64.msi' } | Select-Object -First 1
        if (-not $psAsset) { throw "No win-x64 MSI in PowerShell release $($ps.tag_name)" }
        if ("$($psAsset.digest)" -notmatch '^sha256:([0-9a-fA-F]{64})$') { throw "No SHA256 digest published for $($psAsset.name)" }
        $psSha = $Matches[1].ToUpperInvariant()
        $psMsi = "C:\gh-runner\$($psAsset.name)"
        Invoke-WebRequest -Uri $psAsset.browser_download_url -OutFile $psMsi -UseBasicParsing
        if ((Get-FileHash -LiteralPath $psMsi -Algorithm SHA256).Hash -ne $psSha) { throw "$($psAsset.name) doesn't match GitHub's SHA256" }
        $sig = Get-AuthenticodeSignature -LiteralPath $psMsi
        if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
            throw "PowerShell installer signature: $($sig.Status) $($sig.SignerCertificate.Subject)"
        }
        $p = Start-Process msiexec.exe -Wait -PassThru -ArgumentList '/i', $psMsi, '/qn', '/norestart',
            'ADD_PATH=1', 'USE_MU=0', 'ENABLE_MU=0', 'ENABLE_PSREMOTING=0', 'ADD_EXPLORER_CONTEXT_MENU_OPENPOWERSHELL=0'
        if ($p.ExitCode -ne 0) { throw "PowerShell install failed: msiexec exit $($p.ExitCode)" }
        $env:Path += ';C:\Program Files\PowerShell\7'
        Write-Step "PowerShell $($ps.tag_name) installed"
    }

    # 4. GitHub Actions runner, verified against GitHub's published digest.
    $dir = 'C:\actions-runner'
    $release = Invoke-RestMethod -Uri 'https://api.github.com/repos/actions/runner/releases/latest' -Headers @{ 'User-Agent' = 'aws-dynamic-github-runner' }
    $asset = $release.assets | Where-Object { $_.name -like 'actions-runner-win-x64-*.zip' } | Select-Object -First 1
    if (-not $asset) { throw "No win-x64 asset in runner release $($release.tag_name)" }
    if ("$($asset.digest)" -notmatch '^sha256:([0-9a-fA-F]{64})$') { throw "No SHA256 digest published for $($asset.name)" }
    $expected = $Matches[1].ToUpperInvariant()
    $zip = "C:\gh-runner\$($asset.name)"
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -UseBasicParsing
    if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ne $expected) { throw "$($asset.name) doesn't match GitHub's SHA256" }
    Expand-Archive -LiteralPath $zip -DestinationPath $dir -Force
    Write-Step "Runner $($release.tag_name) unpacked"

    # 5. One-time runner config.
    Import-Module AWSPowerShell
    $param = "$($cfg.paramPrefix)/$name"
    $jit = (Get-SSMParameter -Name $param -WithDecryption $true -Region $cfg.region).Value
    Remove-SSMParameter -Name $param -Region $cfg.region -Force
    Write-Step 'JIT config read (parameter deleted)'

    # Visible to the job: where it can keep caches.
    [Environment]::SetEnvironmentVariable('RUNNER_CACHE_BUCKET', $cfg.bucket, 'Machine')
    [Environment]::SetEnvironmentVariable('AWS_REGION', $cfg.region, 'Machine')
    $env:RUNNER_CACHE_BUCKET = $cfg.bucket
    $env:AWS_REGION = $cfg.region

    # 6. Run one job, with an idle timeout until it starts.
    $runner = Start-Process -FilePath "$dir\run.cmd" -ArgumentList '--jitconfig', $jit -WorkingDirectory $dir -PassThru -NoNewWindow
    $deadline = (Get-Date).AddMinutes([int]$cfg.idleMinutes)
    $started = $false
    while (-not $runner.HasExited) {
        if (-not $started -and (Get-Process -Name Runner.Worker -ErrorAction SilentlyContinue)) {
            $started = $true
            Write-Step 'Job started'
        }
        if (-not $started -and (Get-Date) -gt $deadline) {
            Write-Step "No job within $($cfg.idleMinutes) min (cancelled, or taken by another runner); stopping"
            Get-Process -Name Runner.Listener, Runner.Worker -ErrorAction SilentlyContinue | Stop-Process -Force
            break
        }
        Start-Sleep -Seconds 10
    }
    Write-Step "Runner finished (job started: $started)"
} catch {
    Write-Warning "Bootstrap failed: $_"
} finally {
    # Nothing here may stop the shutdown below.
    $ErrorActionPreference = 'Continue'
    Stop-Transcript | Out-Null
    try {
        Write-S3Object -BucketName $cfg.bucket -Key "logs/$name/bootstrap-$(Get-Date -Format yyyyMMdd-HHmmss).log" `
            -File $log -Region $cfg.region | Out-Null
    } catch { }
    shutdown.exe /a                                   # cancel the lifetime timer set by the user data
    shutdown.exe /s /t 5 /f /c 'gh-runner done'       # ...and shut down now (the instance terminates)
}
