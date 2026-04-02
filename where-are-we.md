# State

## Done
- Streaming responses with rate-limited edits (2s)
- Inline keyboard buttons on `/tasks`, `/reset`
- File upload support (photos + documents)
- Unsupported message type replies (voice, sticker, video)
- `/help` command
- Session persistence and `/compact` for context compression
- `/reset` with confirmation dialog
- Image detection and auto-send on tool use
- Code cleanup: removed all comments and docstrings, simplified imports

## Pending
- `subprocess.Popen` + `asyncio.to_thread` not yet tested end-to-end
- `_proc` on `ClaudeClient` is a single slot — concurrent messages to the same chat could overwrite it
