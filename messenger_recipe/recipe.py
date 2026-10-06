"""Package-owned instance validation, exact template rendering and draft staging.

Template parsing/scalar checks follow pm_voice_agent_lifecycle.py and
lib/messenger_recipe_artifacts.py at b8671f1; no parent modules are imported.
Nothing in this module promotes a draft or calls a provider.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata
import uuid

from .configuration import render_assistant
from .deployment import digest, write_json


class RecipeError(ValueError):
    """The package inputs or local artifacts need operator attention."""


def _slug(slug: str) -> str:
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", slug):
        raise RecipeError("Slug must use lowercase letters, numbers, hyphens or underscores.")
    return slug


def _root(root: Path) -> Path:
    root = Path(root).absolute()
    if root.is_symlink() or root.resolve() != root or not root.is_dir():
        raise RecipeError("Package root must be an existing directory without symlinks.")
    return root


def package_path(root: Path, relative: str) -> Path:
    """Reject traversal and every symlink, including missing-file parent paths."""
    root = _root(root)
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


def instance_state(root: Path, slug: str) -> Path:
    return package_path(root, f".local/{_slug(slug)}")


def _json(path: Path) -> dict:
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


def _scalar(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise RecipeError(f"{label} must be a non-blank scalar without surrounding spaces.")
    if len(value) > 500 or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value):
        raise RecipeError(f"{label} contains control characters or is too long.")
    if any(token in value for token in ("[", "]", "{{", "}}", "`", "*")):
        raise RecipeError(f"{label} contains prompt formatting or placeholder injection.")
    return value


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text(path: Path) -> str:
    raw = path.read_bytes()
    if b"\r" in raw or not raw.endswith(b"\n"):
        raise RecipeError(f"{path.name} requires LF and a final newline.")
    try:
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise RecipeError(f"{path.name} must be UTF-8.") from exc


def _template(path: Path) -> tuple[dict, str]:
    text = _text(path)
    closing = text.find("\n---\n", 4)
    if not text.startswith("---\n") or closing < 0:
        raise RecipeError("System template requires its original frontmatter.")
    metadata = {}
    for line in text[4:closing].splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            raise RecipeError("Malformed template frontmatter.")
        metadata[key.strip()] = value.strip().strip('"')
    return metadata, text[closing + len("\n---\n"):]


def load_instance(root: Path, slug: str) -> dict:
    root = _root(root)
    instance = _json(package_path(root, f"instances/{_slug(slug)}/recipe.json"))
    if instance.get("slug") != slug or instance.get("reviewStatus") != "pending":
        raise RecipeError("Instance slug must match; public source remains pending local review.")
    allowed = {"recipeVersion", "reviewStatus", "slug", "business", "language", "localeEnglish",
               "voice", "sources", "result", "privacy"}
    if set(instance) != allowed:
        raise RecipeError("Instance fields differ from the supported message-only configuration.")
    business = instance.get("business")
    if not isinstance(business, dict) or set(business) != {"name", "fictional", "assistantName", "purpose"}:
        raise RecipeError("Business requires identity, assistantName, purpose and fictional flag.")
    for key in ("name", "assistantName", "purpose"):
        _scalar(business[key], key)
    if business["purpose"] != "Take a message for the team" or business["fictional"] is not True:
        raise RecipeError("Initial pilot requires a fictional message-only business.")
    if instance.get("language") != "en":
        raise RecipeError("This pilot supports English only.")
    _scalar(instance.get("localeEnglish"), "localeEnglish")
    voice = instance.get("voice")
    if not isinstance(voice, dict) or set(voice) != {"provider", "voiceId", "model"}:
        raise RecipeError("Voice requires an exact provider, voiceId and model.")
    for key, value in voice.items():
        _scalar(value, f"voice.{key}")
    if voice["provider"] != "11labs":
        raise RecipeError("Initial Recipe voice selection requires ElevenLabs.")
    sources = instance.get("sources")
    if not isinstance(sources, dict) or set(sources) != {"systemTemplate", "placeholderContract", "openingTemplate", "runtime"}:
        raise RecipeError("Instance requires the four package-owned source paths.")
    for relative in sources.values():
        if not package_path(root, relative).is_file():
            raise RecipeError("A configured package source is missing.")
    result = instance.get("result")
    if not isinstance(result, dict) or set(result) != {"logicalName", "version", "schemaRef"}:
        raise RecipeError("Result requires logicalName, version and package-owned schemaRef.")
    if result["logicalName"] != "messenger-enquiry" or result["version"] != "1.0.0":
        raise RecipeError("Only the existing Messenger enquiry result v1.0.0 is supported.")
    _json(package_path(root, result["schemaRef"]))
    privacy = instance.get("privacy")
    if privacy != {"recordingEnabled": False, "localContentRetentionDays": 14, "liveData": "synthetic_only"}:
        raise RecipeError("Pilot privacy settings require recording off and synthetic data.")
    return instance


def _private_write(root: Path, relative: str, content: str) -> None:
    path = package_path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = package_path(root, relative)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if _text(path) != content:
            raise RecipeError(f"Refusing to overwrite divergent local artifact: {relative}.")
        return
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def record_voice_selection(root: Path, slug: str, approval_source: str) -> dict:
    """Record an explicitly supplied operator decision locally, never publicly."""
    instance = load_instance(root, slug)
    _scalar(approval_source, "approval_source")
    approval = {"slug": slug, "assistantName": instance["business"]["assistantName"],
                "voice": instance["voice"], "approvalSource": approval_source}
    relative = f".local/{slug}/voice-selection.json"
    text = json.dumps(approval, indent=2, ensure_ascii=False) + "\n"
    path = package_path(root, relative)
    if path.exists():
        previous = _text(path)
        if previous == text:
            return approval
        backup = f".local/{slug}/voice-selection.{uuid.uuid4().hex}.json"
        _private_write(root, backup, previous)
        # Only this explicitly confirmed local selection can replace its record.
        # Staging never replaces a divergent prompt or candidate.
        write_json(package_path(root, relative), approval)
    else:
        _private_write(root, relative, text)
    return approval


def _check_runtime(runtime: dict, voice: dict) -> None:
    for key in ("voice", "transcriber", "model"):
        if not isinstance(runtime.get(key), dict):
            raise RecipeError(f"Runtime {key} must be an object.")
    if runtime.get("voice", {}).get("provider") != voice["provider"]:
        raise RecipeError("Runtime and selected voice providers differ.")
    if any(key in runtime.get("voice", {}) for key in ("voiceId", "model", "fallbackPlan")):
        raise RecipeError("Shared voice runtime cannot own identity/model or a fallback voice.")
    if "fallbackPlan" in runtime["transcriber"] or runtime["transcriber"].get("language") != "en":
        raise RecipeError("Transcriber must use English without inherited fallbacks.")
    if runtime.get("analysisPlan") != {"summaryPlan": {"enabled": False},
                                      "structuredDataPlan": {"enabled": False},
                                      "successEvaluationPlan": {"enabled": False}}:
        raise RecipeError("Generic summary, structured data and success analysis must stay disabled.")
    if runtime.get("artifactPlan") != {"recordingEnabled": False, "videoRecordingEnabled": False}:
        raise RecipeError("Provisional runtime must disable recording/video and have no output binding.")
    if runtime.get("compliancePlan") != {"hipaaEnabled": False}:
        raise RecipeError("Pilot compliance configuration differs from the review baseline.")
    if runtime.get("endCallPhrases") != ["Have a great day."] or runtime.get("endCallFunctionEnabled") is not True:
        raise RecipeError("Runtime must preserve the punctuated phrase-triggered close.")
    model = runtime.get("model", {})
    if model.get("tools") != [{"type": "endCall"}] or model.get("toolIds") != [] or "messages" in model:
        raise RecipeError("Only the dormant endCall tool is permitted; prompt comes from its template.")
    if model.get("fallbackModels") != ["gpt-4.1-mini"]:
        raise RecipeError("Conversation model fallback differs from the reviewed proposal.")
    if runtime.get("serverMessages") != [] or runtime.get("clientMessages") != ["transcript"]:
        raise RecipeError("Only client transcripts are permitted; server messages must be explicitly off.")
    if any(key in runtime for key in ("server", "serverUrl", "serverUrlSecret", "variableValues", "hooks", "name", "firstMessage")):
        raise RecipeError("Runtime cannot inherit integrations, instance identity or rendered prompts.")


def _baseline_digest(candidate: dict) -> str:
    return digest({"sourceHashes": candidate["sourceHashes"],
                   "assistantPayload": candidate["assistantPayload"]})


def record_baseline_review(root: Path, slug: str, approval_source: str) -> dict:
    """Record exact Phase 3 approval locally; never promote or authorize live use."""
    _scalar(approval_source, "approval_source")
    candidate = render_candidate(root, slug)
    approval = {"scope": "phase3-baseline", "slug": slug,
                "assistantName": candidate["assistantPayload"]["name"],
                "candidateSha256": _baseline_digest(candidate),
                "approvalSource": approval_source}
    relative = f".local/{slug}/baseline-review.json"
    text = json.dumps(approval, indent=2, ensure_ascii=False) + "\n"
    path = package_path(root, relative)
    if path.exists():
        previous = _text(path)
        if previous == text:
            return approval
        backup = f".local/{slug}/baseline-review.{uuid.uuid4().hex}.json"
        _private_write(root, backup, previous)
        write_json(package_path(root, relative), approval)
    else:
        _private_write(root, relative, text)
    return approval


def render_candidate(root: Path, slug: str) -> dict:
    root = _root(root)
    instance = load_instance(root, slug)
    approval = _json(package_path(root, f".local/{slug}/voice-selection.json"))
    if (approval.get("slug") != slug or approval.get("assistantName") != instance["business"]["assistantName"]
            or approval.get("voice") != instance["voice"] or not approval.get("approvalSource")):
        raise RecipeError("Record approval of this named assistant's exact voice tuple before rendering.")
    _scalar(approval["approvalSource"], "Recorded voice approval source")
    sources = instance["sources"]
    manifest = _json(package_path(root, "templates/provenance.json"))
    hashes = {}
    for key in ("systemTemplate", "placeholderContract", "openingTemplate"):
        relative = sources[key]
        recorded = manifest.get("artifacts", {}).get(relative, {}).get("sha256")
        actual = _hash(package_path(root, relative))
        if recorded != actual:
            raise RecipeError(f"Bundled {key} differs from its recorded provenance; review a deliberate refresh.")
        hashes[key] = actual
    metadata, template = _template(package_path(root, sources["systemTemplate"]))
    contract = _json(package_path(root, sources["placeholderContract"]))
    if (contract.get("template_name") != "messenger_message_only" or
            contract.get("placeholders") != [
                {"name": "company_name", "required": True, "value_format": "scalar", "description": "Public company name"},
                {"name": "company_locale_english", "required": True, "value_format": "scalar", "description": "English locale used by the assistant"}]):
        raise RecipeError("The selected message-only template requires exactly its two scalar inputs.")
    sections = re.findall(r"^\[([^\]\n]+)\]$", template, re.M)
    expected = re.findall(r"\[([^\]]+)\]", metadata.get("required_section_order", ""))
    if len(sections) != 20 or sections != expected:
        raise RecipeError("Message-only prompt must retain all 20 sections in template order.")
    prompt = template.replace("[company_name]", instance["business"]["name"])
    prompt = prompt.replace("[company_locale_english]", instance["localeEnglish"])
    if re.findall(r"^\[([^\]\n]+)\]$", prompt, re.M) != expected or any(c in prompt for c in ("*", "`")):
        raise RecipeError("Rendered prompt formatting or structure is invalid.")
    bracketed = set(re.findall(r"\[([^\]\n]+)\]", prompt))
    if bracketed != set(expected) or "{{" in prompt or "}}" in prompt:
        raise RecipeError("Rendered prompt contains unresolved template or customer-number inputs.")
    if 'On the normal close path, never trigger endCall' not in prompt or 'Have a great day.' not in prompt:
        raise RecipeError("Rendered prompt does not preserve the normal phrase-close contract.")
    opening = _text(package_path(root, sources["openingTemplate"]))
    opening = opening.replace("[assistant_name]", instance["business"]["assistantName"])
    opening = opening.replace("[company_name]", instance["business"]["name"])
    if any(token in opening for token in ("[", "]", "{{", "}}", "*", "`")):
        raise RecipeError("Opening contains unresolved placeholders or forbidden prompt formatting.")
    runtime = _json(package_path(root, sources["runtime"]))
    _check_runtime(runtime, instance["voice"])
    payload = render_assistant(runtime, name=instance["business"]["assistantName"], prompt=prompt,
                               opening=opening.strip(), voice=instance["voice"])
    hashes["runtime"] = _hash(package_path(root, sources["runtime"]))
    hashes["instance"] = _hash(package_path(root, f"instances/{slug}/recipe.json"))
    hashes["systemPrompt"] = hashlib.sha256(prompt.encode()).hexdigest()
    hashes["firstMessage"] = hashlib.sha256(opening.encode()).hexdigest()
    candidate = {"candidateStatus": "provisional_unbound", "reviewStatus": "pending",
                 "systemPrompt": prompt, "firstMessage": opening.strip(),
                 "assistantPayload": payload, "sourceHashes": hashes}
    review_path = package_path(root, f".local/{slug}/baseline-review.json")
    if review_path.exists():
        try:
            review = _json(review_path)
        except RecipeError:
            review = {}  # A malformed approval never grants review status.
        if (review.get("scope") == "phase3-baseline" and review.get("slug") == slug
                and review.get("assistantName") == payload["name"]
                and review.get("candidateSha256") == _baseline_digest(candidate)
                and isinstance(review.get("approvalSource"), str)
                and review["approvalSource"].strip()):
            candidate["reviewStatus"] = "approved_phase_3"
    return candidate


def stage_candidate(root: Path, slug: str, *, refresh: bool = False) -> dict:
    """Stage a draft/opening/review preview; canonical promotion is Phase 4."""
    candidate = render_candidate(root, slug)
    paths = {
        "systemPrompt": f".local/{slug}/prompts/backups/{slug}_vapi_system_prompt_refactor.txt",
        "firstMessage": f".local/{slug}/prompts/backups/{slug}_first_message_candidate.txt",
        "preview": f".local/{slug}/candidate.json",
    }
    contents = {"systemPrompt": candidate["systemPrompt"],
                "firstMessage": candidate["firstMessage"] + "\n",
                "preview": json.dumps(candidate, indent=2, ensure_ascii=False) + "\n"}
    # Validate every destination before writing any artifact. A deliberately
    # requested refresh preserves old review files before replacing them.
    divergent = []
    for key, relative in paths.items():
        path = package_path(root, relative)
        if path.exists() and _text(path) != contents[key]:
            if not refresh:
                raise RecipeError(f"Refusing to overwrite divergent local artifact: {relative}.")
            divergent.append((path, _text(path)))
    for path, previous in divergent:
        _private_write(root, f".local/{slug}/prompts/backups/{path.name}.{uuid.uuid4().hex}.review", previous)
        path.unlink()
    for key, relative in paths.items():
        _private_write(root, relative, contents[key])
    return {**candidate, "artifactPaths": paths}
