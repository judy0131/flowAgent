---
name: filter_rows
description: Filter context.data rows with op in {eq, contains, gt, lt}.
---

# filter_rows

Inputs:
- `field`: target field name
- `op`: `eq` | `contains` | `gt` | `lt`
- `value`: filter value

Output:
- `remain_rows`: remaining row count

Runtime contract:
- Read rows from `ctx["data"]`.
- Write filtered rows back to `ctx["data"]`.
