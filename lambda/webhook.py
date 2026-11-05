"""GitHub workflow_job webhook -> one ephemeral EC2 runner per queued job.

For a 'queued' job from an allowed repository whose labels include RUNNER_LABEL:
  1. pick the instance type (an allowed type named in the labels, else DEFAULT_TYPE)
  2. register a one-time (JIT) runner with the job's labels
  3. store the JIT config in SSM (SecureString); the instance reads and deletes it at boot
  4. launch an instance from the launch template, tagged with the runner name

Every request must carry a valid X-Hub-Signature-256 for the webhook secret.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import time

import boto3

from common import env_list, get_secret, github, log, runner_instances

INSTANCE_TYPE_RE = re.compile(r"^[a-z][a-z0-9-]*\.[a-z0-9]+$")


def verify_signature(body: bytes, header: str, secret: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def plan_launch(payload, allowed_repos, runner_label, allowed_types, default_type):
    """Decides whether a workflow_job payload gets a runner. Returns (plan, reason).

    plan is None when the job is ignored; reason explains why. Pure function, unit-tested.
    """
    job = payload.get("workflow_job") or {}
    labels = job.get("labels") or []
    repo = (payload.get("repository") or {}).get("full_name", "")
    if payload.get("action") != "queued":
        return None, f"action {payload.get('action')}"
    if runner_label not in labels:
        return None, f"job doesn't ask for '{runner_label}'"
    if repo.lower() not in {r.lower() for r in allowed_repos}:
        return None, f"repository {repo} isn't allowed"

    requested = [l for l in labels if INSTANCE_TYPE_RE.match(l)]
    bad = [l for l in requested if l not in allowed_types]
    if bad:
        return None, f"instance type label(s) {bad} not in the allowed list {allowed_types}"
    if len(requested) > 1:
        return None, f"more than one instance type requested: {requested}"
    instance_type = requested[0] if requested else default_type

    # The runner is registered with exactly the job's labels, so it matches this job. A JIT runner
    # gets only the labels passed here: GitHub doesn't add self-hosted, the OS or the architecture.
    return {
        "repo": repo,
        "job_id": str(job.get("id")),
        "run_id": str(job.get("run_id")),
        "instance_type": instance_type,
        "labels": list(labels),
    }, "launch"


def response(status, message):
    return {"statusCode": status, "headers": {"Content-Type": "text/plain"}, "body": message}


def handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    raw = event.get("body") or ""
    body = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()

    if not verify_signature(body, headers.get("x-hub-signature-256", ""), get_secret(os.environ["WEBHOOK_SECRET_ARN"])):
        log("rejected: bad or missing signature")
        return response(401, "bad signature")

    kind = headers.get("x-github-event", "")
    if kind == "ping":
        return response(200, "pong")
    if kind != "workflow_job":
        return response(200, f"ignored event {kind}")

    payload = json.loads(body)
    plan, reason = plan_launch(
        payload,
        env_list("ALLOWED_REPOS"),
        os.environ["RUNNER_LABEL"],
        env_list("ALLOWED_TYPES"),
        os.environ["DEFAULT_TYPE"],
    )
    if not plan:
        log("ignored", reason=reason, job=(payload.get("workflow_job") or {}).get("id"))
        return response(200, f"ignored: {reason}")
    return launch(plan)


def launch(plan):
    stack = os.environ["STACK_NAME"]
    ec2 = boto3.client("ec2")
    ssm = boto3.client("ssm")

    # GitHub can redeliver a webhook: don't launch twice for one job.
    existing = [i for i in runner_instances(ec2, stack, ("pending", "running"))]
    if any(t["Key"] == "gh-job-id" and t["Value"] == plan["job_id"] for i in existing for t in i.get("Tags", [])):
        log("already launched", **plan)
        return response(200, "already launched")
    if len(existing) >= int(os.environ["MAX_INSTANCES"]):
        log("not launched: MAX_INSTANCES reached", running=len(existing), **plan)
        return response(200, "at MAX_INSTANCES; re-run the job later")

    token = get_secret(os.environ["TOKEN_SECRET_ARN"])
    name = f"aws-{plan['job_id']}-{int(time.time())}"
    jit = github("POST", f"/repos/{plan['repo']}/actions/runners/generate-jitconfig", token, {
        "name": name,
        "runner_group_id": 1,
        "labels": plan["labels"],
        "work_folder": "_work",
    })
    runner_id = jit["runner"]["id"]
    param = f"{os.environ['PARAM_PREFIX']}/{name}"
    try:
        # The JIT config is longer than the standard tier's 4 KB limit; Intelligent-Tiering uses the
        # advanced tier (8 KB) when needed. It lives only minutes (the instance deletes it), so the
        # advanced tier's $0.05/month, prorated, comes to a fraction of a cent per runner.
        ssm.put_parameter(Name=param, Value=jit["encoded_jit_config"], Type="SecureString",
                          Tier="Intelligent-Tiering", Overwrite=True)
        tags = [
            {"Key": "Name", "Value": name},
            {"Key": "gh-runner", "Value": name},
            {"Key": "gh-runner-stack", "Value": stack},
            {"Key": "gh-repo", "Value": plan["repo"]},
            {"Key": "gh-job-id", "Value": plan["job_id"]},
            {"Key": "gh-run-id", "Value": plan["run_id"]},
        ]
        result = ec2.run_instances(
            LaunchTemplate={"LaunchTemplateId": os.environ["LAUNCH_TEMPLATE_ID"], "Version": "$Latest"},
            InstanceType=plan["instance_type"],
            MinCount=1,
            MaxCount=1,
            TagSpecifications=[
                {"ResourceType": "instance", "Tags": tags},
                {"ResourceType": "volume", "Tags": tags},
            ],
        )
    except Exception:
        # Don't leave a registered runner or a stored config behind for an instance that never started.
        try:
            ssm.delete_parameter(Name=param)
        except Exception:
            pass
        try:
            github("DELETE", f"/repos/{plan['repo']}/actions/runners/{runner_id}", token)
        except Exception:
            pass
        raise
    instance_id = result["Instances"][0]["InstanceId"]
    log("launched", instance_id=instance_id, runner=name, **plan)
    return response(200, f"launched {instance_id} ({plan['instance_type']}) as {name}")
