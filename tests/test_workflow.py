"""End-to-end package orchestration with synthetic provider responses only."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from messenger_recipe.deployment import DeploymentError
from messenger_recipe.recipe import RecipeError, record_voice_selection, stage_candidate
from messenger_recipe.workflow import deploy, deployment_preview, promote, require_promotion


PACKAGE = Path(__file__).resolve().parents[1]
SLUG = "messenger_recipe_demo"


class Provider:
    def __init__(self):
        self.calls = []
        self.resources = {}
        self.fail_after_create = None

    def _create(self, kind, payload):
        self.calls.append(("post", kind, deepcopy(payload)))
        identity = f"test-{kind}-id"
        response = {**deepcopy(payload), "id": identity, "providerDefault": "retained"}
        self.resources[identity] = response
        if self.fail_after_create == kind:
            self.fail_after_create = None
            raise TimeoutError("Synthetic response was lost after provider creation")
        return deepcopy(response)

    def _get(self, kind, identity):
        self.calls.append(("get", kind, identity))
        return deepcopy(self.resources[identity])

    def create_output(self, payload):
        return self._create("output", payload)

    def get_output(self, identity):
        return self._get("output", identity)

    def create_assistant(self, payload):
        return self._create("assistant", payload)

    def get_assistant(self, identity):
        return self._get("assistant", identity)

    def update_assistant(self, identity, payload):
        self.calls.append(("patch", "assistant", deepcopy(payload)))
        self.resources[identity] = {**deepcopy(payload), "id": identity, "providerDefault": "retained"}
        return deepcopy(self.resources[identity])


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "standalone"
        self.root.mkdir()
        for directory in ("templates", "config", "instances", "result"):
            shutil.copytree(PACKAGE / directory, self.root / directory)
        record_voice_selection(self.root, SLUG, "Synthetic exact voice approval")
        stage_candidate(self.root, SLUG)
        self.provider = Provider()

    def approve(self):
        return promote(self.root, SLUG, "Synthetic operator local promotion; no live authorization")

    def mutate(self, relative, update):
        path = self.root / relative
        value = json.loads(path.read_text())
        update(value)
        path.write_text(json.dumps(value, indent=2) + "\n")

    def test_preview_is_complete_unbound_and_does_not_grant_promotion(self):
        preview = deployment_preview(self.root, SLUG)
        self.assertEqual(preview["bindingStatus"], "provisional_unbound")
        self.assertNotIn("structuredOutputIds", preview["assistantCandidate"]["artifactPlan"])
        self.assertEqual(preview["outputDefinition"]["conditions"], [{"type": "minMessages", "count": 0}])
        self.assertEqual(preview["outputReviewStatus"], "operator_live_review_required")
        self.assertEqual(preview["promotionStatus"], "pending_or_stale")
        with self.assertRaises(RecipeError):
            deploy(self.root, SLUG, self.provider)
        self.assertEqual(self.provider.calls, [])
        self.assertFalse((self.root / ".local" / SLUG / "assistant-accepted.json").exists())

    def test_clone_promotes_its_own_review_without_historical_baseline(self):
        self.assertFalse((self.root / ".local" / SLUG / "baseline-review.json").exists())
        result = self.approve()
        self.assertFalse(result["liveAuthorized"])
        self.assertEqual(deployment_preview(self.root, SLUG)["promotionStatus"], "approved_current_sources")
        canonical = self.root / ".local" / SLUG / "prompts" / f"{SLUG}_vapi_system_prompt.txt"
        staged = self.root / ".local" / SLUG / "prompts/backups" / f"{SLUG}_vapi_system_prompt_refactor.txt"
        self.assertEqual(canonical.read_bytes(), staged.read_bytes())
        self.assertEqual(canonical.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.provider.calls, [])

    def test_two_live_executions_create_once_verify_output_and_preserve_all_settings(self):
        self.approve()
        original = deployment_preview(self.root, SLUG)["assistantCandidate"]
        first = deploy(self.root, SLUG, self.provider)
        second = deploy(self.root, SLUG, self.provider)
        self.assertEqual(first, second)
        self.assertEqual([call[:2] for call in self.provider.calls], [
            ("post", "output"), ("get", "output"), ("post", "assistant"),
            ("get", "assistant"), ("get", "output"), ("get", "assistant")])
        actual = self.provider.resources[first["assistantId"]]
        for key, value in original.items():
            if key != "artifactPlan":
                self.assertEqual(actual[key], value)
        self.assertEqual(actual["artifactPlan"], {**original["artifactPlan"],
                                                 "structuredOutputIds": [first["outputId"]]})
        self.assertEqual(first["publicationStatus"], "operator_dashboard_review_required")
        self.assertFalse((self.root / "agents").exists())

    def test_lost_output_response_blocks_retry_and_explicit_recovery_uses_get_only(self):
        self.approve()
        self.provider.fail_after_create = "output"
        with self.assertRaises(TimeoutError):
            deploy(self.root, SLUG, self.provider)
        with self.assertRaisesRegex(DeploymentError, "Unresolved output"):
            deploy(self.root, SLUG, self.provider)
        self.assertEqual([call[:2] for call in self.provider.calls], [("post", "output")])
        result = deploy(self.root, SLUG, self.provider, reconcile_output_id="test-output-id")
        self.assertEqual(result["outputId"], "test-output-id")
        self.assertEqual([call[:2] for call in self.provider.calls], [
            ("post", "output"), ("get", "output"), ("post", "assistant"), ("get", "assistant")])

    def test_lost_assistant_response_blocks_duplicate_and_recovers_original_binding(self):
        self.approve()
        self.provider.fail_after_create = "assistant"
        with self.assertRaises(TimeoutError):
            deploy(self.root, SLUG, self.provider)
        with self.assertRaisesRegex(DeploymentError, "Unresolved assistant"):
            deploy(self.root, SLUG, self.provider)
        result = deploy(self.root, SLUG, self.provider, reconcile_assistant_id="test-assistant-id")
        self.assertEqual(result["assistantId"], "test-assistant-id")
        self.assertEqual(sum(call[:2] == ("post", "assistant") for call in self.provider.calls), 1)
        self.assertEqual(self.provider.resources[result["assistantId"]]["artifactPlan"]["structuredOutputIds"],
                         ["test-output-id"])

    def test_source_drift_requires_restage_and_review_before_update_retains_attachment(self):
        self.approve()
        deploy(self.root, SLUG, self.provider)
        self.provider.calls.clear()
        self.mutate("config/runtime.json", lambda value: value.update(maxDurationSeconds=330))
        with self.assertRaises(RecipeError):
            deploy(self.root, SLUG, self.provider)
        self.assertEqual(self.provider.calls, [])
        stage_candidate(self.root, SLUG, refresh=True)
        self.approve()
        deploy(self.root, SLUG, self.provider)
        self.assertEqual([call[:2] for call in self.provider.calls], [
            ("get", "output"), ("get", "assistant"), ("patch", "assistant"), ("get", "assistant")])
        updated = self.provider.resources["test-assistant-id"]
        self.assertEqual(updated["maxDurationSeconds"], 330)
        self.assertEqual(updated["artifactPlan"], {"recordingEnabled": False, "videoRecordingEnabled": False,
                                                "structuredOutputIds": ["test-output-id"]})
        self.assertTrue(list((self.root / ".local" / SLUG / "prompts/backups").glob("*.review")))

    def test_remote_output_drift_or_conflicting_local_definition_blocks_assistant_mutation(self):
        self.approve()
        deploy(self.root, SLUG, self.provider)
        self.provider.calls.clear()
        original = deepcopy(self.provider.resources["test-output-id"])
        self.provider.resources["test-output-id"]["conditions"] = []
        with self.assertRaisesRegex(DeploymentError, "output GET differs"):
            deploy(self.root, SLUG, self.provider)
        self.assertEqual([call[:2] for call in self.provider.calls], [("get", "output")])
        self.provider.resources["test-output-id"] = original
        self.provider.calls.clear()
        self.mutate("config/structured-output.json", lambda value: value["model"].update(maxTokens=1999))
        # Extraction changes do not invalidate unchanged prompt promotion.
        require_promotion(self.root, SLUG)
        with self.assertRaisesRegex(DeploymentError, "binding conflicts"):
            deploy(self.root, SLUG, self.provider)
        self.assertEqual(self.provider.calls, [])

    def test_remote_assistant_owned_drift_blocks_patch(self):
        self.approve()
        deploy(self.root, SLUG, self.provider)
        self.provider.calls.clear()
        self.provider.resources["test-assistant-id"]["maxDurationSeconds"] = 123
        with self.assertRaisesRegex(DeploymentError, "Remote owned settings changed"):
            deploy(self.root, SLUG, self.provider)
        self.assertFalse(any(call[0] == "patch" for call in self.provider.calls))

    def test_edited_promoted_artifacts_and_invalid_recovery_are_refused_before_provider_calls(self):
        self.approve()
        with self.assertRaisesRegex(DeploymentError, "No unresolved"):
            deploy(self.root, SLUG, self.provider, reconcile_output_id="invented-id")
        self.assertEqual(self.provider.calls, [])
        canonical = self.root / ".local" / SLUG / "prompts" / f"{SLUG}_vapi_system_prompt.txt"
        canonical.write_text(canonical.read_text() + "Operator edited canonical\n")
        with self.assertRaisesRegex(RecipeError, "Promoted artifact differs"):
            deploy(self.root, SLUG, self.provider)
        self.assertEqual(self.provider.calls, [])


if __name__ == "__main__":
    unittest.main()
