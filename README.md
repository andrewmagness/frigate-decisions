# frigate-decisions

A small sidecar that asks a **decisions API** typed questions about [Frigate](https://frigate.video) events.
It publishes the calibrated answers to MQTT, and they show up in Home Assistant as sensors.

Supported decisions APIs:
- Google Gemini Decisions (`gemini-decision-1`, the default)
- OpenAI Decisions (`gpt-6-luna`)
- TypeSafe Jev
- OpenRouter's decisions endpoint, which routes to more decision models (see [Providers](#providers))

Each request sends one snapshot, clip or frame with any number of questions:

- `noul`: yes/no, answered as a probability.
- `choice`: one of N options, with a probability for each option.
- `score`: a rating on an ordered scale, answered as a weighted level.

It returns **probabilities instead of text**. All questions run in a single pass and there are zero output tokens. Your
automations can then branch on thresholds like `> 0.8` instead of parsing LLM prose.

It is designed to sit **next to** Frigate's own GenAI review summaries, not replace them:
- Frigate's GenAI describes events for people.
- frigate-decisions answers the questions your automations need to decide what to do.

```
frigate/reviews ──► frigate-decisions ──► Frigate API (snapshot / clip / latest.jpg)
                          │
                          └─► Decisions API ──► frigate/decisions/<camera>/<question>  (retained state)
                                                frigate/decisions/events                (one message per run)
                                                homeassistant/sensor/...                 (MQTT discovery)
```

## Triggers

| Trigger | Configured by | Media |
|---|---|---|
| Frigate review messages (`new`, `update`, `end`, `genai`) | `cameras.<name>.on_review` | event snapshot, event clip (`end`/`genai`), or latest frame |
| Time of day (optionally on certain weekdays) | `schedules` | latest frame per camera |
| On-demand request over MQTT | publish to `frigate/decisions/request` | `image_url` you supply, or the camera's latest frame |

Rules can filter by severity, label, zone and a per-camera cooldown. On `genai` reviews the context template can include
Frigate's own `{genai_title}` and `{genai_scene}`. Recognised faces and plates are passed in as `{sub_labels}`, which
lets questions such as "is this a household member?" use more than the pixels.

## Quick start

```bash
cp .env.example .env                       # choose a provider, add its API key, MQTT and Frigate URL
mkdir -p config && cp questions.example.yaml config/questions.yaml
cp docker-compose.example.yml docker-compose.yml   # set the image owner, or use build: .
docker compose up -d
docker compose logs -f
```

Sensors appear in Home Assistant as `sensor.frigate_decisions_<camera>_<question>`. The state is:
- the probability, for `noul`;
- the chosen option key, for `choice`;
- the probability-weighted level index (0 = first level), for `score`.

Each sensor also has `confidence` and `probabilities` attributes.

### On-demand request

```json
{"request_id": "abc123", "set": "bins", "camera": "garage",
 "image_url": "http://homeassistant.local:8123/local/snapshots/bins.jpg",
 "context": {"note": "extra text available as {note} in the set's context"}}
```

The result arrives on `frigate/decisions/events` with the same `request_id`.

### Event message

```json
{"source": "review", "camera": "front_door", "set": "arrival", "review_id": "...",
 "severity": "alert", "labels": ["person"], "sub_labels": [], "zones": ["porch"],
 "answers": {"real_person": {"type": "noul", "value": 0.98, "confidence": 0.98},
             "who": {"type": "choice", "value": "delivery", "confidence": 0.91, "probabilities": {}},
             "concern": {"type": "score", "value": 2.1, "confidence": 0.74, "probabilities": {}, "legend": {}}},
 "usage": {"total_input_tokens": 1450}, "elapsed_ms": 820, "ts": 1700000000.0}
```

If a run fails, the message has `error` instead of `answers`.

## Providers

Set `DECISIONS_PROVIDER` and `DECISIONS_API_KEY`. Question sets are written once, in the format below, and translated
for each provider. Answers come back in the same normalized form, so sensors and automations don't change when you
switch providers.

| `DECISIONS_PROVIDER` | Service | Default model | Media | Limits checked at startup |
|---|---|---|---|---|
| `gemini` (default) | Google Gemini Decisions, `POST /v1beta/decisions` | `gemini-decision-1` | image, video clip, audio, PDF | 1,000 questions; 255 score levels |
| `openai` | OpenAI Decisions, `POST /v1/decisions` | `gpt-6-luna` | images | 400 questions; 10 score levels |
| `systemone` | TypeSafe Jev, `POST /v1/systemone`, and compatible gateways | `jev-latest` | images (model-dependent) | 10 score levels |

For OpenRouter, which routes to several decision models (Jev, Upstage Solar Decide and others), use:

```env
DECISIONS_PROVIDER=systemone
DECISIONS_API_KEY=<openrouter key>
DECISIONS_BASE_URL=https://openrouter.ai/api
DECISIONS_PATH=/alpha/decisions
DECISION_MODEL=typesafe/jev-1.14
```

Notes:
- **Re-tune thresholds when you switch providers.** Calibration differs between models, and a `0.8` from one doesn't mean the same as a `0.8` from another.
- Only the `gemini` provider has been run against the live API so far. The `openai` and `systemone` adapters are tested against the documented request and response formats. If you use them, issue reports and fixes are welcome.
- Anthropic has no native decisions API. This project does not fake one by prompting a chat model, because those probabilities wouldn't be calibrated.

## Configuration

Environment variables are listed in [`.env.example`](.env.example). Question sets, camera rules and schedules go in
[`questions.example.yaml`](questions.example.yaml). Home Assistant automation examples are in [`ha/`](ha).

Tips:
- Use a dedicated project or key for this service. With Gemini, rate limits apply per Google Cloud project, not per key.
- Always give `choice` questions a catch-all option, because the probabilities must sum to 1 even when nothing fits.
- Tune thresholds on your own footage. The API has no feedback endpoint, so calibration lives in your automations.
- Safety-related questions, such as a child near a pool, are an **extra signal only**. Never use them as a substitute for
  physical safety measures.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e .[dev]
.venv/bin/pytest
```

## License

MIT
