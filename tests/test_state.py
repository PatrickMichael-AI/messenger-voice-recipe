"""Instance State path, read and write disciplines over a temporary package root."""

import json
import os
from pathlib import Path
import re
import tempfile
import unittest

from messenger_recipe.state import InstanceState, RecipeError, read_json


class InstanceStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "package"
        self.root.mkdir()
        self.state = InstanceState(self.root, "fixture")

    def write(self, name: str, text: str) -> Path:
        path = self.state.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_paths_reject_bad_slug_and_traversal(self):
        for slug in ("../escape", "Upper", ""):
            with self.subTest(slug=slug), self.assertRaises(RecipeError):
                InstanceState(self.root, slug)
        for name in ("../escape.json", "a/./b.json", "a\\b.json"):
            with self.subTest(name=name), self.assertRaises(RecipeError):
                self.state.path(name)
        self.assertEqual(self.state.relative("calls/x.json"), ".local/fixture/calls/x.json")

    def test_symlinked_state_directory_or_record_is_refused(self):
        outside = Path(self.directory.name) / "outside"
        outside.mkdir()
        (self.root / ".local").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RecipeError, "symlinks"):
            InstanceState(self.root, "fixture")
        (self.root / ".local").unlink()
        target = outside / "original.json"
        target.write_text('{"preserved": true}\n')
        self.state.directory.mkdir(parents=True)
        (self.state.directory / "linked.json").symlink_to(target)
        operations = (
            lambda: self.state.read_json("linked.json"),
            lambda: self.state.create("linked.json", {"overwrite": True}),
            lambda: self.state.replace("linked.json", {"overwrite": True}),
            lambda: self.state.supersede("linked.json", {"overwrite": True}),
            lambda: self.state.move("linked.json", "moved.json"),
        )
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaisesRegex(RecipeError, "symlinks"):
                operation()
        self.assertEqual(target.read_text(), '{"preserved": true}\n')
        self.assertEqual(list(outside.iterdir()), [target])

    def test_strict_read_refuses_duplicates_constants_and_non_objects(self):
        for text, message in (('{"a": 1, "a": 2}\n', "Duplicate JSON key"),
                              ('{"a": NaN}\n', "Invalid JSON constant"),
                              ('{"a": Infinity}\n', "Invalid JSON constant"),
                              ('[1]\n', "must contain a JSON object"),
                              ('not json\n', "Cannot read JSON artifact")):
            with self.subTest(text=text):
                path = self.write("record.json", text)
                with self.assertRaisesRegex(RecipeError, message):
                    self.state.read_json("record.json")
                with self.assertRaisesRegex(RecipeError, message):
                    read_json(path)

    def test_create_is_exclusive_private_and_idempotent(self):
        self.state.create("calls/one.json", {"id": "one"})
        path = self.state.path("calls/one.json")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text()), {"id": "one"})
        self.state.create("calls/one.json", {"id": "one"})
        with self.assertRaisesRegex(RecipeError, re.escape(
                "Refusing to overwrite divergent local artifact: .local/fixture/calls/one.json.")):
            self.state.create("calls/one.json", {"id": "two"})
        self.assertEqual(json.loads(path.read_text()), {"id": "one"})

    def test_replace_is_atomic_private_and_leaves_no_temporary(self):
        self.write("record.json", "old\n")
        self.state.replace("record.json", {"value": 1})
        path = self.state.path("record.json")
        self.assertEqual(path.read_text(), '{\n  "value": 1\n}\n')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(p.name for p in self.state.directory.iterdir()), ["record.json"])
        with self.assertRaises(ValueError):
            self.state.replace("record.json", {"value": float("nan")})
        self.assertEqual(path.read_text(), '{\n  "value": 1\n}\n')
        self.assertEqual(sorted(p.name for p in self.state.directory.iterdir()), ["record.json"])

    def test_supersede_preserves_divergent_record_with_one_naming_rule(self):
        self.assertIsNone(self.state.supersede("approval.json", {"v": 1}))
        self.assertIsNone(self.state.supersede("approval.json", {"v": 1}))
        backup = self.state.supersede("approval.json", {"v": 2})
        self.assertRegex(backup, r"^approval\.[0-9a-f]{32}\.json$")
        self.assertEqual(self.state.read_json(backup), {"v": 1})
        self.assertEqual(self.state.read_json("approval.json"), {"v": 2})
        self.write("prompts/prompt.txt", "old\n")
        backup = self.state.supersede("prompts/prompt.txt", "new\n", backup_dir="prompts/backups")
        self.assertRegex(backup, r"^prompts/backups/prompt\.[0-9a-f]{32}\.txt$")
        self.assertEqual(self.state.read_text(backup), "old\n")
        self.assertEqual(self.state.path(backup).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.read_text("prompts/prompt.txt"), "new\n")

    def test_move_is_atomic_and_never_overwrites(self):
        self.write("pending.json", "{}\n")
        self.write("taken.json", "{}\n")
        with self.assertRaisesRegex(RecipeError, "existing local artifact"):
            self.state.move("pending.json", "taken.json")
        self.state.move("pending.json", "archive.json")
        self.assertFalse(self.state.path("pending.json").exists())
        self.assertEqual(self.state.read_json("archive.json"), {})
        self.state.remove("archive.json")
        self.assertFalse(os.path.exists(self.state.path("archive.json")))


if __name__ == "__main__":
    unittest.main()
