The project you generated failed automated validation before it reached the reviewer. The failures
below come from the framework's own CI checks, a Python syntax and name analysis, and a
cross-check of every column you referenced against the live Unity Catalog snapshot.

Fix all of them and return both complete files again.

Rules for the fix:

- Change only what the failures require. Do not restructure working logic, rename things
  gratuitously, or drop functionality the plan called for.
- A missing-column failure names the catalog table that was checked. Columns of a file / CSV /
  `raw_df` source are not in that snapshot. If the error says `seller_*` does not exist on
  `customers`, keep those columns on the file DataFrame and qualify them
  (`sellers_df["seller_id"]`) rather than deleting them or renaming them to `customer_*`.
- A warning about "No snapshot columns for abfss://..." or "Assuming it comes from raw_df" is
  not a failure. Do not "fix" it by removing the file columns.
- A schema validation failure names the exact field and rule. Read it literally.
- If a failure looks wrong, it is not: these checks run the framework's real validators against
  your output. The exception is file-source columns, which the snapshot cannot see.
