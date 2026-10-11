# ContextEngine architecture

How the whole project works: where the framework lives, what the LLM sees,
how the catalog is synced and searched, what is in SQLite, and how generated
code travels from a chat message to a running Databricks job.

Diagrams are Mermaid. They render in Cursor's markdown preview and on GitHub.

## Short answers

**Where is the framework stored or cached?** It is not in the database.

- **Reference copy:** read live from disk at `paths.framework_repo`
  (`../azure-databricks-ingestion-framework`). Read-only; ContextEngine never
  writes there.
- **Memory cache:** `framework_spec.py` caches the imported config module, the
  example projects and the static spec card in memory with `lru_cache`. This
  lasts until the server restarts.
- **Working clone:** `ContextEngine/data/framework_clone` is a separate git
  repo that generated projects are committed into. It is seeded from a copy of
  the framework because that folder is a subdirectory of the outer repo, not a
  repo root.
- **Databricks workspace copy:**
  `/Workspace/Shared/azure-databricks-ingestion-framework` is where jobs
  actually run it from. Publish uploads generated projects there.

**Is the whole repo sent to the LLM?** No. It sends a **spec card**, built once
from the framework:

- the hard rules, written as text
- the config schema, from `IngestionConfig.model_json_schema()`
- the conditional validator functions, copied verbatim
- two example projects, picked by closeness of source type, sink type and
  write mode

The card is about 3-4k tokens, marked cacheable, and sent only to the plan,
codegen and repair stages. The framework's `src/` code (readers, writers,
runner) is never sent. Later stages read frozen approved artifacts instead of
replaying the whole chat, so cost stays flat.

**How is the catalog synced?** `app/context/sync.py` queries the SQL warehouse
for `information_schema.tables` and `.columns`, plus
`system.access.table_lineage`. Everything lands in one SQLite file.

- **Tables and columns:** replaced per catalog on every sync, so dropped tables
  disappear.
- **Lineage:** incremental, using a `lineage_synced_through` watermark, over a
  90-day first window.
- **Embeddings:** incremental. Only documents whose content hash changed are
  re-embedded.

After a sync, search, lineage and the lineage preview run locally. The
warehouse is not touched again.

## 1. System overview

```mermaid
flowchart LR
  BA([Business analyst]) --> UI[React UI<br/>chat, gates, diff, deploy]
  UI -- REST + SSE --> API[FastAPI<br/>chat / catalog / publish / deploy]

  API --> SM[State machine<br/>6 stages, 4 gates]
  SM --> RET[Retrieval<br/>vector + FTS5 + lineage + rerank]
  SM --> SPEC[Framework spec card]
  SM --> VAL[Validators + repair loop]
  SM --> LLM[LLM layer<br/>Anthropic / OpenAI / Google]

  RET <--> DB[(SQLite<br/>contextengine.db)]
  SM <--> DB
  VAL --> DB

  SPEC -- reads --> FW[/azure-databricks-ingestion-framework<br/>read-only reference/]
  VAL -- runs CI scripts from --> FW

  SYNC[sync.py] -- SQL: information_schema,<br/>system.access.table_lineage --> UC[(Unity Catalog)]
  SYNC --> DB
  SYNC -- embeddings --> OAI[OpenAI embeddings]

  API --> PUB[Publisher]
  PUB --> CLONE[/data/framework_clone<br/>git branch feat/project/]
  PUB -- workspace/import --> WS[/Databricks workspace<br/>projects/name/]
  API --> DEP[Deployer]
  DEP -- jobs/create or reset, run-now --> JOB[Databricks job<br/>ingestion_name]
  JOB --> DRV[driver_notebook]
  DRV --> WS
  DEP -- ALTER COLUMN COMMENT --> UC
```

## 2. The sync flow and what lands in SQLite

```mermaid
flowchart TD
  A[Start sync: UI button or<br/>python -m app.context.sync] --> B[Resolve catalogs<br/>skip system, samples]
  B --> C[Per catalog: information_schema.tables]
  C --> D[replace_catalog_tables<br/>DELETE + INSERT, cascade orphans]
  D --> E[information_schema.columns]
  E --> F[replace_catalog_columns]
  F --> G[system.access.table_lineage<br/>aggregated in the warehouse<br/>since watermark]
  G --> H[upsert_lineage_edges<br/>+ prune edges to unknown tables]
  H --> I[build_documents:<br/>one doc per table, one per column]
  I --> J[upsert docs + docs_fts]
  J --> K{body_hash changed<br/>or model/dim changed?}
  K -- yes --> L[OpenAI embeddings, 768-dim,<br/>L2-normalised, batches of 128]
  K -- no --> M[skip, zero cost]
  L --> N[embeddings table: float32 BLOB]
  N --> O[VectorIndex.invalidate]
  M --> O
  O --> P[set last_sync_at]
```

One file, `data/contextengine.db`, holds everything:

| Table | Purpose |
|---|---|
| `tables_meta`, `columns_meta` | Snapshot of tables and columns: type, owner, comments, ordinal |
| `lineage_edges` | `source_id`, `target_id`, entity type and id, occurrence count, last event |
| `docs` | Text of each table doc and column doc, with a content hash |
| `docs_fts` | FTS5 index over the same text, for BM25 keyword search |
| `embeddings` | One vector (float32 BLOB) per doc, keyed to `docs` |
| `vec_docs` | Optional sqlite-vec virtual table, rebuilt from the embeddings in memory. Falls back to a numpy scan if the extension isn't available |
| `sync_state` | `lineage_synced_through` and `last_sync_at` |
| `sessions` | One row per chat. `state_json` holds intent, candidates, selected tables, plan, code, publish and deploy results |
| `messages` | Chat transcript per session |
| `artifacts` | Versioned intent, candidates, plan, code and deploy payloads, each with an `approved` flag |
| `usage_log` | Tokens and cost per LLM call; drives the cost meter and the session budget |
| `deployments` | Project name, job id and URL, last run id, status |

Only metadata is stored. No row data from your tables ever enters SQLite or an
LLM prompt.

## 3. Retrieval (how tables are found)

```mermaid
flowchart TD
  Q[Analyst request] --> I[Intent LLM call<br/>gpt-4.1-mini: goal, metrics,<br/>search_queries, mentioned_tables]
  I --> QS[Up to 6 query phrasings]
  QS --> E[Embed each query]
  E --> V[Vector top-40<br/>table docs + column docs]
  QS --> L[FTS5 BM25 top-40]
  V --> F[RRF fusion k=60<br/>column hits weighted 0.7]
  L --> F
  F --> G[Lineage prior<br/>seeds = top text matches<br/>BFS 2 hops<br/>boost 0.35 / distance<br/>+ 0.25 co-occurrence]
  G --> S[Shortlist 30 -> compact cards]
  S --> R[One rerank LLM call<br/>sees cards, not full schemas]
  R --> T[Top 8 tables + reason]
```

Vector search alone misses exact identifiers an analyst copies from a
spreadsheet; keyword search alone misses paraphrases. Lineage settles the case
where a dozen tables all have a `revenue` column: the right one is usually
next to the other tables in the request.

## 4. The six stages and four gates

```mermaid
sequenceDiagram
  actor BA
  participant UI
  participant SM as State machine
  participant DB as SQLite
  participant LLM
  participant V as Validators

  BA->>UI: plain-English request
  UI->>SM: message
  SM->>LLM: intent (no framework spec)
  SM->>DB: retrieve candidates
  SM-->>UI: candidates
  Note over BA,UI: GATE 1 confirm tables
  BA->>SM: approve_tables
  SM->>DB: full columns + lineage for the confirmed tables
  SM->>LLM: plan (spec card + table context)
  SM-->>UI: plan + projected lineage
  Note over BA,UI: GATE 2 approve plan
  BA->>SM: approve_plan
  SM->>LLM: codegen (spec card + plan + tables)
  loop up to 2 repairs
    SM->>V: validate_project
    V-->>SM: errors, or ok
    SM->>LLM: repair (spec + failures)
  end
  SM-->>UI: config_yaml + notebook_py + validation
  Note over BA,UI: GATE 3 approve code
  BA->>SM: approve_code
  Note over BA,UI: GATE 4 publish + deploy
```

Stage progression: `intake -> table_review -> plan_review -> code_review ->
publish_ready -> deployed`. Each stage is one prompt with a fixed JSON output
schema. Models are set per stage in `settings.yaml`: `gpt-4.1-mini` for intent
and rerank, `claude-sonnet-4-5` for plan, codegen and repair.

**What the LLM sees at each stage:**

| Stage | Sent to the LLM |
|---|---|
| Intent | Analyst request only |
| Rerank | Compact table cards (about 14 columns each) |
| Plan | Spec card, full columns for confirmed tables, 1-hop lineage neighbours, existing project names |
| Codegen | Spec card, approved plan, table context |
| Repair | Spec card, plan, current code, validator output |

## 5. How the code travels

```mermaid
flowchart TD
  A[LLM returns JSON<br/>config_yaml + notebook_py] --> B[validate_project]
  B --> B1[syntax, YAML shape,<br/>contract AST check, pyflakes]
  B --> B2[column existence vs<br/>SQLite snapshot]
  B --> B3[framework CI scripts run<br/>in a temp projects/ dir]
  B1 & B2 & B3 --> C{errors?}
  C -- yes, attempts left --> D[repair call] --> B
  C -- no, or out of attempts --> E[(artifacts + session state<br/>in SQLite)]
  E --> F[UI code review + diff]
  F --> G[Approve code]
  G --> H[Publish]
  H --> H1[git: feat/project branch<br/>in data/framework_clone]
  H --> H2[workspace/import to<br/>projects/name/<br/>transformation_notebook + ingestion_config.yaml]
  H1 & H2 --> I[Deploy]
  I --> J[fill jobs/job_template.json<br/>serverless by default,<br/>cron or run-once]
  J --> K[jobs/create or reset<br/>ingestion_name]
  K --> L[run-now]
  L --> M[driver_notebook]
  M --> N[load YAML -> read source -> raw_df]
  N --> O["%run transformation_notebook<br/>transform(spark, raw_df)"]
  O --> P[write to sink + audit row]
  P --> Q[Apply column comments<br/>ALTER TABLE ... COMMENT]
  Q -. next sync .-> R[(New table is searchable<br/>by meaning)]
```

The last step feeds back into the first. Column descriptions written to Unity
Catalog show up in the next sync, so the next analyst can find this table by
meaning.

## 6. Things worth knowing

- **Reference vs clone vs workspace.** All three hold the framework, but only
  the workspace copy affects what runs.
- **Sync cost.** Tables and columns are a full replace per catalog on every
  sync. Only embeddings are incremental. That is cheap now but grows with
  catalog size.
- **Why validation is strict.** Gate 3 runs the framework's own CI scripts, so
  what you review has already passed the same checks a human PR would.
- **Notebook naming.** The driver does `%run ../projects/<name>/transformation_notebook`.
  The publisher must upload the notebook without the `.py` extension or the
  driver cannot find it. The git copy keeps `transformation_notebook.py`.
- **Compute and scheduling.** Jobs default to serverless. Deploy can attach a
  Quartz cron or create an unscheduled job for a one-off run.
