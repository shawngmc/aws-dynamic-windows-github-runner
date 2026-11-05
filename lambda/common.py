"""Shared helpers for the webhook and cleanup functions (standard library + boto3 only)."""
import json
import os
import time
import urllib.error
import urllib.request

import boto3

GITHUB_API = "https://api.github.com"
USER_AGENT = "aws-dynamic-github-runner"


_secret_cache = {}


def env_list(name):
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


def get_secret(arn, ttl=300):
    """Secrets Manager value, cached for a few minutes per Lambda container."""
    hit = _secret_cache.get(arn)
    if hit and hit[1] > time.time():
        return hit[0]
    value = boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"].strip()
    _secret_cache[arn] = (value, time.time() + ttl)
    return value


class GitHubError(Exception):
    def __init__(self, status, message):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


def github(method, path, token, body=None):
    """Calls the GitHub REST API; returns parsed JSON (or None for 204)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GITHUB_API + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
        **({"Content-Type": "application/json"} if data else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise GitHubError(e.code, detail) from None


def runner_instances(ec2, stack, states=("pending", "running", "stopping", "stopped")):
    """This stack's runner instances in the given states."""
    out = []
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(Filters=[
        {"Name": "tag:gh-runner-stack", "Values": [stack]},
        {"Name": "instance-state-name", "Values": list(states)},
    ]):
        for res in page["Reservations"]:
            out.extend(res["Instances"])
    return out


def tag(instance, key):
    for t in instance.get("Tags", []):
        if t["Key"] == key:
            return t["Value"]
    return None


def log(msg, **fields):
    print(json.dumps({"msg": msg, **fields}, default=str))
