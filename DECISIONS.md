# Decisions

1. **Indexes.** Startup creates a unique index on employee `emp_code`, a unique compound index on attendance
   (`emp_code`, `date`), an employee `department` index, an attendance `emp_code` index, and a compound attendance
   (`date` descending, `emp_code` ascending) index for the list endpoint's filter/sort. I did not add a standalone
   `status` index: the current list query can also filter by date and employee, and each extra index adds write cost.
   Query plans should be checked with the explain endpoint before changing that choice.
2. **Punch-in race.** Requests may both pass the preliminary duplicate lookup, but only one insert can satisfy the
   unique (`emp_code`, `date`) index. The losing insert raises a database error and the handler returns 409.
3. **Ties.** The leaderboard uses competition ranks. If employees tie at the `limit` rank, every employee tied at that
   rank is returned; the next rank is skipped. Rows are secondarily ordered by `emp_code`.
4. **Headcount.** Department summary first selects employees whose `joined_on` is on or before the month's last day,
   then associates any matching logs. Since employees are counted before logs are processed, employees with no logs
   still contribute to headcount.
5. **At 100x the data.** I would move analytics and trend calculations into indexed MongoDB aggregation pipelines and
   validate their explain plans. The current Python-side collection reads and loops are not suitable for that scale.
