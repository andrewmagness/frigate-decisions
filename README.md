# frigate-decisions

A small sidecar that asks the **Gemini Decisions API** (`gemini-decision-1`) typed questions about
[Frigate](https://frigate.video) events. It publishes the calibrated answers to MQTT, and they show up in
Home Assistant as sensors.

The Decisions API sends one snapshot, clip or frame with any number of questions:

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
cp .env.example .env                       # add your Gemini key, MQTT and Frigate URL
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

## Configuration

Environment variables are listed in [`.env.example`](.env.example). Question sets, camera rules and schedules go in
[`questions.example.yaml`](questions.example.yaml). Home Assistant automation examples are in [`ha/`](ha).

Tips:
- Use a key from a **dedicated Google Cloud project**. Decisions rate limits apply per project, not per key.
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
