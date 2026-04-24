# Windows Config M11 Collection Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `tests/test_config_m11.py` collect cleanly on Windows while preserving the root-user skip behavior on Unix.

**Architecture:** This is a test-portability fix only. The permission test remains Unix-only, and the root-user check must be guarded so importing the test module never calls a missing `os.getuid` attribute.

**Tech Stack:** Python 3.11+, pytest, stdlib `os` and `sys`.

---

## File Structure

- Modify `tests/test_config_m11.py`: make the root-user skip expression portable.

### Task 1: Guard The Root Skip Expression

**Files:**
- Modify: `tests/test_config_m11.py:54-55`

- [ ] **Step 1: Run the failing collection command**

Run:

```bash
python -m pytest tests/test_config_m11.py --collect-only -q
```

Expected on Windows before the fix:

```text
AttributeError: module 'os' has no attribute 'getuid'
```

- [ ] **Step 2: Write the minimal test-file change**

Replace the two decorators above `test_load_config_unreadable_file_raises` with this exact code:

```python
@pytest.mark.skipif(sys.platform == "win32", reason="UNIX permissions only")
@pytest.mark.skipif(
    hasattr(os, "getuid") and os.getuid() == 0,
    reason="root bypasses permission checks",
)
def test_load_config_unreadable_file_raises(tmp_path: Path):
```

This preserves the existing behavior on Unix and prevents import-time failure on Windows.

- [ ] **Step 3: Verify collection now succeeds**

Run:

```bash
python -m pytest tests/test_config_m11.py --collect-only -q
```

Expected on Windows after the fix:

```text
5 tests collected
```

- [ ] **Step 4: Run the focused config I/O tests**

Run:

```bash
python -m pytest tests/test_config_m11.py -q
```

Expected on Windows:

```text
4 passed, 1 skipped
```

Expected on Unix as non-root:

```text
5 passed
```

Expected on Unix as root:

```text
4 passed, 1 skipped
```

- [ ] **Step 5: Commit**

Run:

```bash
git add tests/test_config_m11.py
git commit -m "test: guard unix getuid skip"
```

## Self-Review

Spec coverage: Finding 1 is covered by guarding the `os.getuid()` call and verifying test collection.

Placeholder scan: This plan contains concrete code, commands, and expected outcomes.

Type consistency: The decorators use existing imports, `os`, `sys`, `pytest`, and `Path`; no new helper is introduced.
