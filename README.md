# API Aggregator

AstrBot plugin for managing API groups, testing APIs, configuring message triggers, and exposing one LLM tool:

- `api_aggregator_call`

## Scope

This plugin is responsible for:

- managing API definitions and groups
- testing API requests from the Plugin Page
- matching configured message triggers
- calling upstream HTTP APIs
- normalizing API responses into structured internal data
- returning structured tool results to the Agent

This plugin is not responsible for:

- deciding how the Agent should consume media URLs after tool return
- multimodal follow-up orchestration
- downstream media execution logic outside AstrBot trigger direct-send flow

## Current Downstream Paths

1. Trigger path
   - plugin directly sends text/image/audio/video through AstrBot send results

2. Tool path
   - plugin returns structured JSON result to the Agent through `api_aggregator_call`

3. WebUI path
   - plugin returns JSON state/results to the Plugin Page frontend

## Tool

Tool name:

```text
api_aggregator_call
```

Parameters:

```text
group
strategy
response_path
```

## Response Model

Current response normalization categories:

- `json`
- `text`
- `binary`

Rules:

- `response_path` only applies to JSON responses
- non-JSON text is returned as text data
- binary responses are wrapped as structured metadata instead of forced UTF-8 preview text

## Files

- `main.py`: plugin backend
- `metadata.yaml`: plugin metadata
- `pages/dashboard/`: Plugin Page frontend
- `ARCHITECTURE.md`: current execution model
- `OPEN_QUESTIONS.md`: deferred system-level issues outside plugin scope
- `TESTING.md`: manual test notes

