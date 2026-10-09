"""Checked package paths, strict reads and private Instance State writes.

Instance State is one Recipe Instance's private record set under
.local/<slug>/; it is never exported or published. Every path rejects
traversal and symlinks. Records are created exclusively or replaced
atomically, and preserved before any deliberate replacement.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import uuid


class RecipeError(ValueError):
    """The package inputs or local artifacts need operator attention."""


def check_slug(slug: str) -> str:
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", slug):
        raise RecipeError("Slug must use lowercase letters, numbers, hyphens or underscores.")
    return slug


def package_root(root: Path) -> Path:
    root = Path(root).absolute()
    if root.is_symlink() or root.resolve() != root or not root.is_dir():
        raise RecipeError("Package root must be an existing directory without symlinks.")
    return root


def package_path(root: Path, relative: str) -> Path:
    """Reject traversal and every symlink, including missing-file parent paths."""
    root = package_root(root)
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise RecipeError("Package paths must be non-empty relative POSIX paths.")
    rel = Path(relative)
    if rel.is_absolute() or any(part in {".", ".."} for part in relative.split("/")):
        raise RecipeError("Path escapes the package or contains traversal.")
    path = root / rel
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise RecipeError("Package artifact paths must not use symlinks.")
    if not path.resolve().is_relative_to(root):
        raise RecipeError("Path escapes the package.")
    return path


def read_json(path: Path) -> dict:
    """Strict object read: duplicate keys and NaN/Infinity are refused."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RecipeError(f"Duplicate JSON key: {key}.")
            result[key] = value
        return result

    def invalid_constant(value):
        raise RecipeError(f"Invalid JSON constant: {value}.")

    try:
        result = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique,
                            parse_constant=invalid_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecipeError(f"Cannot read JSON artifact {path.name}.") from exc
    if not isinstance(result, dict):
        raise RecipeError(f"{path.name} must contain a JSON object.")
    return result


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if b"\r" in raw or not raw.endswith(b"\n"):
        raise RecipeError(f"{path.name} requires LF and a final newline.")
    try:
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise RecipeError(f"{path.name} must be UTF-8.") from exc


def _serialize(content: str | dict) -> str:
    if isinstance(content, dict):
        return json.dumps(content, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    return content


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class InstanceState:
    """Private records of one Recipe Instance, named relative to .local/<slug>/."""

    CANDIDATE = "candidate.json"

    def __init__(self, root: Path, slug: str):
        self.root = package_root(root)
        self.slug = check_slug(slug)
        self.directory = package_path(self.root, f".local/{self.slug}")

    def relative(self, name: str) -> str:
        """Package-relative form for reports and refusal messages."""
        return f".local/{self.slug}/{name}"

    def path(self, name: str) -> Path:
        return package_path(self.root, self.relative(name))

    def prompt_paths(self) -> dict:
        slug = self.slug
        return {
            "draft": f"prompts/backups/{slug}_vapi_system_prompt_refactor.txt",
            "openingDraft": f"prompts/backups/{slug}_first_message_candidate.txt",
            "prompt": f"prompts/{slug}_vapi_system_prompt.txt",
            "opening": f"prompts/{slug}_first_message.txt",
        }

    def read_json(self, name: str) -> dict:
        return read_json(self.path(name))

    def read_text(self, name: str) -> str:
        return read_text(self.path(name))

    def _prepared(self, name: str) -> Path:
        self.path(name).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Re-check after creating parents so no directory became a symlink.
        return self.path(name)

    def create(self, name: str, content: str | dict) -> None:
        """Exclusive 0600 write; identical content is a no-op, divergence refuses."""
        text = _serialize(content)
        path = self._prepared(name)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if read_text(path) != text:
                raise RecipeError(f"Refusing to overwrite divergent local artifact: {self.relative(name)}.")
            return
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        _sync_directory(path.parent)

    def replace(self, name: str, content: str | dict) -> None:
        """Atomic 0600 write; both file data and directory entries are synced."""
        text = _serialize(content)
        path = self._prepared(name)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _sync_directory(path.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def preserve(self, name: str, backup_dir: str | None = None) -> str:
        """Copy a record to <stem>.<hex><ext>, beside it or in backup_dir."""
        source = PurePosixPath(name)
        directory = backup_dir or str(source.parent)
        backup = f"{source.stem}.{uuid.uuid4().hex}{source.suffix}"
        backup = backup if directory == "." else f"{directory}/{backup}"
        self.create(backup, self.read_text(name))
        return backup

    def supersede(self, name: str, content: str | dict, backup_dir: str | None = None) -> str | None:
        """Preserve a divergent record, then replace it; returns the backup name."""
        text = _serialize(content)
        backup = None
        if self.path(name).exists():
            if self.read_text(name) == text:
                return None
            backup = self.preserve(name, backup_dir)
        self.replace(name, text)
        return backup

    def move(self, source: str, target: str) -> None:
        """Atomic rename, so the source never disappears before the target exists."""
        origin, destination = self.path(source), self._prepared(target)
        if destination.exists():
            raise RecipeError(f"Refusing to overwrite existing local artifact: {self.relative(target)}.")
        os.replace(origin, destination)
        _sync_directory(destination.parent)

    def remove(self, name: str) -> None:
        path = self.path(name)
        path.unlink()
        _sync_directory(path.parent)
