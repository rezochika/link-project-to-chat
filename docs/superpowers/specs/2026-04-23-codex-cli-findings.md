# Codex CLI Findings

**Captured on:** 2026-04-25
**Binary:** `codex` (`/usr/bin/codex`)
**Version:** `codex-cli 0.125.0`
**Host:** Linux, bash

## Commands validated

```text
which codex
codex --version
codex --help
codex exec --help
codex exec resume --help
codex login status
codex exec --json --sandbox read-only "Reply with exactly OK and do not run any commands."
codex exec resume --json 019dc702-1602-7381-a86f-94950237eab4 "Reply with exactly AGAIN and do not run any commands."
codex exec resume --sandbox read-only --json 019dc702-1602-7381-a86f-94950237eab4 "test"   # confirmed rejected
```

## Observed behavior

- Non-interactive execution works through `codex exec --json`. The first run exited `0` and emitted four JSONL records on stdout in this exact order: `thread.started`, `turn.started`, `item.completed` (`item.type == "agent_message"`), `turn.completed`.
- Resume works through `codex exec resume --json <thread_id> <prompt>`. The CLI **reused the same thread id** (`019dc702-1602-7381-a86f-94950237eab4`) on the resumed turn — the resume run's `thread.started` event echoed the original id rather than minting a new one.
- `codex exec resume` does **not** accept `--sandbox`. Confirmed: passing `--sandbox read-only` produces `error: unexpected argument '--sandbox' found`. The resume option surface still includes `--model`, `--full-auto`, `--ephemeral`, `--dangerously-bypass-approvals-and-sandbox`, `--skip-git-repo-check`, `--ignore-user-config`, `--ignore-rules`, `--last`, `--all`, `--enable`, `--disable`, `--image`, and `--json`.
- Stdout is JSONL with top-level event types:
  - `thread.started` carrying `thread_id`
  - `turn.started`
  - `item.completed` with `item.type == "agent_message"` and a flat `text` field
  - `turn.completed` carrying `usage` with `input_tokens`, `cached_input_tokens`, `output_tokens`, and `reasoning_output_tokens`
- Successful runs **do** emit stderr noise. In this environment the stderr was:

  ```text
  Reading additional input from stdin...
  2026-04-25T23:38:19.857714Z ERROR codex_core::session: failed to record rollout items: thread 019dc702-1602-7381-a86f-94950237eab4 not found
  2026-04-25T23:38:22.955457Z ERROR codex_core::session: failed to record rollout items: thread 019dc702-1602-7381-a86f-94950237eab4 not found
  ```

  Note: this stderr is at `ERROR` level (not `WARN` like the plan template anticipated), but both runs still exited `0` with valid JSONL on stdout. The adapter must therefore treat stderr as informational and rely on exit status + stdout JSONL to judge success.
- No thinking delta stream was observed. `codex exec --help` and the top-level `codex --help` advertise no `--thinking`, `--reason`, `--allowed-tools`, `--permission`, or `--compact` flags.
- No tool-use event stream was observed in this minimal validation (the prompt explicitly forbade running commands). Tool events, if they exist, will need to be discovered when the model is allowed to invoke tools — Phase 3 should not assume any tool-use schema until then.
- Model selection exists as `--model` on both `codex exec` and `codex exec resume`, but the CLI help does not enumerate a fixed supported model list.
- `codex login status` exits `0` and prints `Logged in using ChatGPT` when authenticated.
- The CLI reads from stdin during `codex exec`; the message `Reading additional input from stdin...` appearing on stderr at startup is normal and not a failure signal.

## Captured `usage` fields

Both `turn.completed` events carry a `usage` object with these keys:

- `input_tokens` (int)
- `cached_input_tokens` (int)
- `output_tokens` (int)
- `reasoning_output_tokens` (int)

The presence of `reasoning_output_tokens` confirms the model reasons internally even though no thinking delta is exposed to clients. The Phase 3 adapter can surface usage totals but should not infer a thinking stream from this field.

## Initial capability conclusions

- `supports_resume = True` — `codex exec resume --json <thread_id>` works and reuses the original thread id.
- `supports_thinking = False` — no thinking delta stream and no `--thinking` flag.
- `supports_permissions = False` — no `--permission` / approval flag in the non-interactive surface.
- `supports_compact = False` — no `--compact` or session-management subcommand.
- `supports_allowed_tools = False` — no `--allowed-tools` / tool-allowlist flag.
- `supports_usage_cap_detection = False` — no usage-cap signal observed; the CLI emitted only token counts.
- `models = ()` for now — the CLI advertises `--model` but not a validated fixed list, and the plan-template `--model` cross-check (`codex exec resume --json --model gpt-5.4 ...`) was not exercised in this capture.
