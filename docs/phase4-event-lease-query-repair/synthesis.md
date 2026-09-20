# Phase 4 repair checklist — event / lease / query

## P1 — Preserve one lease domain after rotated-session alias eviction
- **Target:** `gateway/turn_lease.py`: `SessionTurnLeaseRegistry.rebind()` and `_evict_idle()`.
- **Repair:** Treat all IDs aliased by `rebind()` as one eviction unit. Never evict only one idle alias and later create a second `_SessionLease` for its sibling ID; remove the whole alias set together, or canonicalize every alias to one stable registry key.
- **Acceptance:** With `max_entries=1`, acquire `parent`, rebind it to `child`, release, force eviction pressure, then concurrently acquire both IDs. They must contend on the same lock; no two holders may coexist. The registry must remain bounded and all permits/tokens must release cleanly.

## P2 — Do not leave the rotation-collision path fail-open
- **Target:** `gateway/turn_lease.py`: `SessionTurnLeaseRegistry.rebind()` collision branch (currently logs and returns `False`); `gateway/run.py`: `_rebind_turn_lease()` callers and their compression-rotation error handling.
- **Repair:** Define a safe outcome when the destination ID already has a held or pending lease. Do not continue a turn whose flush moved to an unprotected destination: either merge serialization domains without deadlock, or reject/defer the rotation and keep the durable/session-entry target aligned with the protected ID. Surface an actionable retry/diagnostic rather than only a warning.
- **Acceptance:** Reproduce a source holder plus a live/pending destination holder, trigger rotation, and prove that no two turns can load/flush the destination concurrently. Cover holder, queued-waiter handoff, timeout, cancellation, and release of both original tokens.

## P3 — Make read-pool degradation observable and preserve query progress
- **Target:** `hermes_state.py`: `_PathReadBudget.acquire()`, `_reclaim_idle_read_conn_anywhere()`, `SessionDB._checkout_read_conn()` / `_read_ctx()`; tests in `tests/test_session_db_read_conn_pool.py` and `tests/test_session_db_read_path_split.py`.
- **Repair:** Keep the current fail-safe fallback to the locked writer connection, but expose a bounded, queryable metric/log for each refusal cause (file budget, process budget, FD-headroom, read-open backoff). Ensure an idle reclaim failure or concurrent permit race cannot strand permits or make the fallback silently permanent after capacity/headroom returns.
- **Acceptance:** Under simultaneous multi-file bursts, assert the file and process ceilings, correct fallback result, and one classified refusal signal per exercised cause. After releasing/reclaiming connections and clearing simulated FD pressure/open failure, assert a subsequent WAL query again uses a pooled read connection and does not take the writer lock.

## Completion gate
Run the focused lease and read-path suites, including the new adversarial cases, then the relevant gateway/state regression suites. Treat any timeout, leaked permit, concurrent same-session holder, or query forced permanently onto the writer lock as a failure.
