# Messenger Assistant Recipe

A reusable recipe for operators to configure, deploy and manage message-taking voice assistants for businesses. Operators need basic familiarity with Git, Python, terminal commands and configuration files. This standalone package includes its own templates, configuration, deployment CLI and offline tests, with native Vapi Structured Output.

This recipe supports English message-taking for real businesses. The included example is Mira at fictional Pebble Workshop and uses synthetic caller data for testing. It takes a message, a preferred name and a spoken callback number. Business notifications and a custom demo interface are future work.

## Clone and install

Requires Python 3.11 or later. Tested on Ubuntu under WSL with Python 3.12. Windows users should use WSL; native Windows is not currently supported. macOS compatibility has not yet been verified.

**CLI action:** clone the public repository and install its dependencies.

```bash
git clone https://github.com/PatrickMichael-AI/messenger-voice-recipe.git
cd messenger-voice-recipe
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Run the remaining commands from this package directory. The package uses direct HTTP through `httpx`; it does not require the Vapi SDK or another project's CLI, dashboard or Python environment.

## Configure the business and voice

**Operator action:** edit [instances/messenger_recipe_demo/recipe.json](instances/messenger_recipe_demo/recipe.json).

| Setting | What to enter |
| --- | --- |
| `business.name` | The business name callers will hear. |
| `business.fictional` | `false` for your real business; `true` for a fictional test business. |
| `business.assistantName` | The assistant's name. |
| `voice.provider`, `voice.voiceId`, `voice.model` | The exact ElevenLabs voice you approve for this named assistant. The included example uses `11labs`, `NTqGiNK8P02i66yY2GOH`, `eleven_flash_v2_5`. |
| `privacy.recordingEnabled` | Your explicit audio-recording choice. Set [config/runtime.json](config/runtime.json) `artifactPlan.recordingEnabled` to the same value. The example records audio; video is off. |
| `privacy.liveData` | `synthetic_only` for test calls; `real_callers` when taking calls with real caller details. |

Keep `slug` equal to its instance directory name. Leave `reviewStatus` as `pending`: approvals belong to private local state. Keep the fixed purpose, English language and result contract. This version does not configure business services, knowledge, bookings or transfers.

**Operator action plus local CLI action:** after choosing the exact voice, record that decision for this instance.

```bash
.venv/bin/python messenger.py select-voice --confirm
```

An example voice in a configuration file is not an approval. Changing the instance, assistant name or voice requires a new selection. This command records local approval and makes no provider request.

If Vapi uses your own ElevenLabs account, make sure the selected voice is in **My Voices** in that connected account. Add a Voice Library voice to My Voices before deployment, then copy its voice ID from there. An ID from the library alone does not establish availability through your connected account. [ElevenLabs Voice Library instructions](https://elevenlabs.io/docs/eleven-creative/voices/voice-library).

## Review and prepare

**CLI action:** validate and stage the candidate, then print the complete offline deployment review.

```bash
.venv/bin/python messenger.py check
.venv/bin/python messenger.py stage
.venv/bin/python messenger.py deploy
```

Without `--live`, `deploy` makes no provider request. Its output includes `assistantCandidate`, `outputDefinition`, a review hash and promotion status. The candidate has no provider IDs or output binding. `preview` also prints the rendered prompt, opening and assistant settings.

**Operator action:** review the complete speaking prompt, opening, voice, runtime and separate extraction definition. Check the staged files under `.local/messenger_recipe_demo/`. The opening is generated from the configured names:

> Hello, I'm Mira, the automated message assistant for Pebble Workshop. Would you like to leave a message for the team?

The assistant confirms the message, collects any missing name or callback number, reads every number digit slowly and asks for confirmation. When a caller repeats a number segment, it asks for the full number again. It accepts refusals and early endings. Its normal closing finishes with “Have a great day.”, which triggers the end of the call.

**Operator action plus local CLI action:** approve and promote the reviewed prompt, opening and runtime.

```bash
.venv/bin/python messenger.py promote --confirm
```

Promotion validates the staged sources, all 21 prompt sections and the phrase-triggered close. It writes canonical prompts and approval records under `.local/`, preserving replaced prompts in backups. It does not deploy or approve the extraction definition for live use.

## Deploy to Vapi

**Operator action:** review the `outputDefinition` from offline `deploy` and approve the assistant/output provisioning. Review your Vapi and model/voice provider costs and retention settings. Set `VAPI_API_KEY` in your shell using your credential manager or environment setup. [.env.example](.env.example) gives the variable name; the CLI does not load `.env` automatically. A direnv setup must load its environment in the same shell before running a live command.

**Live CLI action:** deploy with one command.

```bash
.venv/bin/python messenger.py deploy --live
```

The command creates or verifies one native output, attaches its accepted ID, and creates or updates one assistant. Each accepted mutation needs an independent GET that matches the authored settings. IDs, raw provider responses and recovery records stay under `.local/<instance>/`.

Running the command again reuses accepted IDs. An unchanged assistant needs no PATCH. Unexplained remote changes or a changed output definition stop execution for review. Creation requests are never retried automatically.

## Test with Vapi Talk

**Operator action:** open the deployed assistant in Vapi. Review any publication diff and publish the intended settings if required, then use Talk with synthetic caller details.

Try a normal message with name and number, a corrected message or number, segmented digits, a refused detail, an early end, a withdrawn message and an unsupported request. Check the spoken number and both closing paths. The normal message close is “Thank you, [Caller name]. I have noted your request and will pass your message on to the team. Have a great day.” A caller who declines or chooses to call back gets the company acknowledgement instead. Unknown names are omitted.

**Read-only live CLI action:** verify the API settings after publication.

```bash
.venv/bin/python messenger.py verify-deployment
```

This compares the current API resources with accepted intent. It does not prove which version a call used or how that call sounded.

**Read-only live CLI action:** retrieve one exact call belonging to this assistant.

```bash
.venv/bin/python messenger.py fetch-call <call-id>
```

**Offline CLI action:** inspect the saved native result.

```bash
.venv/bin/python messenger.py inspect-call <call-id>
```

**Operator action:** review the saved conversation, recording and native result together. A `valid_shape_semantics_unproved` report means the result shape passes; it does not prove message meaning, correct digits or confirmation. Repeated retrieval needs an explicit `--attempt 2` or `--attempt 3` on both commands; earlier snapshots are preserved.

Talk is the included test route. Buying or attaching a telephone number, inbound routing and telephony charges are your responsibility in Vapi or your telephony provider; this package does not provision them.

## Results and privacy

[config/structured-output.json](config/structured-output.json) owns extraction instructions and model selection. [result/enquiry-result.v2.schema.json](result/enquiry-result.v2.schema.json) owns the current canonical result contract, version `2.0.0`. The Recipe package version remains `0.1.0`.

| Result | Meaning |
| --- | --- |
| `enquiry` / `complete` | A useful confirmed message, supplied name and number, and confirmation after the latest complete exact number readback. |
| `enquiry` / `partial` | Message taking accepted, but a required detail or confirmation is missing or uncertain. |
| `declined` / `not_applicable` | Message taking refused or withdrawn; personal and message fields are omitted. |
| `unsupported` / `not_applicable` | An unsupported request without an accepted message. |
| `no_message` / `not_applicable` | No substantive request or established message-taking consent. |

Numbers come only from caller speech, including when the caller says “use this number”; inbound caller ID is not used. Corrections replace earlier values and require fresh confirmation. Choosing to call back retains an already permitted message unless the caller withdraws it. Saved messages are written for the receiving team and preserve whose request or idea is being discussed.

Consent evidence must quote the caller with `speaker: "caller"`. An enquiry includes the caller's acceptance of message taking, such as “Yes, please”, or a direct request, such as “Please ask the team to discuss my idea”. Mira's offer, acknowledgement or closing cannot establish caller permission; silence or a name or number alone does not establish it either. One caller quote may support both `consent` and `message`. Later refusal or withdrawal overrides earlier permission and is evidenced by the caller's own words; suppressed personal details stay excluded. The extractor must not invent acceptance or a consent quote when none exists.

Every current result includes separate Boolean `messageConfirmed` and `callbackNumberConfirmed` fields. A complete enquiry requires both to be true. `messageConfirmed` means the caller explicitly accepted the latest proposed message after the assistant read it back and asked “Is that correct?”. Evidence labels `messageReadback` and `messageConfirmation` identify that assistant turn and caller response; supplied content and corrections use `message`. A correction or addition resets message confirmation until the changed summary is confirmed. Agreement to a number, silence and “no additions” do not confirm a message. A clear unconfirmed request remains partial with `messageConfirmed: false`; an unclear message is omitted. Non-enquiry outcomes omit captured values and set both flags false.

Raw Vapi results use empty strings for absent `callerName`, `message` and `callbackNumber`. The included [normalizer and validator](messenger_recipe/output.py) converts only those empty values to omissions and checks the canonical schema. A future consumer must apply the same normalization and validation. Missing extraction is a processing failure, not a fabricated `no_message` result.

**Operator action:** review consent quotes against the caller turns alongside the existing message and number checks. Shape validation alone cannot establish permission or whether the extractor followed this evidence rule.

Saved `1.0.0` results remain readable under the [historical schema](result/enquiry-result.v1.schema.json). They retain their version and evidence labels; the reader never adds or infers `messageConfirmed`. Their `complete` value does not establish explicit message confirmation. New deployments require `2.0.0`.

Generated prompts, approvals, credentials, provider IDs and call evidence are excluded from Git by [.gitignore](.gitignore). Recordings, transcripts and results can contain caller details. The configuration records a 14-day local content-retention target, but no automatic deletion runner is included. Provider retention is managed separately.

## Use another business

**CLI action:** copy the example configuration into a new instance directory.

```bash
mkdir -p instances/maple_demo
cp instances/messenger_recipe_demo/recipe.json instances/maple_demo/recipe.json
```

**Operator action:** in the new JSON, set `slug` to `maple_demo`, `business.name` to `Maple Studio` and `business.assistantName` to `Robin`. Choose and approve its exact voice. Keep the supported fixed contract. Use a short slug: the generated output name `<slug>-enquiry-v2` must fit within 40 characters.

**Operator action plus local CLI action:** record this instance's voice selection.

```bash
.venv/bin/python messenger.py --instance maple_demo select-voice --confirm
```

**CLI action:** prepare and review it through the same implementation.

```bash
.venv/bin/python messenger.py --instance maple_demo check
.venv/bin/python messenger.py --instance maple_demo stage
.venv/bin/python messenger.py --instance maple_demo deploy
```

**Operator action:** review the new candidate and extraction definition. Its opening becomes “Hello, I'm Robin, the automated message assistant for Maple Studio. Would you like to leave a message for the team?”

**Operator action plus local CLI action:** after review, run `messenger.py --instance maple_demo promote --confirm` with `.venv/bin/python`. A live deployment then requires separate provisioning approval and `messenger.py --instance maple_demo deploy --live`. Each instance keeps its own `.local/<slug>/` state. Never copy another instance's approvals or provider IDs.

## Update and recover

**Operator action:** edit the instance, bundled template or runtime sources. Review a deliberate template refresh against [templates/provenance.json](templates/provenance.json); the renderer rejects templates whose hashes differ from that record.

**CLI action:** preserve the earlier review artifacts and restage.

```bash
.venv/bin/python messenger.py stage --refresh
.venv/bin/python messenger.py deploy
```

**Operator action plus CLI actions:** review the changed preview, run `promote --confirm`, then run `deploy --live` after approving the live change. Changes to the instance, speaking template, opening or runtime invalidate local prompt promotion. Use `--instance <slug>` before each subcommand for another instance.

**Operator action plus live CLI action:** if the extraction definition changed, review the new `outputDefinition` and use this command instead of ordinary `deploy --live` to explicitly update the existing output in place. An extraction-only change does not require another speaking-prompt promotion.

```bash
.venv/bin/python messenger.py deploy --live --update-output
```

This retains the output and assistant IDs, refuses unexplained remote drift, archives the previous accepted definition and verifies the update by GET. A lost PATCH response can be reconciled by a later matching GET without another PATCH.

**Operator action — upgrade an existing `1.0.0` instance:** set its `result.version` to `2.0.0` and `result.schemaRef` to `result/enquiry-result.v2.schema.json`. Review the new extraction definition and separate message-confirmation evidence. The instance file participates in the approval hash, so this contract upgrade requires restaging and local promotion even when the speaking prompt text is unchanged.

**CLI action:** run `stage --refresh` and offline `deploy` as above. **Operator action plus local CLI action:** review that preview, then run `promote --confirm`. **Operator action plus live CLI action:** after approving the live update, run `deploy --live --update-output`. Keep existing bindings and saved calls; do not rewrite historical results. **Operator action:** before release, use Talk to check a confirmed message, a corrected message with fresh confirmation, and a clear message ending before confirmation. Review each saved result alongside the conversation and recording.

If creation stopped with an uncertain response, review the private diagnostics and identify the exact resource in Vapi. **Operator action plus live CLI action:** supply the matching ID for reconciliation.

```bash
.venv/bin/python messenger.py deploy --live --reconcile-output-id <output-id>
.venv/bin/python messenger.py deploy --live --reconcile-assistant-id <assistant-id>
```

The uncertain resource is reconciled with GET only. The command can then continue the remaining approved deployment, including assistant creation after output recovery. A confirmed validation rejection is archived without retry; correct the source and review before explicitly deploying again. Keep conflicting local state for review.

A development export replaces only an explicit public file set, preserves destination Git, credentials and `.local/`, and refuses conflicting destination edits. It does not deploy updated files automatically. Public users can update through Git after preserving their configuration changes, then review and restage before any deployment. Preserve `.local/` to retain bindings and avoid duplicate resources.

## Limits and future work

The package has local tests and initial live assistant/result evidence. The full conversation matrix is unfinished. Repeated segments and full-number questioning still need more call testing; incorrect readback detection is not fully proven. Transcripts can omit spoken digits, so compare recordings when judging readback accuracy. Local schema validation cannot establish extraction accuracy.

This version supports English message-taking. Live testing so far used fictional businesses and synthetic caller data. Some retained dashboard settings are not described in Vapi's request documentation; deployment readback and direct calls are the checks for their behavior. Configuration validates the fixed message-only contract and close settings rather than accepting arbitrary assistant features.

No business notification, webhook consumer, automated retention runner or custom demo UI is included. Post-call processing and notification are planned separately with Vercel and Trigger.dev.

**Offline CLI action — optional development checks:**

```bash
.venv/bin/python -m unittest discover -s tests -v
```

These tests use synthetic fixtures and injected transports. They do not contact Vapi or prove audible behavior.

## Files and licence

| Path | Purpose |
| --- | --- |
| [instances/](instances/) | Business, voice and privacy configuration. |
| [config/runtime.json](config/runtime.json) | Conversation, recording, timing and closing settings. |
| [templates/](templates/) | Bundled prompt, opening and provenance. |
| [config/structured-output.json](config/structured-output.json), [result/](result/) | Extraction definition and canonical result schema. |
| [messenger.py](messenger.py), [messenger_recipe/](messenger_recipe/) | Standalone CLI and implementation. |
| [requirements.txt](requirements.txt), [tests/](tests/) | Pinned dependencies and offline checks. |
| `.local/<instance>/` | Private generated artifacts and provider evidence; excluded from Git. |

The project uses Apache License 2.0; the public repository contains the selected [LICENSE](https://github.com/PatrickMichael-AI/messenger-voice-recipe/blob/main/LICENSE). [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt) preserves dependency attribution and licence texts. Bundled template provenance is recorded in [templates/provenance.json](templates/provenance.json).
