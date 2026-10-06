"""Lossless assistant candidate composition; no filesystem or SDK coupling."""

from copy import deepcopy

from .provider import resource_id


FORBIDDEN_ROOT_FIELDS = {
    "id", "orgId", "createdAt", "updatedAt", "latestVersion", "isServerUrlSecretSet",
    "hipaaEnabled", "recordingEnabled", "recordingPlan", "endCallPlan",
}


def render_assistant(runtime: dict, *, name: str, prompt: str, opening: str,
                     voice: dict, output_ids: list[str] | None = None) -> dict:
    """Copy authored runtime, replace prompt/voice/IDs; never drop empty/null values.

    No output_ids means provisional, unbound composition. A deployment caller
    must separately require a verified non-empty binding before any live use.
    A supplied empty list means explicit clearing, rather than omission.
    """
    if not isinstance(runtime, dict) or FORBIDDEN_ROOT_FIELDS.intersection(runtime):
        raise ValueError("Runtime contains unsupported or provider-owned root fields.")
    if not all(isinstance(value, str) and value.strip() for value in (name, prompt, opening)):
        raise ValueError("Name, system prompt and opening must be non-empty.")
    if not isinstance(voice, dict) or not all(voice.get(key) for key in ("provider", "voiceId", "model")):
        raise ValueError("An exact selected voice provider/ID/model is required.")
    result = deepcopy(runtime)
    if not isinstance(result.get("model"), dict) or not isinstance(result.get("voice"), dict):
        raise ValueError("Runtime requires model and voice objects.")
    if not isinstance(result.get("artifactPlan"), dict):
        raise ValueError("Runtime requires artifactPlan.")
    result["name"] = name
    result["firstMessage"] = opening
    result["model"]["messages"] = [{"role": "system", "content": prompt}]
    result["voice"].update(deepcopy(voice))
    if output_ids is not None:
        if not isinstance(output_ids, list) or len(output_ids) != len(set(output_ids)):
            raise ValueError("Output IDs must be a list without duplicates.")
        result["artifactPlan"]["structuredOutputIds"] = [resource_id(value) for value in output_ids]
    return result
