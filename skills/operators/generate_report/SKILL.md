---
name: generate_report
description: Create a report from current context for downstream consumption.
---

# generate_report

Inputs:
- `title`: report title

Output:
- report object

Runtime contract:
- Read `ctx["data"]`, `ctx["last_total"]`, `ctx["trace"]`.
- Write final report to `ctx["report"]`.
