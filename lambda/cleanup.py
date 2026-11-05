"""Runs every 15 minutes:
  - terminates runner instances older than MAX_RUN_MINUTES + 30 (their own shutdown timer failed)
  - deletes JIT config parameters older than 2 hours (instance never read them)
  - removes offline runner registrations named aws-* that have no instance any more
"""
import datetime
import os

import boto3

from common import GitHubError, env_list, get_secret, github, log, runner_instances, tag


def registered_minutes_ago(name, now):
    """Runner names are aws-<job id>-<unix time>; a registration younger than ~30 min may belong to
    an instance that's still being launched, so it's left alone."""
    try:
        created = datetime.datetime.fromtimestamp(int(name.rsplit("-", 1)[1]), datetime.timezone.utc)
    except (IndexError, ValueError):
        return float("inf")
    return (now - created).total_seconds() / 60


def handler(event, context):
    stack = os.environ["STACK_NAME"]
    now = datetime.datetime.now(datetime.timezone.utc)
    ec2 = boto3.client("ec2")
    ssm = boto3.client("ssm")

    # 1. Overdue instances.
    limit = datetime.timedelta(minutes=int(os.environ["MAX_RUN_MINUTES"]) + 30)
    live = runner_instances(ec2, stack)
    overdue = [i["InstanceId"] for i in live if now - i["LaunchTime"] > limit]
    if overdue:
        ec2.terminate_instances(InstanceIds=overdue)
        log("terminated overdue instances", instances=overdue)

    # 2. Stale JIT configs.
    prefix = os.environ["PARAM_PREFIX"]
    stale = []
    for page in ssm.get_paginator("get_parameters_by_path").paginate(Path=prefix, WithDecryption=False):
        for p in page["Parameters"]:
            if now - p["LastModifiedDate"] > datetime.timedelta(hours=2):
                stale.append(p["Name"])
    for name in stale:
        ssm.delete_parameter(Name=name)
    if stale:
        log("deleted stale JIT parameters", parameters=stale)

    # 3. Orphaned runner registrations (instance gone, runner offline and idle).
    names = {tag(i, "gh-runner") for i in live}
    token = get_secret(os.environ["TOKEN_SECRET_ARN"])
    for repo in env_list("ALLOWED_REPOS"):
        try:
            runners = github("GET", f"/repos/{repo}/actions/runners?per_page=100", token).get("runners", [])
        except GitHubError as e:
            log("couldn't list runners", repo=repo, error=str(e))
            continue
        for r in runners:
            if r["name"].startswith("aws-") and r["status"] == "offline" and not r["busy"] and r["name"] not in names \
                    and registered_minutes_ago(r["name"], now) > 30:
                try:
                    github("DELETE", f"/repos/{repo}/actions/runners/{r['id']}", token)
                    log("removed orphaned runner", repo=repo, runner=r["name"])
                except GitHubError as e:
                    log("couldn't remove runner", repo=repo, runner=r["name"], error=str(e))
    return {"overdue": overdue, "stale_parameters": len(stale)}
