# ContextEngine

Turns a business analyst's description of business logic into a validated,
published and scheduled project for the
`azure-databricks-ingestion-framework`.

The analyst describes what they need in plain language. ContextEngine finds
the right Unity Catalog tables, proposes an execution plan with a projected
lineage graph, generates `ingestion_config.yaml` and
`transformation_notebook.py`, validates them with the framework's own CI
checks, then publishes and schedules the job on Databricks. The analyst
approves at four gates and can send a revision at any of them.

Runs entirely on your machine. Nothing leaves it except calls to the LLM
provider you choose and to your own Databricks workspace.

---

# Part 1 - Setup

## Prerequisites

| Need | Minimum | Check it with |
| --- | --- | --- |
| Python | 3.10+ (3.12 tested) | `python --version` |
| Node.js + npm | Node 18+ | `node --version` and `npm --version` |
| Git | any recent | `git --version` |

You also need, before the app can do anything useful:

- A **Databricks personal access token** and the **HTTP path of a SQL
  warehouse** in the same workspace.
- An **OpenAI API key** — required no matter which chat model you pick,
  because the search index is built from OpenAI embeddings.
- Optionally an **Anthropic** and/or **Google** key, if you want to run
  Claude or Gemini models.

## Where everything lives

ContextEngine expects to sit next to the framework it generates code for.
On this machine that is:

```
c:\Users\HIMANSHUU\Desktop\everything in one place\databricks\CursorConextEngine\
│
├── azure-databricks-ingestion-framework\    ← read-only reference. Never modified.
├── uc-lineage-web\                          ← reference POC. Never modified.
└── ContextEngine\                           ← THIS APP. All commands run from here.
    ├── run.py                               ← the server entry point
    ├── settings.yaml                        ← tuning: models per stage, budgets, paths
    ├── models.yaml                          ← model registry and prices
    ├── secrets.example.yaml                 ← template to copy
    ├── secrets.yaml                         ← your keys (you create this; gitignored)
    ├── requirements.txt
    ├── app\                                 ← backend
    ├── scripts\selftest.py                  ← offline end-to-end check
    ├── web\                                 ← frontend (this is where npm runs)
    └── data\                                ← created at runtime; gitignored
```

**Two rules that make the rest of this readable:**

- Every **Python** command runs from `ContextEngine\`.
- Every **npm** command runs from `ContextEngine\web\`.

If you moved `ContextEngine` somewhere that is *not* beside the framework,
fix `paths.framework_repo` in `settings.yaml` before continuing — see
[Step 4](#step-4--confirm-it-can-see-the-framework).

## Step 1 — Open a terminal in the app folder

Open PowerShell and change into `ContextEngine`. The path contains spaces,
so it must be quoted:

```powershell
cd "c:\Users\HIMANSHUU\Desktop\everything in one place\databricks\CursorConextEngine\ContextEngine"
```

Confirm you are in the right place — this should echo all three filenames
back rather than erroring:

```powershell
Get-ChildItem -Name run.py, settings.yaml, requirements.txt
```

Everything in Steps 2 through 8 is run from this folder.

## Step 2 — Create the Python environment and install dependencies

From `ContextEngine\`:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

This takes a minute or two and installs FastAPI, uvicorn, httpx, numpy,
PyYAML, pydantic, pyflakes and sqlite-vec.

Calling `.\.venv\Scripts\python.exe` directly is deliberate: it needs no
activation and is unaffected by PowerShell's script execution policy. If you
would rather activate the environment and just type `python`:

```powershell
.\.venv\Scripts\Activate.ps1
```

If that fails with *"running scripts is disabled on this system"*, either
allow it once for this session with
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, or skip
activation and keep using the full `.\.venv\Scripts\python.exe` form. **The
rest of this README uses the full form**, so it works either way.

Verify the install:

```powershell
.\.venv\Scripts\python.exe -c "import fastapi, httpx, numpy, yaml, pydantic, pyflakes; print('dependencies ok')"
```

On macOS or Linux the equivalent is `python3 -m venv .venv`, then
`./.venv/bin/python -m pip install -r requirements.txt`, and
`./.venv/bin/python` wherever this README says `.\.venv\Scripts\python.exe`.

## Step 3 — Add your credentials

From `ContextEngine\`, copy the template:

```powershell
Copy-Item secrets.example.yaml secrets.yaml
```

Open `ContextEngine\secrets.yaml` and fill in the real values:
 
```yaml
databricks:
  # Just the host, no https:// and no trailing slash.
  server_hostname: adb-1234567890123456.7.azuredatabricks.net
  # SQL Warehouses -> your warehouse -> Connection details -> HTTP path.
  # The warehouse id is the last path segment; ContextEngine reads it from here.
  http_path: /sql/1.0/warehouses/abcdef1234567890
  # Settings -> Developer -> Access tokens -> Generate new token.
  token: dapi...

llm:
  openai_api_key: sk-...        # required: builds the search index
  anthropic_api_key: sk-ant-... # optional: enables Claude models
  google_api_key: AIza...       # optional: enables Gemini models
```

Notes that save time:

- `secrets.yaml` is gitignored and never leaves your machine.
- Leave a key out entirely, or leave the placeholder text untouched, and
  ContextEngine treats it as absent rather than sending a broken key and
  getting a confusing 401. The model picker greys out models whose provider
  has no key.
- Environment variables override the file and always win, so you can skip
  `secrets.yaml` altogether if you prefer:

| Setting | Environment variable | Needed for |
| --- | --- | --- |
| Databricks hostname | `DATABRICKS_SERVER_HOSTNAME` | everything Databricks |
| SQL warehouse HTTP path | `DATABRICKS_HTTP_PATH` | catalog sync, column comments |
| Personal access token | `DATABRICKS_TOKEN` | catalog sync, publish, deploy |
| OpenAI key | `OPENAI_API_KEY` | embeddings, and GPT models |
| Anthropic key | `ANTHROPIC_API_KEY` | Claude models |
| Google key | `GOOGLE_API_KEY` | Gemini models |

A `.env` file in `ContextEngine\` is also read, if you already keep
credentials that way.

## Step 4 — Confirm it can see the framework

The generator reads the framework's pydantic config schema, its example
projects, its CI validators and its `jobs/job_template.json`. It never
writes there. The default in `settings.yaml` assumes the layout shown above:

```yaml
paths:
  framework_repo: ../azure-databricks-ingestion-framework
```

While you are in `settings.yaml`, set the workspace path if yours differs
from the default:

```yaml
databricks:
  workspace_repo_root: /Workspace/Shared/azure-databricks-ingestion-framework
```

Check it resolves, from `ContextEngine\`:

```powershell
.\.venv\Scripts\python.exe -c "from app.context.framework_spec import describe; import json; print(json.dumps(describe(), indent=2))"
```

You want `"available": true` and a non-empty `examples` list — on this
machine that is `olist_customers`, `project_a` … `project_d`. If you get
`"available": false`, the message names the path it tried; fix
`paths.framework_repo` and re-run.

## Step 5 — Build the frontend

This is the one step that runs in `web\`, not in `ContextEngine\`:

```powershell
cd web
npm install
npm run build
cd ..
```

`npm install` pulls about 60 packages (React and Vite only — the diff
viewer, syntax highlighter and lineage layout are all in-repo, so there is
nothing else to fetch). `npm run build` writes `web\dist\`, which the Python
server then serves from the same origin.

You should see something like `dist/assets/index-*.js  185 kB`. You are now
back in `ContextEngine\` for every remaining step.

## Step 6 — Start the server

From `ContextEngine\`:

```powershell
.\.venv\Scripts\python.exe run.py
```

It prints `ContextEngine on http://127.0.0.1:8100`. Leave this terminal
running and open <http://127.0.0.1:8100> in a browser.

To stop it, press `Ctrl+C` in that terminal. To change the port, edit
`server.port` in `settings.yaml`. Add `--reload` to restart automatically on
backend code changes:

```powershell
.\.venv\Scripts\python.exe run.py --reload
```

## Step 7 — Verify the setup

The status dot beside the **ContextEngine** title in the top-left of the UI
summarises everything; hover it for the breakdown. For the same information
in the terminal, open a **second** PowerShell window (leave the server
running in the first) and run:

```powershell
Invoke-RestMethod http://127.0.0.1:8100/api/health | ConvertTo-Json -Depth 5
```

Read the result like this:

| Field | Healthy value | If it is wrong |
| --- | --- | --- |
| `databricks.status` | `ok`, with `identity` showing your user | `not_configured` means no token or hostname; `error` includes the workspace's own message |
| `databricks.warehouse_id` | the id from your HTTP path | `null` means `http_path` is missing or malformed |
| `framework.exists` | `true` | fix `paths.framework_repo` (Step 4) |
| `framework.job_template` | `true` | the framework checkout is incomplete |
| `llm_providers.openai.configured` | `true` | needed for embeddings (Step 3) |
| `ready` | `true` | becomes true once Databricks, the framework and at least one LLM key are all in place |

`snapshot.tables` will be `0` at this point. That is what Step 8 fixes.

You can also check the whole pipeline without spending anything or touching
Databricks at all. This seeds a synthetic five-table catalog, stubs only the
LLM and embedding calls, and runs everything else for real — retrieval,
lineage projection, code generation, all five validators, and a git commit:

```powershell
.\.venv\Scripts\python.exe scripts\selftest.py --publish
```

It ends with `All checks passed.` Drop `--publish` to skip the git part.

---

# Part 2 - Using it

## Step 8 — Take the first catalog snapshot

Nothing can be found until Unity Catalog has been indexed. In the UI, click
**Sync catalog** in the left sidebar.

This reads `information_schema.tables` and `information_schema.columns` for
every catalog your token can see, plus `system.access.table_lineage` for the
lookback window, into `ContextEngine\data\contextengine.db`, then builds the
embedding and full-text indexes over it. Progress streams into the sidebar.

Expect a minute or two for a mid-sized metastore and roughly one cent of
embeddings for a first full index. Re-syncing later is much cheaper: document
bodies are content-hashed, so only tables whose schema or description
actually changed get re-embedded.

The same thing from the command line, run from `ContextEngine\`:

```powershell
.\.venv\Scripts\python.exe -m app.context.sync                # every readable catalog
.\.venv\Scripts\python.exe -m app.context.sync main_catalog   # only named catalogs
.\.venv\Scripts\python.exe -m app.context.sync --no-embed     # snapshot only, no embedding cost
.\.venv\Scripts\python.exe -m app.context.sync --full         # rebuild lineage from the whole window
```

To restrict which catalogs the UI button touches, list them in
`settings.yaml` under `databricks.catalogs`.

When it finishes, the sidebar shows table, column, lineage-edge and embedded
document counts, plus **% of columns described**. Watch that last number: it
is the single best predictor of retrieval quality, and the flywheel in
[The metadata flywheel](#the-metadata-flywheel) is how it improves.

If the sync reports a lineage error but still counts tables and columns, your
token is missing `GRANT SELECT ON system.access.table_lineage`. Search still
works; it just loses the graph signal.

## Step 9 — Build a project

1. **Pick a model** (optional) in the sidebar. The default is per-stage: a
   cheap model for intent extraction and reranking, a strong one for
   planning and code generation. Choosing one here overrides all stages for
   this session. The live cost meter sits directly underneath.

2. **Click "New project"** and describe what you need in the chat, in
   business terms. Both of these work:

   > Monthly net revenue by product for the last 12 months, excluding
   > cancelled orders.

   > Join `sales.raw.orders` to `sales.raw.order_items` on `order_id`, sum
   > `quantity * unit_price` per product per month, drop cancelled orders,
   > and write it to `sales.curated.monthly_product_revenue`.

3. **Gate 1 — confirm the tables.** The right-hand panel lists candidates
   with the reason each was chosen. Tick the ones to use, **Inspect** any of
   them to see full columns and lineage, and search to add one that was
   missed. Nothing downstream is built from anything you did not confirm.
   Click **Continue with these tables**.

4. **Gate 2 — review the plan.** You get the source and sink tables, the
   write mode, the transformation outline, every output column with its
   description, and a projected lineage graph showing where the new table
   lands among the existing ones — proposed nodes and edges drawn dashed.
   Read the **Assumptions** section; that is where anything decided for you
   is disclosed. To change something, type it in the chat and the plan is
   redrawn. Otherwise click **Approve plan and write code**.

5. **Gate 3 — review the code.** Both generated files are shown with syntax
   highlighting, and **Show changes** diffs them against the previous version
   after a revision. The validation summary at the top has already run before
   you see anything: the framework's config schema check, its transform
   contract check, a syntax and undefined-name pass, and a column existence
   check against the catalog snapshot. Failures were fed back to the model
   automatically and the panel says how many repairs it took. Ask for changes
   in the chat, or click **Approve code**.

6. **Gate 4 — publish and deploy.**
   - **Publish** writes both files to a `feat/<project>` branch in the
     working repository at `ContextEngine\data\framework_clone`, and uploads
     them to
     `/Workspace/Shared/azure-databricks-ingestion-framework/projects/<name>/`.
     The two paths are reported separately because they fail for unrelated
     reasons. The framework checkout is never touched.
   - **Compute** defaults to serverless. Classic (the framework's
     `Standard_DS4_v2` job cluster) is an explicit override.
   - **When it runs** is either a Quartz cron (also written into the YAML)
     or **Once** with no schedule. Once mode is for a one-off load; it runs
     immediately unless you uncheck that.
   - **Create / deploy** creates the job, or resets it if a job of that name
     already exists. Then **Run now** and **Refresh status**.
   - After the first successful run, **Apply column comments** writes the
     plan's column descriptions onto the new table in Unity Catalog.

## Where the output lands

| What | Where |
| --- | --- |
| Generated project, committed | `ContextEngine\data\framework_clone\projects\<name>\` |
| Generated project, in Databricks | `/Workspace/Shared/azure-databricks-ingestion-framework/projects/<name>/` |
| Databricks job | named `ingestion_<name>`, linked from the deploy panel |
| Catalog snapshot, indexes, chat history | `ContextEngine\data\contextengine.db` |

To review what was generated, from `ContextEngine\`:

```powershell
git -C data\framework_clone log --oneline -5
git -C data\framework_clone show --stat HEAD
```

Git publishing is **local-commit-only** here, because the framework folder
is a subdirectory of the enclosing repository rather than a repository of its
own, and that repository has no remotes. To enable pushing, give the working
repository a remote once:

```powershell
git -C data\framework_clone remote add origin <your-repo-url>
```

Subsequent publishes push automatically. To rebuild the working repository
from the current state of the framework, delete
`ContextEngine\data\framework_clone` and publish again.

## Command reference

All Python commands assume you are in `ContextEngine\`; the npm commands
assume `ContextEngine\web\`.

| Task | Folder | Command |
| --- | --- | --- |
| Start the app | `ContextEngine\` | `.\.venv\Scripts\python.exe run.py` |
| Start with auto-reload | `ContextEngine\` | `.\.venv\Scripts\python.exe run.py --reload` |
| Sync the catalog | `ContextEngine\` | `.\.venv\Scripts\python.exe -m app.context.sync` |
| Offline end-to-end check | `ContextEngine\` | `.\.venv\Scripts\python.exe scripts\selftest.py --publish` |
| Check setup | `ContextEngine\` | `Invoke-RestMethod http://127.0.0.1:8100/api/health` |
| Re-install Python deps | `ContextEngine\` | `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` |
| Rebuild the frontend | `ContextEngine\web\` | `npm run build` |
| Frontend dev server | `ContextEngine\web\` | `npm run dev` |
| Reset everything | `ContextEngine\` | `Remove-Item -Recurse -Force data` |

Interactive API documentation is at <http://127.0.0.1:8100/docs> while the
server is running.

## Changing the frontend

`npm run build` is only needed for the production path. While editing files
under `web\src\`, use the Vite dev server instead — it hot-reloads and
proxies `/api` to the Python process. That needs **two terminals**:

Terminal 1, from `ContextEngine\`:

```powershell
.\.venv\Scripts\python.exe run.py
```

Terminal 2, from `ContextEngine\web\`:

```powershell
npm run dev
```

Then use <http://localhost:5173> rather than port 8100. When you are done,
run `npm run build` once so the version served on 8100 is up to date too.

## Troubleshooting

**`python` is not recognised** — Python is not on `PATH`. Either reinstall
it with "Add Python to PATH" ticked, or use the full interpreter path for
Step 2 only: `& "C:\Program Files\Python312\python.exe" -m venv .venv`.
After that, `.\.venv\Scripts\python.exe` works regardless.

**`Activate.ps1 cannot be loaded because running scripts is disabled`** —
skip activation. Use `.\.venv\Scripts\python.exe` as this README does, or
run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first.

**The page says "Frontend not built"** — `npm run build` has not run, or it
ran somewhere other than `ContextEngine\web\`. Confirm `web\dist\index.html`
exists, then restart the server.

**`databricks.status` is `error`** — the message is the workspace's own. A
401 means the token is wrong or expired. A 403 on
`information_schema` means the token lacks `USE CATALOG` / `USE SCHEMA`.
Hostname must have no `https://` prefix and no trailing slash.

**Sync finishes but finds nothing** — the token can see no catalogs, or all
of them are in `databricks.skip_catalogs` in `settings.yaml` (`system`,
`samples` and `__databricks_internal` are skipped by default).

**"No embeddings indexed yet. Run a catalog sync first."** — Step 8 has not
run, or it ran with `--no-embed`, or the OpenAI key is missing.

**A model is greyed out in the picker** — no API key for that provider.
Add it to `secrets.yaml` and restart the server; keys are read at startup.

**"Session budget reached"** — `budget.max_usd_per_session` in
`settings.yaml` has been hit. Raise it, or start a new project.

**Code review shows failing checks** — the model could not satisfy the
validators within `codegen.max_repair_attempts`. Each failure names the
check and the reason; the fastest fix is usually to say what to do
differently in the chat, which regenerates and revalidates.

**Publish says "local commit only"** — expected. See
[Where the output lands](#where-the-output-lands) for how to add a remote.

**Start over from scratch** — delete `ContextEngine\data\` to drop the
snapshot, indexes, chat history and working repository. Your `secrets.yaml`
and `settings.yaml` are untouched, and nothing in the framework or in
Databricks is affected.

---

# Part 3 - Configuration

`ContextEngine\settings.yaml` holds everything tunable. The parts worth
knowing:

- **`stages`** assigns a model per pipeline stage. This is the largest cost
  lever: cheap models handle intent extraction and reranking, strong models
  handle planning and code generation. The model picker in the UI overrides
  all of them for one session.
- **`retrieval`** controls the hybrid search — how many vector and lexical
  candidates to fuse, how far to walk the lineage graph, and how heavily
  graph proximity counts against textual similarity.
- **`budget`** caps spend and tokens per session. The check runs before each
  call, so a session can overshoot by at most one call.
- **`paths.framework_repo`** is read-only. Generated projects are committed
  into `paths.framework_clone` instead.
- **`databricks.catalogs`** limits what a sync indexes; empty means every
  catalog the token can see, minus `databricks.skip_catalogs`.
- **`databricks.cluster_policy_id`** is applied to generated jobs. Leave it
  blank to let the workspace default apply.

`ContextEngine\models.yaml` is the model registry: id, provider, context
window, prices per million tokens, and capability flags. Adding a model is a
config edit, not a code change — the UI picker and the cost meter both read
this file. Prices drift, so update them there rather than anywhere in the
code.

Both files are re-read when the server starts, so restart after editing.

---

# Part 4 - How it works

For diagrams of the whole system (components, catalog sync, retrieval, the
four gates, and how generated code reaches a Databricks job), see
[ARCHITECTURE.md](ARCHITECTURE.md).

## Three context sources, served three different ways

**The framework contract** is small, static and unforgiving, so it is
compiled once into a spec card rather than retrieved.
`app/context/framework_spec.py` renders `IngestionConfig.model_json_schema()`
plus the conditional validator functions verbatim from the framework's own
source, and attaches the two most structurally similar example projects.
It is sent as a cacheable system block, so providers with prompt caching
bill it at the cached rate after the first call of a session. Retrieval over
the repository would be strictly worse here: it can silently omit the one
required field that makes a config valid.

**Unity Catalog metadata** is too large for a prompt, so this is the part
that genuinely needs retrieval. `app/context/sync.py` snapshots it locally
and `app/context/retrieval.py` searches it.

**The lineage graph** is snapshotted as an edge table and traversed
in-process. Querying the warehouse per hop would be slow and expensive; in
SQLite a two-hop walk is microseconds, cheap enough to run on every query.

## Retrieval

Four signals, fused:

1. Vector similarity over table documents and column documents
2. BM25 over the same documents via SQLite FTS5
3. Lineage proximity to whatever the first two agree on
4. A single LLM rerank over compact candidate cards

Reciprocal rank fusion merges the first two, then tables reachable from the
strongest matches are boosted by graph distance, with an extra boost for
tables reachable from more than one of them. That is what resolves the
problem this platform exists for: a dozen tables have a `revenue` column,
and the right one is the one sitting in the same part of the graph as the
other tables the request is about.

The rerank sees compact cards rather than full schemas, and runs once over
the union of all query phrasings, so recall is cheap.

## Validation before review

`app/agent/validators.py` writes candidates to a staging directory shaped
like `projects/<name>/` and runs five checks:

1. `ci/validate_yaml_configs.py` — the framework's own pydantic validation,
   including `project_name` matching the folder
2. `ci/validate_transformation_contract.py` — the framework's own AST check
   of the `transform(spark, raw_df)` signature
3. `ast.parse`, plus an import-time side-effect check, because the notebook
   is inlined into the driver session with `%run`
4. `pyflakes`, where an undefined name is an error and the rest is a warning
5. A column existence check: every column the transform references must
   exist on a table it reads, or be one it created

Failures loop back to the model with the validator's own words, up to
`codegen.max_repair_attempts` times. The analyst only ever reviews code that
has already passed all five, which is what makes code review about business
logic rather than syntax.

## Publish and deploy

Publishing does two independent things, reported separately because they
fail for unrelated reasons:

- **Git** — a `feat/<project>` branch and commit in the working repository
  at `data/framework_clone`. If the framework directory is a git repository
  root it is cloned; otherwise a standalone repository is seeded from a copy
  of it. Push happens only if an `origin` remote exists. In this workspace
  the framework is a subdirectory of a larger repository, so this is a local
  commit until you add a remote.
- **Workspace** — upload to
  `/Workspace/Shared/azure-databricks-ingestion-framework/projects/<name>/`,
  which is what makes the project runnable immediately.

Deploy still uses the framework job's name, notebook path and parameters from
`jobs/job_template.json`, then finds the job by name and creates or resets it.

Compute defaults to **Jobs serverless** (`databricks.job_compute` in
`settings.yaml`). The template's classic cluster (Spark 14.3,
`Standard_DS4_v2`, 1–4 workers) is not used unless you pick **Classic job
cluster** in the UI. Serverless tasks have no `job_cluster_key`; Databricks
sizes the run. `STANDARD` vs `PERFORMANCE_OPTIMIZED` is
`databricks.serverless_performance_target`.

When it runs:

- **On a schedule** — Quartz cron on the job, also written into
  `ingestion_config.yaml`, so the committed config and the running job cannot
  drift apart.
- **Once** — no cron. The job is created unscheduled. Check **Run it as soon
  as the job is created** (on by default in this mode) or use **Run now**
  later. A one-off CSV load belongs here.

Switching modes later is safe: `jobs/reset` overwrites the previous settings
instead of merging with them.

## The metadata flywheel

The plan generates a description for every output column. After the first
successful run, **Apply column comments** writes them into Unity Catalog
with `ALTER TABLE ... ALTER COLUMN ... COMMENT`. The next sync picks them
up, so every project built here makes retrieval better for the next one.
Column comment coverage is shown in the sidebar for exactly this reason — it
is the single number that predicts retrieval quality.

## Code layout

```
run.py                      uvicorn entry point
settings.yaml               per-stage models, retrieval tuning, budgets, paths
models.yaml                 model registry and pricing
app/
  config.py                 settings + secrets resolution
  main.py                   FastAPI application
  api/                      health, catalog, chat, publish, deploy
  context/
    databricks_client.py    Statement Execution API, Workspace, Jobs
    store.py                SQLite: snapshot, indexes, sessions, artifacts
    sync.py                 catalog and lineage snapshot
    documents.py            what gets embedded
    embeddings.py           embedding generation and the vector index
    retrieval.py            hybrid search and rerank
    lineage_graph.py        traversal and projected lineage
    framework_spec.py       the compiled spec card
  agent/
    state_machine.py        six stages, four gates
    artifacts.py            output schemas
    validators.py           the five checks and the repair loop
    prompts/                one markdown template per stage
  llm/
    base.py                 provider-neutral types
    anthropic.py openai.py google.py
    registry.py cost.py client.py
  delivery/
    publisher.py            git branch and workspace upload
    deployer.py             job create/reset, runs, column comments
web/                        React + Vite
data/                       database and framework working clone (gitignored)
scripts/selftest.py         end-to-end check with no external services
```

## Known limits

- `system.access.table_lineage` needs `GRANT SELECT` and only covers its
  lookback window, so a brand-new table has no lineage and retrieval falls
  back to embeddings and keyword matching alone.
- Retrieval quality tracks column comment coverage closely. If comments are
  sparse, the flywheel above matters more — worth measuring right after the
  first sync.
- Git publishing is local-commit-only until the working repository has a
  remote.
- Generated code is validated statically, never executed. A first run on
  Databricks is still the real test, which is why **Run now** is offered
  right after deploy.
