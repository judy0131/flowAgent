---
name: load_csv
description: Load a CSV file and store rows in context.data.
---

# load_csv

Inputs:
- `path`: local CSV path

Output:
- `rows`: loaded row count

Runtime contract:
- Write loaded records to `ctx["data"]`.
- Keep previous trace in `ctx["trace"]`.
