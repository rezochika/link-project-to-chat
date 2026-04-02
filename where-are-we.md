# State

## Done
- Streaming responses with rate-limited edits (2s throttle)
- Inline keyboard buttons on `/tasks` (cancel/log) and `/reset` (confirm/cancel)
- File upload support (photos + documents) — saved to `{project}/uploads/`
- Unsupported message type replies (voice, sticker, video, location, contact, audio)
- `/help` command
- `/model` and `/effort` commands for runtime model and thinking depth selection
- Session persistence across restarts
- `/compact` for context compression
- `/reset` with confirmation dialog
- Image detection and auto-send on Claude tool use
- Username-based auth with trusted user_id locking (numeric ID stored globally)
- Bot refuses to start without a configured username
- httpx logs suppressed to prevent Telegram token leaking
- `configure --username` CLI command for one-time global setup

## Pending
- Stream state (`_stream_messages`, `_stream_text`) not cleaned up on cancel — accumulates over time
- Open file handles in `_send_image` (`open(path, "rb")` passed directly without closing)
- File uploads stored permanently in project dir — consider `/tmp/{project_name}/` for temp files
- Stale `reset_confirm` keyboard button after restart silently resets even without an active session
- `_proc` on `ClaudeClient` is a single slot — concurrent Claude tasks to the same chat could overwrite it
