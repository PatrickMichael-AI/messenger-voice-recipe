"""Exercise the public CLI from a copy containing only package inputs/code."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.outside = Path(self.directory.name)
        self.package = self.outside / "standalone"
        self.package.mkdir()
        for directory in ("messenger_recipe", "templates", "config", "result"):
            shutil.copytree(ROOT / directory, self.package / directory,
                            ignore=shutil.ignore_patterns("__pycache__"))
        source = ROOT / "instances/messenger_recipe_demo/recipe.json"
        destination = self.package / "instances/messenger_recipe_demo/recipe.json"
        destination.parent.mkdir(parents=True)
        shutil.copy2(source, destination)
        shutil.copy2(ROOT / "messenger.py", self.package / "messenger.py")

    def run_cli(self, *arguments):
        # No inherited Python path or provider credential can make these pass.
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"PYTHONPATH", "VAPI_API_KEY", "PM_VAPI_TOKEN"}}
        return subprocess.run([sys.executable, str(self.package / "messenger.py"), *arguments],
                              cwd=self.outside, env=environment, text=True, capture_output=True,
                              timeout=15)

    def test_clean_copy_requires_voice_selection_then_renders_from_unrelated_cwd(self):
        missing = self.run_cli("check")
        self.assertEqual(missing.returncode, 1)
        self.assertNotIn("Traceback", missing.stderr)
        self.assertEqual(self.run_cli("select-voice", "--confirm").returncode, 0)
        checked = self.run_cli("check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        preview = self.run_cli("preview")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        candidate = json.loads(preview.stdout)
        self.assertEqual(candidate["candidateStatus"], "provisional_unbound")
        self.assertEqual(candidate["assistantPayload"]["voice"]["voiceId"], "NTqGiNK8P02i66yY2GOH")
        self.assertNotIn("structuredOutputIds", candidate["assistantPayload"]["artifactPlan"])
        staged = self.run_cli("stage")
        self.assertEqual(staged.returncode, 0, staged.stderr)
        paths = json.loads(staged.stdout)["artifactPaths"]
        for relative in paths.values():
            self.assertTrue((self.package / relative).is_file())
        self.assertFalse((self.package / "agents").exists())
        self.assertFalse((self.package / "agent-configs").exists())
        self.assertFalse((self.package / "var").exists())
        self.assertFalse((self.package / ".local/messenger_recipe_demo/prompts/messenger_recipe_demo_vapi_system_prompt.txt").exists())

    def test_stage_preserves_operator_edits_and_live_deploy_requires_promotion(self):
        self.assertEqual(self.run_cli("select-voice", "--confirm").returncode, 0)
        self.assertEqual(self.run_cli("stage").returncode, 0)
        draft = self.package / ".local/messenger_recipe_demo/prompts/backups/messenger_recipe_demo_vapi_system_prompt_refactor.txt"
        draft.write_text("Operator edited draft\n")
        result = self.run_cli("stage")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(draft.read_text(), "Operator edited draft\n")
        self.assertNotIn("Traceback", result.stderr)
        preview = self.run_cli("deploy")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertEqual(json.loads(preview.stdout)["bindingStatus"], "provisional_unbound")
        self.assertEqual(self.run_cli("deploy", "--live").returncode, 1)

    def test_clone_promotes_without_history_and_still_requires_live_credential(self):
        self.assertEqual(self.run_cli("select-voice", "--confirm").returncode, 0)
        self.assertEqual(self.run_cli("stage").returncode, 0)
        self.assertEqual(self.run_cli("promote").returncode, 2)
        promoted = self.run_cli("promote", "--confirm")
        self.assertEqual(promoted.returncode, 0, promoted.stderr)
        self.assertFalse(json.loads(promoted.stdout)["liveAuthorized"])
        preview = self.run_cli("deploy")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertEqual(json.loads(preview.stdout)["promotionStatus"], "approved_current_sources")
        missing = self.run_cli("deploy", "--live")
        self.assertEqual(missing.returncode, 1)
        self.assertNotIn("Traceback", missing.stderr)
        self.assertFalse((self.package / ".local/messenger_recipe_demo/output-pending.json").exists())

    def test_output_update_requires_explicit_live_intent(self):
        result = self.run_cli("deploy", "--update-output")
        self.assertEqual(result.returncode, 1)
        self.assertIn("require --live", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((self.package / ".local").exists())

    def test_saved_call_inspection_is_offline_and_attempts_are_bounded(self):
        state = self.package / ".local/messenger_recipe_demo"
        (state / "calls").mkdir(parents=True)
        (state / "assistant-accepted.json").write_text(json.dumps({"id": "fixture-assistant"}))
        (state / "output-accepted.json").write_text(json.dumps({"id": "fixture-output"}))
        result = {"schemaVersion": "1.0.0", "outcome": "no_message", "completeness": "not_applicable",
                  "callbackNumberConfirmed": False, "evidence": [], "uncertainties": []}
        call = {"id": "fixture-call", "assistantId": "fixture-assistant",
                "artifact": {"structuredOutputs": {"fixture-output": {"result": result}}}}
        (state / "calls/fixture-call.attempt-2.json").write_text(json.dumps(call))
        inspected = self.run_cli("inspect-call", "fixture-call", "--attempt", "2")
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        self.assertEqual(json.loads(inspected.stdout)["status"], "valid_shape_semantics_unproved")
        for command in ("fetch-call", "inspect-call"):
            invalid = self.run_cli(command, "fixture-call", "--attempt", "4")
            self.assertEqual(invalid.returncode, 2)
            self.assertNotIn("Traceback", invalid.stderr)
        missing = self.run_cli("verify-deployment")
        self.assertEqual(missing.returncode, 1)
        self.assertNotIn("Traceback", missing.stderr)
        self.assertFalse((state / "evidence").exists())


if __name__ == "__main__":
    unittest.main()
