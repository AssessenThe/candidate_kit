# Review of the starter `app/main.py`

This review compares the original starter with the current implementation. The “How addressed” column describes the
current code; items marked **Still outstanding** are known gaps and should not be read as completed fixes.

| # | Where (function / line) | What was wrong in the starter | How you'd notice it (test, input, or symptom) | How addressed in the current code |
|---|---|---|---|---|
| 1 | `health` | Returned `{"status": "ok"}` without checking MongoDB, so it could report ready while the database was unavailable. | Stop MongoDB and request `/health`; the starter still returned 200. | Sends a MongoDB ping and returns 503 if it fails. |
| 2 | `EmployeeIn`, `create_employee` | Accepted values without the contract's formats and constraints; did not reject equal shift start/end times. | Submit a malformed employee code, email, date, or equal shift times. | Adds Pydantic constraints and date/shift checks, though string length constraints are not all implemented. |
| 3 | `create_employee`, startup | A check-then-insert duplicate check was vulnerable to concurrent requests; no unique database index enforced `emp_code`. | Send concurrent creates for one code; more than one could be inserted. | Creates a unique `emp_code` index and maps insertion failures to 409. |
| 4 | `create_employee` | Stored `created_at` as a naive local datetime and returned a datetime representation rather than epoch milliseconds. | Inspect the stored timezone or response field type. | Stores UTC and converts datetimes in API responses to epoch milliseconds. |
| 5 | `list_employees` | Used `page * page_size` for the skip (making page 1 skip the first page), counted all employees instead of filtered results, and did not sort. | Request page 1 or filter by department and inspect `total`. | Uses `(page - 1) * page_size`, counts the filtered query, sorts by `emp_code`, and validates pagination. |
| 6 | `punch_in` | Did not return 404 for an unknown employee before reading its shift; accepted invalid statuses and weakly validated timestamps. | Punch in with an unknown employee or an invalid status/timestamp. | Adds an employee 404, presence-status validation, and epoch-millisecond range/type checks. |
| 7 | `punch_in`, date helpers | Converted timestamps using the machine's local timezone, did not truncate to whole seconds, and assigned dates incorrectly for overnight shifts. | Punch near IST midnight or within an overnight shift; compare stored date and returned instant. | Converts to UTC/IST, truncates supplied instants to seconds, and accounts for overnight date assignment. |
| 8 | `punch_in` | Duplicate detection was check-then-insert only, so simultaneous punches could create duplicate records. | Send simultaneous punches for the same employee and date. | Adds a unique (`emp_code`, `date`) index; duplicate insert failures return 409. |
| 9 | `compute_late_minutes` | Used naive local datetimes and did not model IST/overnight shifts reliably. | Test a punch around shift start, especially after midnight for an overnight shift. | Has IST-aware helper logic, but **overnight late calculation remains incorrect** because the helper is not given the employee's actual shift end. |
| 10 | `compute_work_hours` | Used Python's `round`, which uses ties-to-even rather than the contract's half-up rounding. | Test a duration whose hours round exactly at a half-cent boundary. | Uses `Decimal` with `ROUND_HALF_UP`. |
| 11 | `compute_overtime` | Did not apply the 30-minute minimum and did not correctly move an overnight shift end to the next day. | Test 29 versus 30 overtime minutes, and a punch-out after an overnight shift. | Applies the 30-minute threshold, but **overnight overtime remains incorrect** because the helper does not receive `shift_start` and always places the end on the record date. |
| 12 | `list_attendance` | Loaded every matching attendance document into Python, sorted in memory, and had no database pagination; it also omitted the `emp_code` sort tie-breaker. | Use a large collection or multiple employees with records on the same date. | Applies sorting and pagination in MongoDB with a date/employee index; total is filtered. |
| 13 | `list_attendance`, response serialization | Returned an extra `id` field and datetime values in the wrong API format. | Compare the response with the `AttendanceRecord` schema. | Omits Mongo `_id` and converts datetimes to epoch milliseconds. |
| 14 | Endpoint coverage | The starter did not implement punch-out, regularization, monthly/department analytics, leaderboard, department trend, or explain endpoints. | Call any of those documented paths; they returned 404. | Adds route handlers for these paths. Some handlers still have contract/performance gaps described below. |
| 15 | Startup / indexes | Created no indexes, so uniqueness was unenforced and documented query patterns were not indexed. | Inspect indexes or test duplicates and query plans. | Creates unique and query indexes at startup. |

## Known gaps remaining in the current implementation

- `compute_late_minutes` and `compute_overtime` do not correctly handle overnight shifts, as noted above.
- Analytics and trend endpoints read collections into Python and calculate results there rather than using the MongoDB
  aggregations required by the contract. This will not scale to the stated dataset size.
- The trend moving average keeps the last seven **non-null rates**, rather than averaging non-null rates among the
  current day and the six preceding calendar rows. Null-rate days therefore shift the window incorrectly.
- Regularization reads and replaces the history array without an atomic compare-and-set. Concurrent corrections can
  overwrite one another's audit entries.
- A repeated punch-out after the record is already closed can return 404 because the initial lookup only searches open
  records; the contract calls for 409 when that record has already been punched out.
- The explain handlers do not consistently explain the exact query/aggregation used by their corresponding endpoint;
  their plans and index use need verification against MongoDB.

The items above were identified by comparing the starter and current source with `openapi.yaml` and `DATA_MODEL.md`.
They should be tested against the full contract before claiming complete compliance.
