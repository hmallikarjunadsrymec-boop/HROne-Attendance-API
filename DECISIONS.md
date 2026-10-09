# DECISIONS.md

1. **Indexes.**
   - `employees`: `{ emp_code: 1 }` (unique, guarantees `emp_code` uniqueness across concurrent creations), `{ department: 1, joined_on: 1 }` (powers department filtering and R9 headcount checks without collection scans).
   - `attendance_logs`: `{ emp_code: 1, date: 1 }` (unique, guarantees one record per employee per attendance date), `{ date: -1, emp_code: 1 }` (powers default list attendance sorting), `{ status: 1, date: 1 }` and `{ date: 1, late_minutes: 1 }` (accelerate analytics pipelines).
   - **Considered & Rejected**: Indexing `history` array fields. History is append-only audit data and never queried directly in filters.

2. **Punch-in race.**
   When two identical punch-in requests arrive simultaneously for the same employee and date, both compute the IST attendance date. Both attempt an `insert_one` into `attendance_logs`. MongoDB's primary node locks the unique compound index `{ emp_code: 1, date: 1 }`. The first request acquires the index key and succeeds with `201 Created`. The second request triggers a `DuplicateKeyError` from the unique index, which our exception handler intercepts to immediately return `409 Conflict`. No check-then-insert race window exists.

3. **Ties.**
   The late leaderboard uses MongoDB's `$setWindowFields` stage with `$rank`. Standard competition ranking assigns identical ranks to tied values (e.g. 1, 2, 2, 4). Filtering by `{ rank: { $lte: limit } }` returns all tied employees at rank 2 even if `limit=2`, resulting in more rows than `limit`. Tied rows are ordered by `emp_code` ascending. This strictly obeys standard competition ranking rules.

4. **Headcount.**
   The department summary pipeline initiates from the `employees` collection (matching `joined_on <= month_end`), performing a `$lookup` to join matching `attendance_logs` for that month. Unwinding with `preserveNullAndEmptyArrays: true` ensures employees with zero attendance logs are retained as documents with `logs: null`. Grouping by department counts unique employee codes via `headcount: { $size: "$emp_codes" }`, correctly including zero-log employees in headcount.

5. **100x Data Scale Change.**
   For 100x data scale (~10M logs), I would pre-aggregate daily and monthly analytics into dedicated read-optimized summary collections via background workers or MongoDB Change Streams. Additionally, `attendance_logs` would be sharded on `{ department: 1, emp_code: 1, date: 1 }` to distribute write load and isolate department trend/summary queries to single shards.
