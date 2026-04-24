# Team Relay Lifecycle Test Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update the Telegram transport relay lifecycle test so it matches the current `TeamRelay` contract with both new-message and edited-message handlers.

**Architecture:** Production code already registers two Telethon handlers and removes two callbacks. The test should assert that both callbacks are registered and later removed, using callback method names so it remains independent of Telethon event-builder object identity.

**Tech Stack:** Python 3.11+, pytest async tests, `unittest.mock.MagicMock`, python-telegram-bot test doubles, Telethon event builders.

---

## File Structure

- Modify `tests/transport/test_telegram_transport.py`: update `test_enable_team_relay_lifecycle` assertions.

### Task 1: Update Lifecycle Assertions

**Files:**
- Modify: `tests/transport/test_telegram_transport.py:526-530`

- [ ] **Step 1: Run the failing lifecycle test**

Run:

```bash
python -m pytest tests/transport/test_telegram_transport.py::test_enable_team_relay_lifecycle -q
```

Expected before the test update:

```text
AssertionError: Expected 'add_event_handler' to have been called once. Called 2 times.
```

- [ ] **Step 2: Replace the stale assertions**

Replace:

```python
    await t.start()
    mock_client.add_event_handler.assert_called_once()

    await t.stop()
    mock_client.remove_event_handler.assert_called_once()
```

with:

```python
    await t.start()
    assert mock_client.add_event_handler.call_count == 2
    registered_callbacks = [
        call.args[0].__name__
        for call in mock_client.add_event_handler.call_args_list
    ]
    assert registered_callbacks == ["_on_new_message", "_on_message_edited"]

    await t.stop()
    assert mock_client.remove_event_handler.call_count == 2
    removed_callbacks = [
        call.args[0].__name__
        for call in mock_client.remove_event_handler.call_args_list
    ]
    assert removed_callbacks == ["_on_new_message", "_on_message_edited"]
```

- [ ] **Step 3: Run the focused lifecycle test**

Run:

```bash
python -m pytest tests/transport/test_telegram_transport.py::test_enable_team_relay_lifecycle -q
```

Expected:

```text
1 passed
```

- [ ] **Step 4: Run the Telegram transport tests**

Run:

```bash
python -m pytest tests/transport/test_telegram_transport.py -q
```

Expected:

```text
23 passed
```

- [ ] **Step 5: Commit**

Run:

```bash
git add tests/transport/test_telegram_transport.py
git commit -m "test: update team relay lifecycle assertions"
```

## Self-Review

Spec coverage: Finding 4 is covered by changing the test contract from one handler to the two handlers the relay now intentionally uses.

Placeholder scan: This plan contains exact assertion replacement and verification commands.

Type consistency: The callback names match `TeamRelay._on_new_message` and `TeamRelay._on_message_edited` from `src/link_project_to_chat/transport/_telegram_relay.py`.
