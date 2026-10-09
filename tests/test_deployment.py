"""Offline fixtures, not fresh provider evidence."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from messenger_recipe.comparison import differences
from messenger_recipe.configuration import render_assistant
from messenger_recipe.deployment import Deployment, DeploymentError, digest
from messenger_recipe.provider import ProviderError, VapiProvider
from messenger_recipe.state import InstanceState, RecipeError


INTENT = {"name": "Synthetic fixture", "artifactPlan": {"recordingEnabled": False,
          "structuredOutputIds": ["output-fixture"]}, "endCallPhrases": ["Have a great day."],
          "compliancePlan": {"hipaaEnabled": False}, "serverMessages": [],
          "model": {"fallbackModels": ["gpt-4.1-mini", "gpt-4.1"]}}


def fixture_state(directory) -> InstanceState:
    """Deployment state under a temporary package root."""
    return InstanceState(Path(directory), "fixture")


class ComparisonTests(unittest.TestCase):
    def test_json_numbers_compare_by_value_without_boolean_or_string_coercion(self):
        for requested, saved in ((1.0, 1), (1, 1.0), (0.0, 0), (0, 0.0)):
            with self.subTest(requested=requested, saved=saved):
                self.assertEqual(differences({"voice": {"speed": requested}},
                                             {"voice": {"speed": saved}}), [])
        for requested, saved in ((1.0, 1.01), (1.0, True), (0.0, False),
                                 (1.0, "1"), (False, 0), (True, 1.0)):
            with self.subTest(requested=requested, saved=saved):
                self.assertEqual(differences({"voice": {"speed": requested}},
                                             {"voice": {"speed": saved}}), ["voice.speed"])

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
    def test_validation_rejection_is_archived_without_automatic_retry(self):
        body = {"message": ["property recordingPlan should not exist"],
                "error": "Bad Request", "statusCode": 400}
        requests = []

        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(400, json=body)
            return httpx.Response(200, json=dict(copy.deepcopy(INTENT), id="fixture"))

        with tempfile.TemporaryDirectory() as directory:
            state = fixture_state(directory).directory
            with VapiProvider("offline-secret-never-saved",
                              transport=httpx.MockTransport(handler)) as provider:
                deployment = Deployment(provider, fixture_state(directory))
                with self.assertRaises(ProviderError) as raised:
                    deployment.ensure("assistant", INTENT)
                self.assertNotIn("recordingPlan", str(raised.exception))
                self.assertEqual(len(requests), 1)
                self.assertFalse((state / "assistant-pending.json").exists())
                self.assertFalse((state / "assistant-accepted.json").exists())
                rejected = list(state.glob("assistant-rejected-*.json"))
                self.assertEqual(len(rejected), 1)
                archive = json.loads(rejected[0].read_text())
                self.assertEqual(archive["intent"], INTENT)
                self.assertEqual(archive["status"], "validation-rejected")
                self.assertIsNone(archive["id"])
                self.assertEqual(rejected[0].stat().st_mode & 0o777, 0o600)
                deployment.ensure("assistant", INTENT)
            files = list(state.glob("evidence/assistant-error-*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(json.loads(files[0].read_text())["body"], body)
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
            self.assertNotIn("offline-secret-never-saved", files[0].read_text())
            self.assertIn(str(files[0]), str(raised.exception))
        self.assertEqual([request.method for request in requests], ["POST", "POST", "GET"])

    def test_uncertain_create_errors_keep_pending_blocker(self):
        validation = {"message": ["schema.type is invalid"], "error": "Bad Request", "statusCode": 400}
        for status, body in ((400, "Bad Request"), (400, {"message": "Unknown failure"}),
                             (400, dict(validation, id="possible-resource")),
                             (400, dict(validation, statusCode=422)),
                             (409, validation), (500, validation)):
            with self.subTest(status=status, body=body), tempfile.TemporaryDirectory() as directory:
                requests = []
                def handler(request):
                    requests.append(request)
                    return httpx.Response(status, json=body)
                with VapiProvider("offline-fixture", transport=httpx.MockTransport(handler)) as provider:
                    deployment = Deployment(provider, fixture_state(directory))
                    with self.assertRaises(ProviderError):
                        deployment.ensure("output", INTENT)
                    with self.assertRaises(DeploymentError):
                        deployment.ensure("output", INTENT)
                self.assertEqual(len(requests), 1)
                self.assertTrue((fixture_state(directory).directory / "output-pending.json").exists())
                self.assertFalse(list(fixture_state(directory).directory.glob("output-rejected-*.json")))

    def test_get_validation_error_after_successful_post_keeps_pending(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.method == "POST":
                return httpx.Response(200, json=dict(INTENT, id="fixture"))
            return httpx.Response(400, json={"message": ["schema.type is invalid"],
                                            "error": "Bad Request", "statusCode": 400})
        with tempfile.TemporaryDirectory() as directory:
            with VapiProvider("offline-fixture", transport=httpx.MockTransport(handler)) as provider:
                deployment = Deployment(provider, fixture_state(directory))
                with self.assertRaises(ProviderError):
                    deployment.ensure("output", INTENT)
                with self.assertRaises(DeploymentError):
                    deployment.ensure("output", INTENT)
            pending = json.loads((fixture_state(directory).directory / "output-pending.json").read_text())
            self.assertEqual(pending["id"], "fixture")
            self.assertFalse(list(fixture_state(directory).directory.glob("output-rejected-*.json")))
        self.assertEqual([request.method for request in requests], ["POST", "GET"])

    def test_validation_rejection_archive_failure_keeps_pending(self):
        def handler(request):
            return httpx.Response(422, json={"message": ["schema.type is invalid"],
                                            "error": "Unprocessable Entity", "statusCode": 422})
        with tempfile.TemporaryDirectory() as directory:
            state = fixture_state(directory).directory
            with VapiProvider("offline-fixture", transport=httpx.MockTransport(handler)) as provider:
                deployment = Deployment(provider, fixture_state(directory))
                original_replace = InstanceState.replace
                def fail_archive_record(instance, name, content):
                    if isinstance(content, dict) and content.get("status") == "validation-rejected":
                        raise OSError("Synthetic archive write failure")
                    original_replace(instance, name, content)
                with patch.object(InstanceState, "replace", autospec=True, side_effect=fail_archive_record):
                    with self.assertRaises(OSError):
                        deployment.ensure("output", INTENT)
                self.assertTrue((state / "output-pending.json").exists())
                self.assertFalse(list(state.glob("output-rejected-*.json")))
                self.assertEqual(len(list(state.glob("evidence/output-error-*.json"))), 1)
                with self.assertRaises(DeploymentError):
                    deployment.ensure("output", INTENT)

    def test_explicit_422_rejection_is_archived(self):
        def handler(request):
            return httpx.Response(422, json={"message": ["schema.type is invalid"],
                                            "error": "Unprocessable Entity", "statusCode": 422})
        with tempfile.TemporaryDirectory() as directory:
            state = fixture_state(directory).directory
            with VapiProvider("offline-fixture", transport=httpx.MockTransport(handler)) as provider:
                with self.assertRaises(ProviderError):
                    Deployment(provider, fixture_state(directory)).ensure("output", INTENT)
            self.assertFalse((state / "output-pending.json").exists())
            self.assertEqual(len(list(state.glob("output-rejected-*.json"))), 1)

    def test_validation_rejection_failed_archive_move_keeps_pending(self):
        def handler(request):
            return httpx.Response(400, json={"message": ["schema.type is invalid"],
                                            "error": "Bad Request", "statusCode": 400})
        with tempfile.TemporaryDirectory() as directory:
            state = fixture_state(directory).directory
            original_replace = os.replace
            def fail_archive_move(source, target):
                if Path(target).name.startswith("output-rejected-"):
                    raise OSError("Synthetic archive move failure")
                original_replace(source, target)
            with VapiProvider("offline-fixture", transport=httpx.MockTransport(handler)) as provider:
                deployment = Deployment(provider, fixture_state(directory))
                with patch("messenger_recipe.state.os.replace", side_effect=fail_archive_move):
                    with self.assertRaises(OSError):
                        deployment.ensure("output", INTENT)
                self.assertTrue((state / "output-pending.json").exists())
                self.assertFalse(list(state.glob("output-rejected-*.json")))
                with self.assertRaises(DeploymentError):
                    deployment.ensure("output", INTENT)

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
    def test_integer_voice_speed_readback_reconciles_existing_assistant_get_only(self):
        intent = dict(copy.deepcopy(INTENT), voice={"speed": 1.0})
        self.provider.live["voice"] = {"speed": 1}
        self.instance.replace("assistant-pending.json", {
            "id": "fixture", "status": "reconciliation-required",
            "payloadSha256": digest(intent)})
        result = self.deployment.ensure("assistant", intent, reconcile_id="fixture")
        self.assertEqual(result["voice"]["speed"], 1)
        self.assertEqual(self.provider.posts, 0)
        self.assertEqual(self.provider.patches, 0)
        self.assertFalse((self.state / "assistant-pending.json").exists())
        self.assertTrue((self.state / "assistant-accepted.json").exists())

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.instance = fixture_state(self.directory.name)
        self.state = self.instance.directory
        self.provider = FixtureProvider()
        self.deployment = Deployment(self.provider, self.instance)

    def test_create_verified_and_rerun_never_duplicates(self):
        self.deployment.ensure("assistant", INTENT)
        self.assertFalse((self.state / "assistant-pending.json").exists())
        accepted = self.state / "assistant-accepted.json"
        self.assertEqual(accepted.stat().st_mode & 0o777, 0o600)
        before = accepted.read_bytes()
        self.deployment.ensure("assistant", INTENT)
        self.assertEqual(accepted.read_bytes(), before)
        with self.assertRaisesRegex(DeploymentError, "binding conflicts"):
            self.deployment.ensure("assistant", dict(INTENT, name="changed"))
        self.assertEqual(self.provider.posts, 1)
        self.assertEqual(self.provider.patches, 0)

    def test_plan_is_read_only_and_refuses_invalid_recovery(self):
        self.assertEqual(self.deployment.plan("assistant"), "create")
        with self.assertRaisesRegex(DeploymentError, "No unresolved assistant"):
            self.deployment.plan("assistant", reconcile_id="fixture")
        with self.assertRaises(ValueError):
            self.deployment.plan("assistant", reconcile_id="../escape")
        self.deployment.ensure("assistant", INTENT)
        self.assertEqual(self.deployment.plan("assistant"), "check")
        self.assertEqual(self.deployment.plan("assistant", update=True), "update")
        self.instance.replace("assistant-pending.json", {"payloadSha256": digest(INTENT)})
        with self.assertRaisesRegex(DeploymentError, "Unresolved assistant creation"):
            self.deployment.plan("assistant")
        with self.assertRaisesRegex(DeploymentError, "both accepted and pending"):
            self.deployment.plan("assistant", reconcile_id="fixture")
        self.assertEqual((self.provider.posts, self.provider.patches), (1, 0))

    def test_ambiguous_create_blocks_retry_and_reconciles_get_only(self):
        for kind in ("assistant", "output"):
            with self.subTest(kind=kind):
                self.provider.fail_post = True
                with self.assertRaises(ProviderError):
                    self.deployment.ensure(kind, INTENT)
                with self.assertRaises(DeploymentError):
                    self.deployment.ensure(kind, INTENT)
                with self.assertRaises(DeploymentError):
                    self.deployment.ensure(kind, dict(INTENT, name="changed"), reconcile_id="fixture")
                self.deployment.ensure(kind, INTENT, reconcile_id="fixture")
        self.assertEqual(self.provider.posts, 2)
        self.assertEqual(self.provider.patches, 0)

    def test_failed_get_or_drift_not_accepted(self):
        self.provider.fail_get = True
        with self.assertRaises(ProviderError):
            self.deployment.ensure("assistant", INTENT)
        self.assertFalse((self.state / "assistant-accepted.json").exists())
        self.provider.fail_get = False
        self.provider.live["endCallPhrases"] = ["changed"]
        with self.assertRaises(DeploymentError):
            self.deployment.ensure("assistant", INTENT, reconcile_id="fixture")
        self.assertTrue((self.state / "assistant-pending.json").exists())

    def test_noop_and_remote_drift_protect_update(self):
        self.deployment.ensure("assistant", INTENT)
        self.deployment.ensure("assistant", INTENT, update=True)
        self.assertEqual(self.provider.patches, 0)
        previous = (self.state / "assistant-accepted.json").read_bytes()
        self.provider.live["artifactPlan"]["recordingEnabled"] = True
        with self.assertRaises(DeploymentError):
            self.deployment.ensure("assistant", dict(INTENT, name="new"), update=True)
        self.assertEqual(self.provider.patches, 0)
        self.assertEqual((self.state / "assistant-accepted.json").read_bytes(), previous)

    def test_explicit_clearing_and_attachment_preserved(self):
        before = copy.deepcopy(INTENT)
        before["serverMessages"] = ["end-of-call-report"]
        self.provider.live.update(copy.deepcopy(before))
        self.deployment.ensure("assistant", before)
        self.deployment.ensure("assistant", INTENT, update=True)
        self.assertEqual(self.provider.live["serverMessages"], [])
        self.assertEqual(self.provider.live["artifactPlan"], INTENT["artifactPlan"])

    def test_symlink_evidence_does_not_escape_local_state(self):
        outside = self.state / "outside"
        outside.mkdir(parents=True)
        (self.state / "evidence").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RecipeError):
            self.deployment.ensure("assistant", INTENT)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue((self.state / "assistant-pending.json").exists())
        self.assertFalse((self.state / "assistant-accepted.json").exists())


if __name__ == "__main__":
    unittest.main()
