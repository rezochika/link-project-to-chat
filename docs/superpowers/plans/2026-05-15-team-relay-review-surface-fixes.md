# Team Relay Review Surface Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent the 2026-05-15 team-chat failure mode where repeated manager review handoffs looked stale, triggered the same-author relay pause, and left the bots disagreeing about whether review was against the working tree, `HEAD`, or `origin`.

**Architecture:** Keep the existing Telegram relay as the single bot-to-bot choke point. Tighten its state machine so a visible peer response breaks the same-author streak, add short-lived duplicate-forward suppression for repeated handoffs, and update the bundled team personas so manager/developer handoffs always name the review surface explicitly.

**Tech Stack:** Python 3.11+, asyncio, Telethon relay test doubles, pytest, bundled Markdown personas.

---

## File Structure

- Modify `src/link_project_to_chat/transport/_telegram_relay.py`: relay state-machine changes.
- Modify `tests/test_team_relay.py`: focused regression tests for same-author streak reset and duplicate suppression.
- Modify `src/link_project_to_chat/personas/software_manager.md`: manager review protocol for working tree vs commit vs remote.
- Modify `src/link_project_to_chat/personas/software_dev.md`: developer handoff protocol for unstaged/staged/committed work.
- Modify `tests/test_bundled_personas.py`: persona invariant tests.
- Modify `docs/TODO.md`: record this hardening plan as a designed follow-up once implementation lands.

### Task 1: Break Same-Author Streak On Peer Response

**Files:**
- Modify: `src/link_project_to_chat/transport/_telegram_relay.py`
- Test: `tests/test_team_relay.py`

- [ ] **Step 1: Write the failing regression test**

Append this test near the same-author streak tests in `tests/test_team_relay.py`:

```python
@pytest.mark.asyncio
async def test_relay_peer_response_clears_same_author_streak():
    """A peer bot response breaks the same-author streak even if that response
    is not itself forwarded to the original sender.

    The 2026-05-15 team chat paused after repeated manager forwards even though
    the developer had replied. The relay's halt reason says "with no peer reply
    between"; the state machine must match that promise.
    """
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids(start_id=12_000)
    relay = TeamRelay(
        client,
        "acme",
        -100_111,
        {"acme_mgr_bot", "acme_dev_bot"},
        max_consecutive_bot_relays=3,
    )

    for i in range(2):
        ev = await _mk_event(
            f"@acme_dev_bot review request {i}",
            sender_username="acme_mgr_bot",
            sender_is_bot=True,
            msg_id=12_100 + i,
        )
        await _dispatch(relay, ev)

    assert relay._rounds == 2
    assert relay._halted is False

    peer_reply = await _mk_event(
        "Patched both items; re-review please.",
        sender_username="acme_dev_bot",
        sender_is_bot=True,
        msg_id=12_200,
    )
    await _dispatch(relay, peer_reply)

    assert relay._rounds == 0
    assert relay._halted is False

    ev = await _mk_event(
        "@acme_dev_bot please confirm the committed diff",
        sender_username="acme_mgr_bot",
        sender_is_bot=True,
        msg_id=12_300,
    )
    await _dispatch(relay, ev)

    assert relay._rounds == 1
    assert relay._halted is False
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
pytest tests/test_team_relay.py::test_relay_peer_response_clears_same_author_streak -q
```

Expected before implementation:

```text
FAILED tests/test_team_relay.py::test_relay_peer_response_clears_same_author_streak
E       assert 2 == 0
```

- [ ] **Step 3: Change `_delete_pending_for_peer` to return whether the peer responded**

In `src/link_project_to_chat/transport/_telegram_relay.py`, replace `_delete_pending_for_peer` with:

```python
    async def _delete_pending_for_peer(self, sender_username: str) -> bool:
        """Delete relay forwards waiting for `sender_username`; return True if any existed."""
        to_delete = [
            mid for mid, peer in self._pending_deletes.items()
            if peer == sender_username
        ]
        for mid in to_delete:
            self._pending_deletes.pop(mid, None)
            timer = self._pending_delete_timers.pop(mid, None)
            if timer is not None and not timer.done():
                timer.cancel()
            await self._delete_relay_message(mid)
        return bool(to_delete)
```

- [ ] **Step 4: Clear relay rounds when a pending peer response arrives**

In `_handle_event`, replace:

```python
        if not is_edit:
            await self._delete_pending_for_peer(sender_username)
```

with:

```python
        if not is_edit:
            peer_responded = await self._delete_pending_for_peer(sender_username)
            if peer_responded and not self._halted:
                self._round_times.clear()
                self._round_senders.clear()
```

- [ ] **Step 5: Run the focused test**

Run:

```bash
pytest tests/test_team_relay.py::test_relay_peer_response_clears_same_author_streak -q
```

Expected:

```text
1 passed
```

- [ ] **Step 6: Commit Task 1**

Run:

```bash
git add src/link_project_to_chat/transport/_telegram_relay.py tests/test_team_relay.py
git commit -m "fix: reset relay streak on peer response"
```

### Task 2: Suppress Short-Lived Duplicate Bot Handoffs

**Files:**
- Modify: `src/link_project_to_chat/transport/_telegram_relay.py`
- Test: `tests/test_team_relay.py`

- [ ] **Step 1: Write the failing duplicate-forward test**

Append this test near the relay forwarding tests in `tests/test_team_relay.py`:

```python
@pytest.mark.asyncio
async def test_relay_suppresses_recent_duplicate_forward():
    """Distinct Telegram messages with the same sender, peer, and body should
    not create duplicate peer tasks inside a short time window.
    """
    from link_project_to_chat.transport._telegram_relay import TeamRelay

    client = _mk_client_with_ids(start_id=13_000)
    relay = TeamRelay(client, "acme", -100_111, {"acme_mgr_bot", "acme_dev_bot"})

    text = "@acme_dev_bot\n\nRequest changes: README quick start is broken."
    first = await _mk_event(
        text,
        sender_username="acme_mgr_bot",
        sender_is_bot=True,
        msg_id=13_100,
    )
    second = await _mk_event(
        "  @acme_dev_bot\n\nRequest changes:   README quick start is broken.  ",
        sender_username="acme_mgr_bot",
        sender_is_bot=True,
        msg_id=13_101,
    )

    await _dispatch(relay, first)
    await _dispatch(relay, second)

    forwards = [
        call for call in client.send_message.await_args_list
        if call.args[1].startswith("@acme_dev_bot")
    ]
    assert len(forwards) == 1
    assert relay._rounds == 1
    assert 13_100 in relay._relayed_ids
    assert 13_101 in relay._relayed_ids
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
pytest tests/test_team_relay.py::test_relay_suppresses_recent_duplicate_forward -q
```

Expected before implementation:

```text
FAILED tests/test_team_relay.py::test_relay_suppresses_recent_duplicate_forward
E       assert 2 == 1
```

- [ ] **Step 3: Add duplicate-forward constants and helper**

In `src/link_project_to_chat/transport/_telegram_relay.py`, below `_COALESCE_WINDOW_SECONDS`, add:

```python
_RECENT_FORWARD_TTL_SECONDS = 300.0
_MAX_RECENT_FORWARD_SIGNATURES = 100
```

Below `_normalize_mention_spacing`, add:

```python
def _forward_signature(sender_username: str, peer: str, text: str) -> tuple[str, str, str]:
    """Normalize a bot-to-bot handoff enough to detect accidental repeats."""
    body = _body_without_mention(text, peer).lower()
    body = re.sub(r"\s+", " ", body).strip()
    return sender_username, peer, body[:2000]
```

- [ ] **Step 4: Store recent signatures on the relay**

In `TeamRelay.__init__`, after `_coalesce_pending`, add:

```python
        # Recent normalized forwards. Prevents repeated review/dispatch messages
        # from spawning duplicate peer tasks while still allowing a later retry
        # after the TTL expires.
        self._recent_forward_signatures: dict[tuple[str, str, str], float] = {}
```

- [ ] **Step 5: Add the duplicate decision method**

Add this method below `_is_same_author_streak`:

```python
    def _is_recent_duplicate_forward(
        self,
        sender_username: str,
        peer: str,
        text: str,
    ) -> bool:
        now = time.monotonic()
        cutoff = now - _RECENT_FORWARD_TTL_SECONDS
        stale = [
            sig for sig, seen_at in self._recent_forward_signatures.items()
            if seen_at < cutoff
        ]
        for sig in stale:
            self._recent_forward_signatures.pop(sig, None)

        signature = _forward_signature(sender_username, peer, text)
        if signature in self._recent_forward_signatures:
            self._recent_forward_signatures[signature] = now
            return True

        if len(self._recent_forward_signatures) >= _MAX_RECENT_FORWARD_SIGNATURES:
            oldest = min(
                self._recent_forward_signatures,
                key=self._recent_forward_signatures.get,
            )
            self._recent_forward_signatures.pop(oldest, None)
        self._recent_forward_signatures[signature] = now
        return False
```

- [ ] **Step 6: Drop duplicates before sending**

In `_finalize_relay`, after the ack-only block and before `sent_id = await self._relay(...)`, add:

```python
        if peer is not None and self._is_recent_duplicate_forward(sender_username, peer, text):
            logger.info(
                "TeamRelay: dropping duplicate bot message from @%s to @%s (team=%s)",
                sender_username, peer, self._team_name,
            )
            if msg_id is not None:
                self._relayed_ids.add(msg_id)
            return
```

- [ ] **Step 7: Run the focused duplicate test**

Run:

```bash
pytest tests/test_team_relay.py::test_relay_suppresses_recent_duplicate_forward -q
```

Expected:

```text
1 passed
```

- [ ] **Step 8: Run relay tests**

Run:

```bash
pytest tests/test_team_relay.py -q
```

Expected:

```text
passed
```

- [ ] **Step 9: Commit Task 2**

Run:

```bash
git add src/link_project_to_chat/transport/_telegram_relay.py tests/test_team_relay.py
git commit -m "fix: suppress duplicate team relay forwards"
```

### Task 3: Add Review-Surface Discipline To Team Personas

**Files:**
- Modify: `src/link_project_to_chat/personas/software_manager.md`
- Modify: `src/link_project_to_chat/personas/software_dev.md`
- Test: `tests/test_bundled_personas.py`

- [ ] **Step 1: Add failing persona invariant tests**

Append these tests to `tests/test_bundled_personas.py`:

```python
def test_software_manager_persona_requires_review_surface():
    p = files("link_project_to_chat.personas").joinpath("software_manager.md")
    content = p.read_text().lower()
    assert "review surface" in content
    assert "working tree" in content
    assert "head" in content
    assert "origin" in content
    assert "commit" in content


def test_software_dev_persona_reports_review_surface_state():
    p = files("link_project_to_chat.personas").joinpath("software_dev.md")
    content = p.read_text().lower()
    assert "review surface" in content
    assert "unstaged" in content
    assert "staged" in content
    assert "committed" in content
    assert "ask before committing" in content
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
pytest tests/test_bundled_personas.py::test_software_manager_persona_requires_review_surface tests/test_bundled_personas.py::test_software_dev_persona_reports_review_surface_state -q
```

Expected before persona edits:

```text
FAILED tests/test_bundled_personas.py::test_software_manager_persona_requires_review_surface
FAILED tests/test_bundled_personas.py::test_software_dev_persona_reports_review_surface_state
```

- [ ] **Step 3: Update manager persona**

In `src/link_project_to_chat/personas/software_manager.md`, replace:

```markdown
Review protocol: before approving any change, read the actual files the Developer modified. Do not rely solely on their summary.
```

with:

```markdown
Review protocol: before approving any change, read the actual files the Developer modified. Do not rely solely on their summary. Always name the review surface you are checking: working tree (`git diff`), staged changes (`git diff --staged`), a local commit (`git show HEAD` or `git show <hash>`), or remote comparison (`git diff origin/<branch>..HEAD`). If the Developer says fixes are unstaged and your review still sees stale content, stop repeating findings and ask them to either keep you on the working-tree diff or create a local review commit; then review that exact surface.
```

- [ ] **Step 4: Update developer persona**

In `src/link_project_to_chat/personas/software_dev.md`, after the `Execution:` paragraph, add:

```markdown
Review surface: when handing work to the Manager, state whether the changes are unstaged, staged, committed locally, or pushed. Include the exact commit hash when committed, or say "working tree only" when not. If the Manager appears to review stale content, report `git status --short --branch`, name the review surface they should use, and ask before committing solely to stabilize review.
```

- [ ] **Step 5: Run persona tests**

Run:

```bash
pytest tests/test_bundled_personas.py -q
```

Expected:

```text
passed
```

- [ ] **Step 6: Commit Task 3**

Run:

```bash
git add src/link_project_to_chat/personas/software_manager.md src/link_project_to_chat/personas/software_dev.md tests/test_bundled_personas.py
git commit -m "docs: clarify team review surfaces"
```

### Task 4: Record The Follow-Up In TODO Status

**Files:**
- Modify: `docs/TODO.md`

- [ ] **Step 1: Add a concise follow-up row**

In `docs/TODO.md`, add this row under the team/transport follow-up area:

```markdown
| TR-2026-05-15 | Team relay review-surface hardening | `docs/superpowers/plans/2026-05-15-team-relay-review-surface-fixes.md` | ✅ closed — peer replies reset same-author relay streaks, duplicate bot handoffs are suppressed, and bundled team personas require explicit review surfaces. |
```

If there is no current team/transport follow-up table on the branch, add a short subsection under `## 1. Transport Abstraction Track`:

```markdown
### 1.4 Team relay hardening follow-ups

| ID | Item | Plan | Status |
|---|---|---|---|
| TR-2026-05-15 | Team relay review-surface hardening | [plan](superpowers/plans/2026-05-15-team-relay-review-surface-fixes.md) | ✅ closed — peer replies reset same-author relay streaks, duplicate bot handoffs are suppressed, and bundled team personas require explicit review surfaces. |
```

- [ ] **Step 2: Run docs whitespace check**

Run:

```bash
git diff --check
```

Expected:

```text
```

No output.

- [ ] **Step 3: Commit Task 4**

Run:

```bash
git add docs/TODO.md
git commit -m "docs: record team relay hardening follow-up"
```

### Task 5: Final Verification

**Files:**
- Verify only.

- [ ] **Step 1: Run focused regression suites**

Run:

```bash
pytest tests/test_team_relay.py tests/test_bundled_personas.py -q
```

Expected:

```text
passed
```

- [ ] **Step 2: Run team wiring smoke tests**

Run:

```bash
pytest tests/test_bot_team_wiring.py tests/test_group_halt_integration.py -q
```

Expected:

```text
passed
```

- [ ] **Step 3: Run full suite**

Run:

```bash
pytest -q
```

Expected:

```text
passed
```

- [ ] **Step 4: Run whitespace check**

Run:

```bash
git diff --check
```

Expected:

```text
```

No output.

- [ ] **Step 5: Summarize the implementation**

Post a short completion summary containing:

```text
Implemented team relay review-surface hardening.

Commits:
- <task 1 hash> fix: reset relay streak on peer response
- <task 2 hash> fix: suppress duplicate team relay forwards
- <task 3 hash> docs: clarify team review surfaces
- <task 4 hash> docs: record team relay hardening follow-up

Verification:
- pytest tests/test_team_relay.py tests/test_bundled_personas.py -q
- pytest tests/test_bot_team_wiring.py tests/test_group_halt_integration.py -q
- pytest -q
- git diff --check
```

## Self-Review

Spec coverage: This plan covers the observed 2026-05-15 failure from three angles: the relay no longer treats a peer response as "no peer reply"; exact duplicate handoffs no longer spawn duplicate peer tasks; and the bundled personas force explicit review-surface language so manager and developer do not talk past each other about working tree vs `HEAD` vs `origin`.

Placeholder scan: No task contains TBD/TODO/fill-later language. Code changes and tests include concrete snippets and exact commands.

Type consistency: New relay helpers use existing `sender_username`, `peer`, `_round_times`, `_round_senders`, `_pending_deletes`, and `_relayed_ids` names from `TeamRelay`. Persona tests use the existing `importlib.resources.files` pattern from `tests/test_bundled_personas.py`.
