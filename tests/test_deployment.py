"""Offline fixtures, not fresh provider evidence."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from messenger_recipe.comparison import differences
from messenger_recipe.configuration import render_assistant
from messenger_recipe.deployment import Deployment, DeploymentError, write_json
from messenger_recipe.provider import ProviderError, VapiProvider


INTENT = {"name": "Synthetic fixture", "artifactPlan": {"recordingEnabled": False,
          "structuredOutputIds": ["output-fixture"]}, "endCallPhrases": ["Have a great day."],
          "compliancePlan": {"hipaaEnabled": False}, "serverMessages": [],
          "model": {"fallbackModels": ["gpt-4.1-mini", "gpt-4.1"]}}


class ComparisonTests(unittest.TestCase):
    def test_defaults_and_known_empty_normalization(self):
        actual = copy.deepcopy(INTENT)
        actual.pop("serverMessages")
        actual.update(id="fixture", latestVersion="v99", createdAt="fixture", recordingEnabled=False)
        actual["model"]["temperature"] = 0.7
        self.assertEqual(differences(INTENT, actual), [])

    def test_owned_drift_missing_and_order_preserved(self):
        for field, value in (("artifactPlan", {}), ("endCallPhrases", ["Bye"]),
                             ("model", {"fallbackModels": ["gpt-4.1", "gpt-4.1-mini"]}),
                             ("serverMessages", ["end-of-call-report"])):
            with self.subTest(field=field):
                actual = copy.deepcopy(INTENT)
                actual[field] = value
                self.assertTrue(differences(INTENT, actual))
        actual = copy.deepcopy(INTENT)
        actual["recordingEnabled"] = True
        self.assertIn("recordingEnabled", differences(INTENT, actual))

    def test_null_and_missing_are_distinct(self):
        self.assertEqual(differences({"server": None}, {"server": None}), [])
        self.assertEqual(differences({"server": None}, {}), ["server"])
        self.assertTrue(differences({"enabled": False}, {"enabled": 0}))

    def test_schema_required_order_only(self):
        intent = {"schema": {"required": ["name", "message"], "additionalProperties": False}}
        self.assertEqual(differences(intent, {"schema": {"required": ["message", "name"],
                                                       "additionalProperties": False}}), [])
        self.assertTrue(differences(intent, {"schema": {"required": ["name", "message"]}}))


class TransportTests(unittest.TestCase):
    def test_validation_details_stay_private_and_failed_create_is_not_retried(self):
        body = {"message": ["property recordingPlan should not exist"],
                "statusCode": 400}
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(400, json=body)

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with VapiProvider("offline-secret-never-saved",
                              transport=httpx.MockTransport(handler)) as provider:
                deployment = Deployment(provider, state)
                with self.assertRaises(ProviderError) as raised:
                    deployment.create("assistant", INTENT)
                self.assertNotIn("recordingPlan", str(raised.exception))
                with self.assertRaises(DeploymentError):
                    deployment.create("assistant", INTENT)
            files = list(state.glob("evidence/assistant-error-*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(json.loads(files[0].read_text())["body"], body)
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
            self.assertNotIn("offline-secret-never-saved", files[0].read_text())
        self.assertEqual(len(requests), 1)

    def test_documented_paths_lossless_body_and_response(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"id": "fixture", "unknownFutureField": None})
        with VapiProvider("offline-fixture", transport=httpx.MockTransport(handler)) as provider:
            payload = {"server": None, "serverMessages": [], "artifactPlan": {"recordingEnabled": False}}
            for result in (provider.create_assistant(payload), provider.update_assistant("fixture", payload),
                           provider.get_assistant("fixture"), provider.create_output({"name": "fixture"}),
                           provider.get_output("fixture"), provider.get_call("fixture")):
                self.assertIn("unknownFutureField", result)
        self.assertEqual([(r.method, r.url.path) for r in requests], [
            ("POST", "/assistant"), ("PATCH", "/assistant/fixture"), ("GET", "/assistant/fixture"),
            ("POST", "/structured-output"), ("GET", "/structured-output/fixture"), ("GET", "/call/fixture")])
        self.assertEqual(json.loads(requests[1].content), payload)

    def test_no_post_retry(self):
        attempts = []
        def timeout(request):
            attempts.append(request)
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        with VapiProvider("offline-fixture", transport=httpx.MockTransport(timeout)) as provider:
            with self.assertRaises(ProviderError):
                provider.create_assistant(INTENT)
        self.assertEqual(len(attempts), 1)

    def test_full_runtime_survives_post_and_patch_without_field_loss(self):
        # Package runtime includes dashboard fields absent from the request
        # reference. This proves transport fidelity, not provider acceptance.
        path = Path(__file__).resolve().parents[1] / "config" / "runtime.json"
        runtime = json.loads(path.read_text(encoding="utf-8"))
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"id": "fixture"})

        with VapiProvider("offline-fixture", transport=httpx.MockTransport(handler)) as provider:
            provider.create_assistant(runtime)
            provider.update_assistant("fixture", runtime)
        self.assertEqual([request.method for request in requests], ["POST", "PATCH"])
        for request in requests:
            with self.subTest(method=request.method):
                self.assertEqual(json.loads(request.content), runtime)

    def test_native_output_schema_and_short_call_condition_survive_transport(self):
        # Synthetic extraction fixture, not an approved output definition.
        payload = {
            "name": "Synthetic enquiry",
            "schema": {
                "type": "object",
                "properties": {"message": {"type": ["string", "null"]},
                               "confirmed": {"type": "boolean"}},
                "required": ["message", "confirmed"],
                "additionalProperties": False,
                "allOf": [{"if": {"type": "object", "properties": {
                    "confirmed": {"const": True}}}, "then": {"type": "object",
                    "properties": {"message": {"type": "string"}}}}],
            },
            "conditions": [{"type": "minMessages", "count": 0}],
        }
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=dict(payload, id="fixture", futureField=None))

        with VapiProvider("offline-fixture", transport=httpx.MockTransport(handler)) as provider:
            result = provider.create_output(payload)
        self.assertEqual(json.loads(requests[0].content), payload)
        self.assertEqual(result, dict(payload, id="fixture", futureField=None))


class ConfigurationTests(unittest.TestCase):
    def test_render_preserves_controls_and_explicit_clears(self):
        runtime = copy.deepcopy(INTENT)
        runtime["voice"] = {"provider": "11labs", "speed": 1.0}
        voice = {"provider": "11labs", "voiceId": "fixture-voice", "model": "eleven_flash_v2_5"}
        result = render_assistant(runtime, name="Fixture", prompt="Fixture prompt", opening="Hello",
                                  voice=voice, output_ids=[])
        self.assertEqual(result["artifactPlan"], {"recordingEnabled": False, "structuredOutputIds": []})
        self.assertEqual(result["serverMessages"], [])
        self.assertEqual(result["compliancePlan"], INTENT["compliancePlan"])
        self.assertEqual(result["endCallPhrases"], INTENT["endCallPhrases"])
        self.assertEqual(result["model"]["fallbackModels"], INTENT["model"]["fallbackModels"])
        self.assertEqual(runtime["artifactPlan"], INTENT["artifactPlan"])

    def test_rejected_provider_fields_and_missing_voice(self):
        for forbidden in ("recordingPlan", "endCallPlan", "id", "latestVersion"):
            with self.subTest(forbidden=forbidden), self.assertRaises(ValueError):
                render_assistant(dict(INTENT, **{forbidden: {}}), name="Fixture", prompt="Fixture",
                                 opening="Hello", voice={})


class FixtureProvider:
    def __init__(self):
        self.live = dict(copy.deepcopy(INTENT), id="fixture")
        self.posts = 0
        self.patches = 0
        self.fail_post = False
        self.fail_get = False

    def create_assistant(self, payload):
        self.posts += 1
        if self.fail_post:
            raise ProviderError("synthetic uncertain POST")
        return copy.deepcopy(self.live)

    create_output = create_assistant

    def get_assistant(self, identity):
        if self.fail_get:
            raise ProviderError("synthetic unavailable GET")
        return copy.deepcopy(self.live)

    get_output = get_assistant

    def update_assistant(self, identity, payload):
        self.patches += 1
        self.live.update(copy.deepcopy(payload))
        return copy.deepcopy(self.live)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.provider = FixtureProvider()
        self.deployment = Deployment(self.provider, self.state)

    def test_create_verified_and_duplicate_refused(self):
        self.deployment.create("assistant", INTENT)
        self.assertFalse((self.state / "assistant-pending.json").exists())
        self.assertEqual((self.state / "assistant-accepted.json").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(DeploymentError):
            self.deployment.create("assistant", INTENT)
        self.assertEqual(self.provider.posts, 1)

    def test_ambiguous_create_blocks_retry_and_reconciles_get_only(self):
        for kind in ("assistant", "output"):
            with self.subTest(kind=kind):
                self.provider.fail_post = True
                with self.assertRaises(ProviderError):
                    self.deployment.create(kind, INTENT)
                with self.assertRaises(DeploymentError):
                    self.deployment.create(kind, INTENT)
                with self.assertRaises(DeploymentError):
                    self.deployment.reconcile(kind, "fixture", dict(INTENT, name="changed"))
                self.deployment.reconcile(kind, "fixture", INTENT)
        self.assertEqual(self.provider.posts, 2)
        self.assertEqual(self.provider.patches, 0)

    def test_failed_get_or_drift_not_accepted(self):
        self.provider.fail_get = True
        with self.assertRaises(ProviderError):
            self.deployment.create("assistant", INTENT)
        self.assertFalse((self.state / "assistant-accepted.json").exists())
        self.provider.fail_get = False
        self.provider.live["endCallPhrases"] = ["changed"]
        with self.assertRaises(DeploymentError):
            self.deployment.reconcile("assistant", "fixture", INTENT)
        self.assertTrue((self.state / "assistant-pending.json").exists())

    def test_noop_and_remote_drift_protect_update(self):
        self.deployment.create("assistant", INTENT)
        self.deployment.update_assistant(INTENT)
        self.assertEqual(self.provider.patches, 0)
        previous = (self.state / "assistant-accepted.json").read_bytes()
        self.provider.live["artifactPlan"]["recordingEnabled"] = True
        with self.assertRaises(DeploymentError):
            self.deployment.update_assistant(dict(INTENT, name="new"))
        self.assertEqual(self.provider.patches, 0)
        self.assertEqual((self.state / "assistant-accepted.json").read_bytes(), previous)

    def test_explicit_clearing_and_attachment_preserved(self):
        before = copy.deepcopy(INTENT)
        before["serverMessages"] = ["end-of-call-report"]
        self.provider.live.update(copy.deepcopy(before))
        self.deployment.create("assistant", before)
        self.deployment.update_assistant(INTENT)
        self.assertEqual(self.provider.live["serverMessages"], [])
        self.assertEqual(self.provider.live["artifactPlan"], INTENT["artifactPlan"])

    def test_symlink_evidence_does_not_escape_local_state(self):
        outside = self.state / "outside"
        outside.mkdir()
        (self.state / "evidence").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(DeploymentError):
            self.deployment.create("assistant", INTENT)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue((self.state / "assistant-pending.json").exists())
        self.assertFalse((self.state / "assistant-accepted.json").exists())

    def test_private_write_rejects_file_symlink(self):
        target = self.state / "original.json"
        target.write_text("Preserved\n")
        link = self.state / "linked.json"
        link.symlink_to(target)
        with self.assertRaises(DeploymentError):
            write_json(link, {"overwrite": True})
        self.assertEqual(target.read_text(), "Preserved\n")


if __name__ == "__main__":
    unittest.main()
