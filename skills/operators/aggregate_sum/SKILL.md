---
name: aggregate_sum
description: Sum one numeric column from context.data and store to context.last_total.
---

# aggregate_sum

Inputs:
- `field`: numeric field

Output:
- `sum`: aggregated numeric result

Runtime contract:
- Read rows from `ctx["data"]`.
- Write result into `ctx["last_total"]`.
