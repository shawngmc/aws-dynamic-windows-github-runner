"""Unit tests for the pure parts of the Lambda code. Run: python3 -m unittest discover tests"""
import datetime
import hashlib
import hmac
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))
sys.modules.setdefault("boto3", types.ModuleType("boto3"))  # not needed by the functions under test

import cleanup  # noqa: E402
import webhook  # noqa: E402

ALLOWED = ["shawngmc/windows-offline-install-media-builder"]
TYPES = ["m6id.2xlarge", "c6id.2xlarge"]


def payload(labels, action="queued", repo=ALLOWED[0]):
    return {
        "action": action,
        "repository": {"full_name": repo},
        "workflow_job": {"id": 123, "run_id": 456, "labels": labels},
    }


def plan(p):
    return webhook.plan_launch(p, ALLOWED, "aws-windows", TYPES, "m6id.2xlarge")


class SignatureTests(unittest.TestCase):
    def test_valid(self):
        body = b'{"a":1}'
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertTrue(webhook.verify_signature(body, sig, "s3cret"))

    def test_wrong_secret_or_body(self):
        body = b'{"a":1}'
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertFalse(webhook.verify_signature(body, sig, "other"))
        self.assertFalse(webhook.verify_signature(b'{"a":2}', sig, "s3cret"))

    def test_missing_or_malformed(self):
        self.assertFalse(webhook.verify_signature(b"x", "", "s"))
        self.assertFalse(webhook.verify_signature(b"x", "sha1=abc", "s"))


class PlanTests(unittest.TestCase):
    def test_default_type(self):
        p, reason = plan(payload(["self-hosted", "aws-windows"]))
        self.assertEqual(reason, "launch")
        self.assertEqual(p["instance_type"], "m6id.2xlarge")
        self.assertEqual(p["labels"], ["aws-windows"])  # self-hosted is a system label
        self.assertEqual(p["job_id"], "123")

    def test_requested_type(self):
        p, _ = plan(payload(["self-hosted", "aws-windows", "c6id.2xlarge"]))
        self.assertEqual(p["instance_type"], "c6id.2xlarge")
        self.assertIn("c6id.2xlarge", p["labels"])

    def test_disallowed_type(self):
        p, reason = plan(payload(["self-hosted", "aws-windows", "p5.48xlarge"]))
        self.assertIsNone(p)
        self.assertIn("not in the allowed list", reason)

    def test_two_types(self):
        p, reason = plan(payload(["aws-windows", "m6id.2xlarge", "c6id.2xlarge"]))
        self.assertIsNone(p)
        self.assertIn("more than one", reason)

    def test_other_runner_label(self):
        p, reason = plan(payload(["self-hosted", "something-else"]))
        self.assertIsNone(p)

    def test_repo_not_allowed(self):
        p, reason = plan(payload(["aws-windows"], repo="someone/else"))
        self.assertIsNone(p)
        self.assertIn("isn't allowed", reason)

    def test_repo_case_insensitive(self):
        p, _ = plan(payload(["aws-windows"], repo=ALLOWED[0].upper()))
        self.assertIsNotNone(p)

    def test_not_queued(self):
        p, _ = plan(payload(["aws-windows"], action="completed"))
        self.assertIsNone(p)

    def test_system_labels_dropped(self):
        p, _ = plan(payload(["self-hosted", "Windows", "X64", "aws-windows"]))
        self.assertEqual(p["labels"], ["aws-windows"])


class CleanupTests(unittest.TestCase):
    def test_registration_age(self):
        now = datetime.datetime(2026, 11, 5, 12, 0, tzinfo=datetime.timezone.utc)
        ts = int((now - datetime.timedelta(minutes=45)).timestamp())
        self.assertAlmostEqual(cleanup.registered_minutes_ago(f"aws-123-{ts}", now), 45, places=3)
        self.assertEqual(cleanup.registered_minutes_ago("aws-odd", now), float("inf"))


if __name__ == "__main__":
    unittest.main()
