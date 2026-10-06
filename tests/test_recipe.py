"""Standalone synthetic instance/draft checks; never call a provider."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest

from messenger_recipe.recipe import (
    RecipeError, instance_state, load_instance, record_voice_selection,
    record_baseline_review, render_candidate, stage_candidate,
)


PACKAGE = Path(__file__).resolve().parents[1]
SLUG = "messenger_recipe_demo"


class RecipeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "standalone"
        self.root.mkdir()
        for directory in ("templates", "config", "instances", "result"):
            shutil.copytree(PACKAGE / directory, self.root / directory)

    def approve(self):
        return record_voice_selection(self.root, SLUG, "Synthetic operator decision; offline test fixture")

    def update(self, relative, mutate):
        path = self.root / relative
        value = json.loads(path.read_text())
        mutate(value)
        path.write_text(json.dumps(value, indent=2) + "\n")

    def test_voice_gate_precedes_render_and_does_not_stage(self):
        with self.assertRaises(RecipeError):
            render_candidate(self.root, SLUG)
        self.assertFalse((self.root / ".local").exists())

    def test_exact_twenty_section_render_and_pending_runtime_candidate(self):
        self.approve()
        candidate = render_candidate(self.root, SLUG)
        prompt = candidate["systemPrompt"]
        self.assertEqual(len(re.findall(r"^\[[^\]\n]+\]$", prompt, re.M)), 20)
        self.assertEqual(hashlib.sha256(prompt.encode()).hexdigest(),
                         "c670cb6584780299cdaa89f9fda43f3e10750418b71621a2ecb3c6aaa1790059")
        self.assertEqual(candidate["firstMessage"],
                         "Hello, I'm Mira, the automated message assistant for Pebble Workshop. "
                         "Would you like to leave a message for the team?")
        self.assertEqual(candidate["candidateStatus"], "provisional_unbound")
        self.assertEqual(candidate["reviewStatus"], "pending")
        payload = candidate["assistantPayload"]
        self.assertEqual(payload["model"]["messages"], [{"role": "system", "content": prompt}])
        self.assertEqual(payload["voice"]["voiceId"], "fFAtoTPtP0TtMyhbR3L9")
        self.assertEqual(payload["voice"]["model"], "eleven_flash_v2_5")
        self.assertEqual(payload["artifactPlan"], {"recordingEnabled": False, "videoRecordingEnabled": False})
        self.assertEqual(payload["endCallPhrases"], ["Have a great day."])
        self.assertEqual(payload["serverMessages"], [])
        self.assertNotIn("server", payload)

    def test_moved_package_renders_from_unrelated_cwd(self):
        self.approve()
        expected = render_candidate(self.root, SLUG)
        previous = Path.cwd()
        try:
            os.chdir(self.root.parent)
            self.assertEqual(render_candidate(self.root, SLUG), expected)
        finally:
            os.chdir(previous)
        self.assertFalse((self.root / "agents").exists())
        self.assertFalse((self.root / "agent-configs").exists())
        self.assertFalse((self.root / "var").exists())

    def test_stage_is_idempotent_private_and_does_not_promote(self):
        self.approve()
        candidate = stage_candidate(self.root, SLUG)
        self.assertEqual(stage_candidate(self.root, SLUG), candidate)
        for relative in candidate["artifactPaths"].values():
            path = self.root / relative
            self.assertTrue(path.resolve().is_relative_to(self.root / ".local"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((instance_state(self.root, SLUG) / "prompts" /
                          f"{SLUG}_vapi_system_prompt.txt").exists())

    def test_stage_refuses_divergent_draft_before_creating_other_artifacts(self):
        self.approve()
        path = instance_state(self.root, SLUG) / "prompts/backups" / f"{SLUG}_vapi_system_prompt_refactor.txt"
        path.parent.mkdir(parents=True)
        path.write_text("A deliberately edited review draft.\n")
        with self.assertRaisesRegex(RecipeError, "divergent"):
            stage_candidate(self.root, SLUG)
        self.assertEqual(path.read_text(), "A deliberately edited review draft.\n")
        self.assertFalse((instance_state(self.root, SLUG) / "candidate.json").exists())

    def test_stale_voice_and_name_require_fresh_operator_selection(self):
        self.approve()
        relative = f"instances/{SLUG}/recipe.json"
        self.update(relative, lambda v: v["voice"].update(voiceId="another-exact-fixture-voice"))
        with self.assertRaisesRegex(RecipeError, "exact voice tuple"):
            render_candidate(self.root, SLUG)
        record_voice_selection(self.root, SLUG, "Operator explicitly approved changed synthetic fixture voice")
        self.assertEqual(render_candidate(self.root, SLUG)["assistantPayload"]["voice"]["voiceId"],
                         "another-exact-fixture-voice")
        backups = list(instance_state(self.root, SLUG).glob("voice-selection.*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text())["voice"]["voiceId"], "fFAtoTPtP0TtMyhbR3L9")
        self.update(relative, lambda v: v["business"].update(assistantName="Other"))
        with self.assertRaisesRegex(RecipeError, "exact voice tuple"):
            render_candidate(self.root, SLUG)

    def test_traversal_in_slug_source_and_schema_is_rejected(self):
        with self.assertRaises(RecipeError):
            load_instance(self.root, "../escape")
        relative = f"instances/{SLUG}/recipe.json"
        self.update(relative, lambda v: v["sources"].update(runtime="../foreign.json"))
        with self.assertRaisesRegex(RecipeError, "traversal"):
            load_instance(self.root, SLUG)
        self.update(relative, lambda v: v["sources"].update(runtime="config/runtime.json"))
        self.update(relative, lambda v: v["result"].update(schemaRef="../foreign.json"))
        with self.assertRaisesRegex(RecipeError, "traversal"):
            load_instance(self.root, SLUG)

    def test_symlink_sources_and_state_are_rejected(self):
        foreign = self.root.parent / "foreign"
        foreign.mkdir()
        self.root.joinpath(".local").symlink_to(foreign, target_is_directory=True)
        with self.assertRaisesRegex(RecipeError, "symlinks"):
            record_voice_selection(self.root, SLUG, "Synthetic test approval")
        self.assertEqual(list(foreign.iterdir()), [])
        self.root.joinpath(".local").unlink()
        original = self.root / "config/runtime.json"
        target = foreign / "runtime.json"
        target.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(target)
        with self.assertRaisesRegex(RecipeError, "symlinks"):
            load_instance(self.root, SLUG)

    def test_template_drift_fails_provenance_check(self):
        self.approve()
        template = self.root / "templates/messenger_message_only.txt"
        template.write_text(template.read_text().replace("Never ask two questions", "Ask three questions"))
        with self.assertRaisesRegex(RecipeError, "recorded provenance"):
            render_candidate(self.root, SLUG)

    def test_business_values_are_single_owned_and_cannot_inject_prompt(self):
        self.update(f"instances/{SLUG}/recipe.json", lambda v: v["business"].update(name="[Injected]"))
        with self.assertRaisesRegex(RecipeError, "placeholder injection"):
            load_instance(self.root, SLUG)

    def test_editable_runtime_is_canonical_and_hash_is_review_evidence(self):
        self.approve()
        before = render_candidate(self.root, SLUG)
        self.update("config/runtime.json", lambda v: v.update(maxDurationSeconds=330))
        after = render_candidate(self.root, SLUG)
        self.assertEqual(after["assistantPayload"]["maxDurationSeconds"], 330)
        self.assertNotEqual(before["sourceHashes"]["runtime"], after["sourceHashes"]["runtime"])
        self.assertEqual(after["reviewStatus"], "pending")

    def test_inherited_analysis_integration_and_fallback_are_rejected(self):
        self.approve()
        runtime_path = self.root / "config/runtime.json"
        original = runtime_path.read_text()
        changes = (
            lambda v: v["analysisPlan"]["summaryPlan"].update(enabled=True),
            lambda v: v.update(server={"url": "https://example.invalid"}),
            lambda v: v["voice"].update(fallbackPlan={"voices": []}),
            lambda v: v["model"].update(toolIds=["unapproved-tool"]),
            lambda v: v.update(serverMessages=["end-of-call-report"]),
            lambda v: v["artifactPlan"].update(structuredOutputIds=["invented-output"]),
            lambda v: v.update(endCallPhrases=["Have a great day"]),
        )
        for change in changes:
            with self.subTest(change=change):
                runtime_path.write_text(original)
                self.update("config/runtime.json", change)
                with self.assertRaises(RecipeError):
                    render_candidate(self.root, SLUG)

    def test_malformed_runtime_objects_fail_with_actionable_errors(self):
        self.approve()
        runtime_path = self.root / "config/runtime.json"
        original = runtime_path.read_text()
        for key in ("voice", "transcriber", "model"):
            for invalid in (None, [], "invalid"):
                with self.subTest(key=key, invalid=invalid):
                    runtime_path.write_text(original)
                    self.update("config/runtime.json", lambda v: v.update({key: invalid}))
                    with self.assertRaisesRegex(RecipeError, "must be an object"):
                        render_candidate(self.root, SLUG)

    def test_notification_and_polling_fields_are_not_supported(self):
        self.update(f"instances/{SLUG}/recipe.json", lambda v: v.update(collection={"mode": "polling"}))
        with self.assertRaisesRegex(RecipeError, "supported message-only"):
            load_instance(self.root, SLUG)

    def test_duplicate_json_keys_and_crlf_template_are_rejected(self):
        instance = self.root / f"instances/{SLUG}/recipe.json"
        instance.write_text('{"slug":"one","slug":"two"}\n')
        with self.assertRaisesRegex(RecipeError, "Duplicate JSON"):
            load_instance(self.root, SLUG)
        shutil.copyfile(PACKAGE / f"instances/{SLUG}/recipe.json", instance)
        self.approve()
        template = self.root / "templates/messenger_message_only.txt"
        template.write_bytes(template.read_bytes().replace(b"\n", b"\r\n"))
        with self.assertRaisesRegex(RecipeError, "recorded provenance"):
            render_candidate(self.root, SLUG)

    def test_phase_three_review_is_private_bound_and_does_not_promote(self):
        self.approve()
        before = render_candidate(self.root, SLUG)
        approval = record_baseline_review(self.root, SLUG, "Synthetic operator Phase 3 approval")
        self.assertEqual(approval["scope"], "phase3-baseline")
        self.assertEqual(render_candidate(self.root, SLUG)["reviewStatus"], "approved_phase_3")
        path = instance_state(self.root, SLUG) / "baseline-review.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(load_instance(self.root, SLUG)["reviewStatus"], "pending")
        self.assertFalse((instance_state(self.root, SLUG) / "prompts").exists())
        after = render_candidate(self.root, SLUG)
        self.assertEqual(after["assistantPayload"], before["assistantPayload"])
        self.assertEqual(after["candidateStatus"], "provisional_unbound")

    def test_runtime_or_business_changes_invalidate_baseline_review(self):
        self.approve()
        record_baseline_review(self.root, SLUG, "Synthetic operator approved original fixture")
        self.update("config/runtime.json", lambda v: v.update(maxDurationSeconds=330))
        self.assertEqual(render_candidate(self.root, SLUG)["reviewStatus"], "pending")
        record_baseline_review(self.root, SLUG, "Synthetic operator approved updated runtime fixture")
        self.assertEqual(render_candidate(self.root, SLUG)["reviewStatus"], "approved_phase_3")
        self.update(f"instances/{SLUG}/recipe.json", lambda v: v["business"].update(name="Other Business"))
        self.assertEqual(render_candidate(self.root, SLUG)["reviewStatus"], "pending")
        self.assertEqual(len(list(instance_state(self.root, SLUG).glob("baseline-review.*.json"))), 1)

    def test_mismatched_or_malformed_review_never_grants_approval(self):
        self.approve()
        record_baseline_review(self.root, SLUG, "Synthetic operator fixture approval")
        path = instance_state(self.root, SLUG) / "baseline-review.json"
        original = path.read_text()
        for change in (
                lambda v: v.update(slug="other"),
                lambda v: v.update(assistantName="Other"),
                lambda v: v.update(scope="live-provisioning"),
                lambda v: v.update(candidateSha256="wrong"),
                lambda v: v.update(approvalSource="")):
            path.write_text(original)
            value = json.loads(path.read_text())
            change(value)
            path.write_text(json.dumps(value) + "\n")
            self.assertEqual(render_candidate(self.root, SLUG)["reviewStatus"], "pending")
        path.write_text("invalid approval\n")
        self.assertEqual(render_candidate(self.root, SLUG)["reviewStatus"], "pending")


if __name__ == "__main__":
    unittest.main()
