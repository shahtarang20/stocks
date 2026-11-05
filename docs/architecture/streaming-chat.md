# Design note: streaming chat completions (B2)

Status: for owner approval (L8). Scope: `POST /v1/chat/completions` with `stream: true`, and the agent endpoint that relays real tokens.

## Wire format
- `Content-Type: text/event-stream`. Each event is `data: <json>\n\n`.
- First chunk `delta {"role":"assistant","content":""}`, then one chunk per piece with `delta {"content": "..."}`, then a chunk with empty `delta` and `finish_reason: "stop"`, then `data: [DONE]`.
- Every chunk is `object: "chat.completion.chunk"`, same `id` for the whole stream, plus the `computecoin` block (`execution_mode`, `model_version`, `image_digest`).
- An error after the stream started is one `data: {"error": {...}}` event and the stream ends WITHOUT `[DONE]`. Errors before the first byte are normal HTTP errors (same bodies as non-streaming).

## Simulated models
The text is the same string the non-streaming path returns, cut into pieces of `INFERENCE_STREAM_CHUNK_CHARS`. Joining the deltas gives exactly the non-streamed content. `execution_mode` stays `"simulated"`.

## Real models
- The job is created exactly as for non-streaming (billing unchanged: normal hold, capture on completion, metered seconds).
- Token relay: the agent POSTs `/v1/agents/jobs/{job_id}/stream` (machine HMAC, same as the other agent calls) with `{"seq": n, "text": "...", "final": bool}`. `seq` starts at 0 and must be exactly the next number. A gap or a reordering (seq above or below the expected one with different content) records an error on the stream, answers 409 `stream_sequence_error:<reason>` (for example `sequence_gap`, `reordered_or_modified_chunk`, `chunk_after_final`, `stream_too_large`), and the customer stream ends with an error event and the job is cancelled. An exact repeat of an already accepted chunk is accepted and ignored (safe retry).
- The relay is in memory in the API process and only accepts chunks for a stream a customer request has opened (no unbounded buffering; capped at `INFERENCE_STREAM_MAX_CHARS`). Several API workers need a shared bus before this is enabled behind a load balancer: use `STREAM_RELAY_BACKEND=redis` (see the end of this note); the default `memory` backend is process-local.
- If the agent sends no chunks, the stream falls back to cutting the final `output.json` text, so a customer always gets the full text. If chunks were relayed, the final text must start with them; otherwise the stream ends with an error.

## Disconnect
When the client disconnects, the generator is closed; its `finally` cancels the job (when not terminal), releases the reservation and releases the whole hold (a cancelled job is never charged). Same code path as a timeout.

## Private tiers
The request accepts an optional `isolation_tier`. For `sealed`, `sealed_pro`, `confidential`:
- `stream: false` is always refused 400 `private_tier_requires_streaming` (a private tier is never silently run as an ordinary container job, which would send the plaintext messages to the host).
- `stream: true` is refused 400 `streaming_not_supported_for_private` unless `PRIVACY_STREAMING_ENABLED` is on and privacy is active (`privacy_active`). When it is on, the end-to-end sealed stream of the U18 design note applies: the request carries `encrypted_input`, `dk_shares` and `result_public_key` plus the single placeholder message `sealed`, and the SSE events are `chat.completion.chunk.sealed` envelopes the relay cannot read.
- On the agent endpoint, a private job relays only sealed chunks (`sealed`, `text` empty); a private job whose stream was not opened sealed is refused 409 `streaming_not_supported_for_private`, and a plain chunk for a private job, or a sealed chunk for an ordinary job, is refused 409 `stream_sealed_mismatch`.

## Tests
Chunk format and order; `[DONE]`; streamed text equals non-streamed; disconnect cancels and refunds; private refused; agent endpoint rejects unsigned, gapped and reordered chunks.

## Agent streaming contract (B2 wiring)

For a real (hardware) `text-generation` job the agent runs a per-job monitor (`provider-agent/computecoin_agent/jobmonitor.py`).
The workload and the agent share two files in the job's output directory (`/out` inside the container; the env vars
`COMPUTECOIN_STREAM` and `COMPUTECOIN_PROGRESS` hold the paths):

- `stream.ndjson`: append-only, one JSON object per line, `{"text": "<new text>", "final": false}`; the last line is
  `{"text": "", "final": true}`. The agent tails complete lines only, numbers the chunks from 0 in file order and POSTs them to
  `/v1/agents/jobs/{job_id}/stream` (signed). A sequence error (409) stops streaming; the job itself carries on and the final
  output still reaches the customer. If the workload ends without a final line the agent sends an empty final chunk.
- `progress.json`: optional, replaced atomically, `{"progress_pct": 0-100, "step_count": int, "phase": str}`; sent with real GPU
  utilisation and VRAM to `POST /v1/telemetry/job/progress`. Without it the agent reports 0 % and never invents a percentage.

`STREAM_RELAY_BACKEND=memory|redis` (default `memory`) selects the API-side relay. `redis` shares chunks between API workers
(uses `REDIS_URL`; the `redis` package is imported only then).
