"""Package-owned instance validation, exact template rendering and draft staging.

Template parsing/scalar checks follow pm_voice_agent_lifecycle.py and
lib/messenger_recipe_artifacts.py at b8671f1; no parent modules are imported.
Nothing in this module promotes a draft or calls a provider.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import unicodedata

from .configuration import render_assistant
from .state import (
    InstanceState, RecipeError, check_slug, package_path, package_root, read_json, read_text,
)


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


def _template(path: Path) -> tuple[dict, str]:
    text = read_text(path)
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
    root = package_root(root)
    instance = read_json(package_path(root, f"instances/{check_slug(slug)}/recipe.json"))
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
    if type(business["fictional"]) is not bool:
        raise RecipeError("Business fictional flag must be an explicit boolean.")
    if business["purpose"] != "Take a message for the team":
        raise RecipeError("This recipe requires a message-only business purpose.")
    if instance.get("language") != "en":
        raise RecipeError("This recipe supports English only.")
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
    if result["logicalName"] != "messenger-enquiry" or result["version"] != "2.0.0":
        raise RecipeError("New deployments require Messenger enquiry result v2.0.0.")
    read_json(package_path(root, result["schemaRef"]))
    privacy = instance.get("privacy")
    if (not isinstance(privacy, dict)
            or set(privacy) != {"recordingEnabled", "localContentRetentionDays", "liveData"}
            or type(privacy.get("recordingEnabled")) is not bool
            or privacy.get("localContentRetentionDays") != 14
            or privacy.get("liveData") not in ("synthetic_only", "real_callers")):
        raise RecipeError("Privacy settings require an explicit recording choice, 14-day local retention "
                          "and liveData set to synthetic_only or real_callers.")
    return instance


def record_voice_selection(root: Path, slug: str, approval_source: str) -> dict:
    """Record an explicitly supplied operator decision locally, never publicly."""
    instance = load_instance(root, slug)
    _scalar(approval_source, "approval_source")
    approval = {"slug": slug, "assistantName": instance["business"]["assistantName"],
                "voice": instance["voice"], "approvalSource": approval_source}
    # Only this explicitly confirmed local selection can replace its record.
    # Staging never replaces a divergent prompt or candidate without --refresh.
    InstanceState(root, slug).supersede("voice-selection.json", approval)
    return approval


def _check_runtime(runtime: dict, voice: dict, recording_enabled: bool) -> None:
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
    artifacts = runtime.get("artifactPlan")
    if (not isinstance(artifacts, dict)
            or set(artifacts) != {"recordingEnabled", "videoRecordingEnabled"}
            or artifacts.get("recordingEnabled") is not recording_enabled
            or artifacts.get("videoRecordingEnabled") is not False):
        raise RecipeError("Provisional runtime recording must match the instance privacy choice; video stays off and output binding is absent.")
    compliance = runtime.get("compliancePlan")
    if (not isinstance(compliance, dict)
            or set(compliance) != {"hipaaEnabled", "zdrEnabled"}
            or compliance.get("hipaaEnabled") is not False
            or compliance.get("zdrEnabled") is not False):
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


def render_candidate(root: Path, slug: str) -> dict:
    root = package_root(root)
    instance = load_instance(root, slug)
    approval = InstanceState(root, slug).read_json("voice-selection.json")
    if (approval.get("slug") != slug or approval.get("assistantName") != instance["business"]["assistantName"]
            or approval.get("voice") != instance["voice"] or not approval.get("approvalSource")):
        raise RecipeError("Record approval of this named assistant's exact voice tuple before rendering.")
    _scalar(approval["approvalSource"], "Recorded voice approval source")
    sources = instance["sources"]
    manifest = read_json(package_path(root, "templates/provenance.json"))
    hashes = {}
    for key in ("systemTemplate", "placeholderContract", "openingTemplate"):
        relative = sources[key]
        recorded = manifest.get("artifacts", {}).get(relative, {}).get("sha256")
        actual = _hash(package_path(root, relative))
        if recorded != actual:
            raise RecipeError(f"Bundled {key} differs from its recorded provenance; review a deliberate refresh.")
        hashes[key] = actual
    metadata, template = _template(package_path(root, sources["systemTemplate"]))
    contract = read_json(package_path(root, sources["placeholderContract"]))
    if (contract.get("template_name") != "messenger_message_only" or
            contract.get("placeholders") != [
                {"name": "company_name", "required": True, "value_format": "scalar", "description": "Public company name"},
                {"name": "company_locale_english", "required": True, "value_format": "scalar", "description": "English locale used by the assistant"}]):
        raise RecipeError("The selected message-only template requires exactly its two scalar inputs.")
    sections = re.findall(r"^\[([^\]\n]+)\]$", template, re.M)
    expected = re.findall(r"\[([^\]]+)\]", metadata.get("required_section_order", ""))
    if len(sections) != 21 or sections != expected:
        raise RecipeError("Message-only prompt must retain all 21 sections in template order.")
    prompt = template.replace("[company_name]", instance["business"]["name"])
    prompt = prompt.replace("[company_locale_english]", instance["localeEnglish"])
    if re.findall(r"^\[([^\]\n]+)\]$", prompt, re.M) != expected or any(c in prompt for c in ("*", "`")):
        raise RecipeError("Rendered prompt formatting or structure is invalid.")
    bracketed = set(re.findall(r"\[([^\]\n]+)\]", prompt))
    if bracketed != set(expected) or "{{" in prompt or "}}" in prompt:
        raise RecipeError("Rendered prompt contains unresolved template or customer-number inputs.")
    if 'On the normal close path, never trigger endCall' not in prompt or 'Have a great day.' not in prompt:
        raise RecipeError("Rendered prompt does not preserve the normal phrase-close contract.")
    opening = read_text(package_path(root, sources["openingTemplate"]))
    opening = opening.replace("[assistant_name]", instance["business"]["assistantName"])
    opening = opening.replace("[company_name]", instance["business"]["name"])
    if any(token in opening for token in ("[", "]", "{{", "}}", "*", "`")):
        raise RecipeError("Opening contains unresolved placeholders or forbidden prompt formatting.")
    runtime = read_json(package_path(root, sources["runtime"]))
    _check_runtime(runtime, instance["voice"], instance["privacy"]["recordingEnabled"])
    payload = render_assistant(runtime, name=instance["business"]["assistantName"], prompt=prompt,
                               opening=opening.strip(), voice=instance["voice"])
    hashes["runtime"] = _hash(package_path(root, sources["runtime"]))
    hashes["instance"] = _hash(package_path(root, f"instances/{slug}/recipe.json"))
    hashes["systemPrompt"] = hashlib.sha256(prompt.encode()).hexdigest()
    hashes["firstMessage"] = hashlib.sha256(opening.encode()).hexdigest()
    return {"candidateStatus": "provisional_unbound",
            "systemPrompt": prompt, "firstMessage": opening.strip(),
            "assistantPayload": payload, "sourceHashes": hashes}


def stage_candidate(root: Path, slug: str, *, refresh: bool = False) -> dict:
    """Stage a draft/opening/review preview; canonical promotion is Phase 4."""
    candidate = render_candidate(root, slug)
    state = InstanceState(root, slug)
    prompts = state.prompt_paths()
    names = {"systemPrompt": prompts["draft"], "firstMessage": prompts["openingDraft"],
             "preview": state.CANDIDATE}
    contents = {"systemPrompt": candidate["systemPrompt"],
                "firstMessage": candidate["firstMessage"] + "\n",
                "preview": json.dumps(candidate, indent=2, ensure_ascii=False) + "\n"}
    # Validate every destination before writing any artifact. A deliberately
    # requested refresh preserves old review files before replacing them.
    for key, name in names.items():
        if state.path(name).exists() and state.read_text(name) != contents[key]:
            if not refresh:
                raise RecipeError(f"Refusing to overwrite divergent local artifact: {state.relative(name)}.")
    for key, name in names.items():
        state.supersede(name, contents[key])
    return {**candidate, "artifactPaths": {key: state.relative(name) for key, name in names.items()}}
