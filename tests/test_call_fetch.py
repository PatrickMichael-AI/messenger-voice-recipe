"""Exact-call retrieval checks against synthetic provider fixtures."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from messenger import fetch_call, inspect_call
from messenger_recipe.deployment import DeploymentError


class FixtureProvider:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def get_call(self, call_id):
        self.requests.append(call_id)
        return copy.deepcopy(self.response)


class CallFetchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.slug = "fixture"
        state = self.root / ".local" / self.slug
        state.mkdir(parents=True)
        (state / "assistant-accepted.json").write_text(json.dumps({"id": "assistant-fixture"}))
        (state / "output-accepted.json").write_text(json.dumps({"id": "output-fixture"}))
        self.response = {"id": "call-fixture", "assistantId": "assistant-fixture",
                         "artifact": {"structuredOutputs": {"output-fixture": {"result": {"fixture": True}}}}}

    def test_fetches_exact_call_and_preserves_provider_payload(self):
        provider = FixtureProvider(self.response)
        path = fetch_call(self.root, self.slug, "call-fixture", provider)
        self.assertEqual(provider.requests, ["call-fixture"])
        self.assertEqual(json.loads(path.read_text()), self.response)
        self.assertTrue(path.is_relative_to(self.root / ".local"))

    def test_refuses_foreign_assistant_and_unsafe_id(self):
        foreign = dict(self.response, assistantId="foreign-fixture")
        with self.assertRaises(ValueError):
            fetch_call(self.root, self.slug, "call-fixture", FixtureProvider(foreign))
        provider = FixtureProvider(self.response)
        with self.assertRaises(ValueError):
            fetch_call(self.root, self.slug, "../../escape", provider)
        self.assertEqual(provider.requests, [])

    def test_missing_accepted_identity_prevents_request(self):
        provider = FixtureProvider(self.response)
        with self.assertRaisesRegex(DeploymentError, "No accepted assistant"):
            fetch_call(self.root, "unknown", "call-fixture", provider)
        self.assertEqual(provider.requests, [])

    def test_three_explicit_attempts_preserve_each_snapshot_and_reject_overwrites_before_get(self):
        provider = FixtureProvider(self.response)
        first = fetch_call(self.root, self.slug, "call-fixture", provider)
        original = first.read_bytes()
        provider.response["artifact"]["structuredOutputs"] = {}
        paths = [first]
        for attempt in (2, 3):
            paths.append(fetch_call(self.root, self.slug, "call-fixture", provider, attempt=attempt))
        self.assertEqual(first.name, "call-fixture.json")
        self.assertEqual([path.name for path in paths[1:]], ["call-fixture.attempt-2.json", "call-fixture.attempt-3.json"])
        self.assertEqual(first.read_bytes(), original)
        for attempt in (1, 2, 3, 0, 4, True, "2"):
            with self.subTest(attempt=attempt), self.assertRaises(ValueError):
                fetch_call(self.root, self.slug, "call-fixture", provider, attempt=attempt)
        self.assertEqual(provider.requests, ["call-fixture"] * 3)

    def test_refetch_rechecks_identity_and_does_not_save_foreign_payload(self):
        provider = FixtureProvider(self.response)
        first = fetch_call(self.root, self.slug, "call-fixture", provider)
        original = first.read_bytes()
        for field in ("id", "assistantId"):
            response = dict(self.response, **{field: "foreign-fixture"})
            with self.subTest(field=field), self.assertRaises(ValueError):
                fetch_call(self.root, self.slug, "call-fixture", FixtureProvider(response), attempt=2)
        self.assertEqual(first.read_bytes(), original)
        self.assertFalse((first.parent / "call-fixture.attempt-2.json").exists())

    def save(self, response, attempt=1):
        provider = FixtureProvider(response)
        return fetch_call(self.root, self.slug, "call-fixture", provider, attempt=attempt)

    def test_inspection_reports_valid_shape_without_semantic_claim_or_private_content(self):
        response = copy.deepcopy(self.response)
        result = {"schemaVersion": "1.0.0", "outcome": "no_message", "completeness": "not_applicable",
                  "callbackNumberConfirmed": False, "evidence": [], "uncertainties": []}
        response["artifact"]["structuredOutputs"]["output-fixture"] = {"result": result, "version": 7}
        response["transcript"] = "Private synthetic transcript"
        path = self.save(response, attempt=2)
        report = inspect_call(self.root, self.slug, "call-fixture", attempt=2)
        self.assertEqual(report["status"], "valid_shape_semantics_unproved")
        self.assertEqual(report["semanticValidity"], "operator_transcript_review_required")
        self.assertEqual(report["providerOutputMetadata"], {"version": 7})
        self.assertEqual(report["evidencePath"], str(path.relative_to(self.root)))
        self.assertNotIn("Private synthetic transcript", json.dumps(report))
        self.assertNotIn("result", report)

    def test_inspection_validates_provider_placeholders_without_rewriting_call(self):
        response = copy.deepcopy(self.response)
        result = {"schemaVersion": "1.0.0", "outcome": "declined", "completeness": "not_applicable",
                  "callbackNumberConfirmed": False, "callerName": "", "message": "", "callbackNumber": "",
                  "evidence": [], "uncertainties": []}
        response["artifact"]["structuredOutputs"]["output-fixture"] = {"result": result}
        path = self.save(response)
        original = path.read_bytes()
        report = inspect_call(self.root, self.slug, "call-fixture")
        self.assertEqual(report["status"], "valid_shape_semantics_unproved")
        self.assertEqual(path.read_bytes(), original)
        self.assertNotIn("result", report)

    def test_inspection_rejects_wrong_call_identity_and_invalid_attempt(self):
        path = self.save(self.response)
        for field in ("id", "assistantId"):
            path.write_text(json.dumps(dict(self.response, **{field: "foreign-fixture"})))
            with self.subTest(field=field), self.assertRaises(ValueError):
                inspect_call(self.root, self.slug, "call-fixture")
        with self.assertRaises(ValueError):
            inspect_call(self.root, self.slug, "call-fixture", attempt=4)


if __name__ == "__main__":
    unittest.main()
