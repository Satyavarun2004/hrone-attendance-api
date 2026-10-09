# DECISIONS.md

1. **Indexes:**
We created a unique index on `employees.emp_code` to guarantee code uniqueness. On `attendance_logs`, we created a compound unique index on `(emp_code, date)` to prevent race conditions and enforce one record per day. We added supporting indexes on `(date, emp_code)` for sorted pagination and `(date, status)` for analytics queries. We considered an index on `history` but rejected it because history is append-only audit data and never queried independently.

2. **Punch-in race:**
When two identical requests arrive simultaneously, both compute the IST attendance date and attempt an insert. The database-level unique compound index on `(emp_code, date)` guarantees only one insert succeeds (201 Created). The second triggers a `DuplicateKeyError`, which our code catches and translates into an HTTP 409 Conflict without relying on application-level checks.

3. **Ties:**
The leaderboard uses standard competition ranking (1, 2, 2, 4). When employees tie on late minutes at the cutoff boundary, all tied employees share the rank. Because `limit` filters by `rank <= limit`, our endpoint returns every tied employee, which may return more than `limit` rows as required by the contract.

4. **Headcount:**
Department summary groups and counts the `employees` collection first (`joined_on <= month_end`) to determine total department headcount. This guarantees employees with zero attendance records are counted before attendance logs are joined.

5. **One thing to change for 100x data:**
At 100x data (~10 million records), we would maintain pre-aggregated monthly summary rollups updated asynchronously via background jobs instead of aggregating on the fly, and shard `attendance_logs` by `emp_code`.