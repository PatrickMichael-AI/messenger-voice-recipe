"""Native extraction source rendering and enquiry-v1 shape checks.

The definition follows Vapi's create-output API (checked 2026-10-06):
https://docs.vapi.ai/api-reference/structured-outputs/structured-output-controller-create
Custom extraction uses documented transcript/structuredOutput.schema Liquid
variables. The provider schema is a plain required-field object; absent captured
strings use an empty string. Local normalization restores omissions and enforces
the enquiry contract. Offline checks do not establish live extraction accuracy.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re

from .deployment import digest
from .recipe import load_instance
from .state import RecipeError, package_path, read_json


SCHEMA_DIGEST = "f304b725ddd73019f564a84fea7cb2c422b53073df8f658ed4a32e6e73dddd19"
DEFINITION_PATH = "config/structured-output.json"


def render_output(root: Path, slug: str) -> dict:
    """Produce API intent without a provider ID or fictional binding.

    The result contract stays unchanged in its sole instance-owned source.
    Its provider representation uses the common Vapi/OpenAI schema subset.
    Cross-field conditions and non-empty checks remain in local validation.
    Changing the v1 contract requires a separately reviewed version/migration.
    """
    instance = load_instance(root, slug)
    definition = read_json(package_path(root, DEFINITION_PATH))
    if set(definition) != {"name", "description", "type", "model", "conditions"}:
        raise RecipeError("Output source requires only name, description, type, model and conditions.")
    name = definition.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise RecipeError("Output name must be a non-empty lowercase name.")
    full_name = f"{slug}-{name}"
    if len(full_name) > 40:
        raise RecipeError("Instance slug plus output name exceeds Vapi's 40-character limit.")
    if not isinstance(definition.get("description"), str) or not definition["description"].strip():
        raise RecipeError("Output description is required.")
    if definition.get("type") != "ai":
        raise RecipeError("Messenger extraction requires the AI output type.")
    conditions = definition.get("conditions")
    if conditions != [{"type": "minMessages", "count": 0}] or type(conditions[0]["count"]) is not int:
        raise RecipeError("Output must explicitly remove the short-call minimum with minMessages count 0.")
    model = definition.get("model")
    if not isinstance(model, dict) or set(model) != {"provider", "model", "temperature", "maxTokens", "messages"}:
        raise RecipeError("Output requires an explicit extraction model and custom messages.")
    if model["provider"] != "openai" or model["model"] != "gpt-4.1-mini":
        raise RecipeError("This reviewed output uses the separate OpenAI gpt-4.1-mini extraction model.")
    if type(model["temperature"]) not in (int, float) or model["temperature"] != 0:
        raise RecipeError("Extraction temperature must be 0.")
    if type(model["maxTokens"]) is not int or not 1 <= model["maxTokens"] <= 10000:
        raise RecipeError("Extraction maxTokens must be a positive bounded integer.")
    messages = model["messages"]
    if not isinstance(messages, list) or len(messages) != 2:
        raise RecipeError("Extraction requires its own system and user messages.")
    for message, role in zip(messages, ("system", "user")):
        if (not isinstance(message, dict) or set(message) != {"role", "content"}
                or message.get("role") != role or not isinstance(message.get("content"), str)
                or not message["content"].strip()):
            raise RecipeError("Extraction messages require non-empty system and user content.")
    text = "\n".join(message["content"] for message in messages)
    variables = re.findall(r"{{\s*([^{}]+?)\s*}}", text)
    if set(variables) != {"transcript", "structuredOutput.schema"}:
        raise RecipeError("Extraction must reference only transcript and structuredOutput.schema.")
    remainder = re.sub(r"{{\s*[^{}]+?\s*}}", "", text)
    if "{{" in remainder or "}}" in remainder or "{%" in remainder or "%}" in remainder:
        raise RecipeError("Extraction contains an unsupported Liquid expression.")
    schema = read_json(package_path(root, instance["result"]["schemaRef"]))
    if digest(schema) != SCHEMA_DIGEST:
        raise RecipeError("Enquiry v1.0.0 schema differs from the agreed contract; review a version change.")
    result = deepcopy(definition)
    result["name"] = full_name
    result["schema"] = _vapi_schema(schema)
    return result


def _vapi_schema(schema: dict) -> dict:
    """Render the fixed contract's extraction shape, not its validation schema.

    OpenAI rejects conditional composition and requires every object field.
    Vapi's documented type enum excludes null/unions. Empty captured strings
    therefore represent absence on the wire only. Never relax the canonical
    validator or infer consent/confirmation while normalizing the response.
    """
    def wire(node: dict) -> dict:
        result = {"type": node["type"]}
        if "enum" in node:
            result["enum"] = deepcopy(node["enum"])
        if "const" in node:
            result["enum"] = [node["const"]]
        if node["type"] == "object":
            result["properties"] = {key: wire(value) for key, value in node["properties"].items()}
            result["required"] = list(result["properties"])
            result["additionalProperties"] = False
        if node["type"] == "array":
            result["items"] = wire(node["items"])
        return result

    result = wire(schema)
    for field in ("callerName", "message", "callbackNumber"):
        result["properties"][field]["description"] = "Use an empty string when absent or withdrawn; never invent a value."
    return result


def normalize_native_result(result: dict) -> dict:
    """Return a validated canonical copy; leave raw provider evidence untouched.

    Historical canonical results without empty placeholders remain readable.
    Only exact empty captured strings become omissions; nulls, missing required
    fields, unknown fields and contradictory outcomes still fail validation.
    """
    if not isinstance(result, dict):
        raise RecipeError("Native result must be an object.")
    normalized = deepcopy(result)
    for field in ("callerName", "message", "callbackNumber"):
        if normalized.get(field) == "":
            del normalized[field]
    validate_result_shape(normalized)
    return normalized


def read_enquiry_result(call: dict, output_id: str) -> dict:
    """Classify one call's native result for the bound output; never raises for payload problems.

    Returns status, providerOutputMetadata when present, and the canonical
    result only when its shape is valid. The result is caller content: reports
    must not print it. A valid shape does not prove semantic correctness.
    """
    artifact = call.get("artifact")
    outputs = artifact.get("structuredOutputs") if isinstance(artifact, dict) else None
    if artifact is not None and not isinstance(artifact, dict):
        return {"status": "malformed_native_result"}
    if outputs is None or outputs == {}:
        return {"status": "missing_native_result"}
    if not isinstance(outputs, dict):
        return {"status": "malformed_native_result"}
    if output_id not in outputs:
        return {"status": "wrong_output_binding"}
    entry = outputs[output_id]
    if not isinstance(entry, dict):
        return {"status": "malformed_native_result"}
    reading = {}
    metadata = {key: entry[key] for key in ("version", "versionId", "structuredOutputVersion", "structuredOutputVersionId")
                if key in entry and type(entry[key]) in (str, int)}
    if metadata:
        reading["providerOutputMetadata"] = metadata
    if any(key in entry and entry[key] != output_id for key in ("id", "structuredOutputId")):
        reading["status"] = "wrong_output_binding"
    elif entry.get("error") or entry.get("status") in ("failed", "error"):
        reading["status"] = "failed_native_result"
    elif "result" not in entry or entry["result"] is None:
        reading["status"] = "missing_native_result"
    else:
        try:
            reading["result"] = normalize_native_result(entry["result"])
        except RecipeError:
            reading["status"] = "malformed_native_result"
        else:
            reading["status"] = "valid_shape_semantics_unproved"
    return reading


def validate_result_shape(result: dict) -> None:
    """Check the fixed enquiry-v1 schema shape; never claim semantic validity.

This small contract-specific check needs no JSON Schema runtime dependency.
It enforces the existing v1 fields/conditions, including nested evidence. It
does not verify quotes, consent, chronology, latest values or extraction success.
"""
    required = {"schemaVersion", "outcome", "completeness", "callbackNumberConfirmed", "evidence", "uncertainties"}
    captured = {"callerName", "message", "callbackNumber"}
    if not isinstance(result, dict) or not required.issubset(result) or set(result) - required - captured:
        raise RecipeError("Result has missing required or unknown fields.")
    if result["schemaVersion"] != "1.0.0":
        raise RecipeError("Result schemaVersion must be 1.0.0.")
    if result["outcome"] not in ("enquiry", "declined", "unsupported", "no_message"):
        raise RecipeError("Result outcome is invalid.")
    if result["completeness"] not in ("complete", "partial", "not_applicable"):
        raise RecipeError("Result completeness is invalid.")
    if type(result["callbackNumberConfirmed"]) is not bool:
        raise RecipeError("Result callbackNumberConfirmed must be a boolean.")
    for field in captured.intersection(result):
        if not isinstance(result[field], str) or len(result[field]) < 1:
            raise RecipeError(f"Result {field} must be a non-empty string when supplied.")
    if "callbackNumber" in result and not re.fullmatch(r"\+?[0-9]{3,20}", result["callbackNumber"]):
        raise RecipeError("Result callbackNumber must contain 3–20 digits with an optional supplied plus.")
    if result["outcome"] == "enquiry":
        if result["completeness"] not in ("complete", "partial"):
            raise RecipeError("Enquiry completeness must be complete or partial.")
    elif (result["completeness"] != "not_applicable" or result["callbackNumberConfirmed"]
          or captured.intersection(result)):
        raise RecipeError("Non-enquiry results omit captured fields and use not_applicable/false.")
    if result["completeness"] == "complete" and (not captured.issubset(result) or not result["callbackNumberConfirmed"]):
        raise RecipeError("Complete results require name, message, number and confirmation.")
    if result["callbackNumberConfirmed"] and "callbackNumber" not in result:
        raise RecipeError("Confirmed result requires a callback number.")
    if not isinstance(result["evidence"], list) or not isinstance(result["uncertainties"], list):
        raise RecipeError("Evidence and uncertainties must be arrays.")
    fields = ("consent", "callerName", "message", "callbackNumber", "callbackReadback", "callbackConfirmation", "outcome")
    for item in result["evidence"]:
        if (not isinstance(item, dict) or set(item) != {"field", "speaker", "quote"}
                or item["field"] not in fields or item["speaker"] not in ("caller", "assistant")
                or not isinstance(item["quote"], str) or not item["quote"]):
            raise RecipeError("Evidence items require a permitted field, speaker and non-empty quote.")
    if any(not isinstance(item, str) or not item for item in result["uncertainties"]):
        raise RecipeError("Uncertainties must contain non-empty strings.")
