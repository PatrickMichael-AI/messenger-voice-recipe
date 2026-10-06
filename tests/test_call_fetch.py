"""Exact-call retrieval checks against synthetic provider fixtures."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from messenger import fetch_call


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
        with self.assertRaises(OSError):
            fetch_call(self.root, "unknown", "call-fixture", provider)
        self.assertEqual(provider.requests, [])


if __name__ == "__main__":
    unittest.main()
