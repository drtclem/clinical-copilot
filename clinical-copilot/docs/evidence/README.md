# Observability evidence (production)

What the Co-Pilot records for every chat turn, with real artifacts. The log sample and the per-day
trace counts were taken from the production droplet on **2026-09-26**, while it ran commit
**`5734e1f`** (MR !14). Production now runs **`8d65b3b`** (MR !16), which changes no logging. The Langfuse screenshot is from a **local run, not production** (see below). All
patients are synthetic fixtures, and nothing here contains a raw patient identifier, document text or
free-text model output.

| File | What it is |
|---|---|
| `langfuse_trace_local.png` | Langfuse trace from a local run, 2026-09-25, same code path as prod. A chart-only turn: the nested span tree (`chat_turn` → `worker:chart_data_worker` → `llm_call_N` / `tool:*`) with latency, token counts and cost per span, and the turn's `eval_outcome`, `domain_constraint_pass` and `source_attribution_pass` scores. The patient id, trace id and public key are covered. |
| `prod_turn_log_sample.jsonl` | The complete redacted journald output (`journalctl -u clinical-copilot`) for one production turn, correlation id `cd79dcda-…`, captured 2026-09-26 20:44 UTC: `turn_started`, `routing_decision`, `llm_call`, `tool_call`, `citation_counts`, `verification`, `turn_summary`, `turn_finished`. |

Prod's Langfuse UI listens on loopback only, and its public query API is disabled in Langfuse v4
`events_only` mode (`W2_ARCHITECTURE.md` §4.3), so the production evidence below is the journald
sample plus a direct count of stored traces. The screenshot is from a local Langfuse instead.

## Production is traced: trace counts per day

Read-only count in prod Langfuse's ClickHouse (`events_core`, distinct `trace_id`; the `traces`
table is empty in `events_only` mode), taken 2026-09-26 22:08 UTC. Numbers only:

| Day (UTC) | Traces | Root `chat_turn` spans |
|---|---|---|
| 2026-09-24 | 8 | 8 |
| 2026-09-25 | 15 | 15 |
| 2026-09-26 (until 20:45 UTC) | 22 | 22 |

## Where each PRD Core Requirement 7 field appears

| Required field | In the log sample (`prod_turn_log_sample.jsonl`) | In Langfuse (screenshot: local, chart-only turn) |
|---|---|---|
| Tool sequence | `routing_decision.workers` (`chart_data_worker`, `intake_extractor`) and each `tool_call` event (`tool`, `arg_names`, `latency_s`, `failed`, `retry_count`) | `worker:*` and `tool:*` spans, in order (visible in the screenshot) |
| Latency by step | `turn_summary.node_metrics.<node>.latency_ms`, plus per-call `llm_call.latency_s` and `tool_call.latency_s` | span durations (visible) |
| Token usage | `llm_call.input_tokens` / `output_tokens`; `turn_summary.node_metrics.<node>` | per-span token counts (visible) |
| Cost estimate | `turn_summary.cost_usd` and `node_metrics.<node>.cost_usd` | per-span and per-trace cost (visible); `turn_cost_usd` score |
| Retrieval hits | `turn_summary.retrieval_hits` (`null` on this turn: no guideline retrieval was routed) | `worker:evidence_retriever` span (not in this screenshot) |
| Extraction confidence | `turn_summary.extraction_confidence` (one `high` / `low` / `unreadable` tier per extracted field) | `worker:intake_extractor` span (not in this screenshot) |
| Eval outcome | `turn_summary.eval_outcome`; the `verification` event shows the checks behind it | `eval_outcome` score (visible) |

## What the routing log shows, and what it deliberately does not

`routing_decision` records **which workers the supervisor chose** (`workers`) and the **length of its
rationale** (`rationale_length`). The rationale text itself is not written to the log, because the
model's free text can restate clinical terms from the chart (`app/log_redaction.py`). The full
rationale is stored in the Langfuse trace, in the supervisor's routing LLM call.

## Redaction, and an older-logs caveat

In the sample, `patient_id` and `resident_username` appear only as salted hashes (`h_<12 hex>`,
`LOG_HASH_SALT` is set on prod), tool inputs are reduced to `arg_names`, and `verification` carries
a count rather than the flagged claims. The sample also passes the repo's own PHI grader
(`evals.gate.find_phi_in_logs`, the one behind the `no_phi_in_logs` rubric) with no findings.

Journald on prod also still holds older lines, written before the current build started
(prod's last restart was 2026-09-26 16:37 UTC), that carry raw fixture patient UUIDs, drug names and rationale text. No
line in this directory comes from that period.
