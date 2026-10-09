# Generic Lakebase Databricks Asset Bundle (DAB)

A parameterized Databricks Asset Bundle for deploying **Lakebase** (Autoscaling
Postgres) as infrastructure-as-code. One edit-and-deploy workflow covers
catalogs, synced tables, change data feed, high availability, disaster
recovery, and compute sizing across `dev` / `stage` / `prod`.

This bundle is written to be edited by hand **or** by the Databricks Assistant
(Genie): every knob is a bundle variable, and every repeatable thing (synced
tables, roles) is a self-contained block marked "copy this block."

## What it deploys

| Resource | File | Purpose |
|---|---|---|
| `postgres_projects` | `resources/lakebase.yml` | The Lakebase project |
| `postgres_branches` | `resources/lakebase.yml` | Production branch (adopted), DR protection |
| `postgres_endpoints` | `resources/lakebase.yml` | Primary RW endpoint (adopted), autoscale + HA group |
| `postgres_catalogs` | `resources/lakebase.yml` | Unity Catalog registration over the Postgres database |
| `postgres_roles` | `resources/roles.yml` | Identity-aware roles (USER / SERVICE_PRINCIPAL / GROUP) |
| `postgres_synced_tables` | `resources/synced_tables.yml` | Delta to Lakebase sync: SNAPSHOT + CONTINUOUS (CDC) |
| `jobs` (CDF enabler) | `resources/cdf_job.yml` + `src/enable_lakebase_cdf.py` | Turns on Lakebase CDF (Lakebase to Delta) |

### Two directions of change data, do not confuse them

| | Synced tables | Lakebase CDF |
|---|---|---|
| Direction | **Delta to Lakebase** | **Lakebase to Delta** |
| Purpose | Publish lakehouse data to Postgres for low-latency serving | Capture Postgres inserts/updates/deletes into Delta history tables |
| How | Declarative (`postgres_synced_tables`) | Job + API (no declarative resource exists) |
| CDC role | `CONTINUOUS` policy reads the **source Delta** table's CDF | Writes `lb_<table>_history` Delta tables in Unity Catalog |

## Prerequisites

- **Databricks CLI >= 1.0.0** (`postgres_catalogs` and `postgres_synced_tables`
  need 1.0.0+; projects need 0.287.0+). Check with `databricks -v`.
- Authentication to your workspace: `databricks auth login --host <url> --profile <name>`.
- `CAN MANAGE` on Lakebase projects.
- **`CREATE CATALOG` on the metastore** (metastore-admin privilege) for the
  `postgres_catalogs` registration, and therefore for synced tables. Without it,
  that step fails with `PERMISSION_DENIED: User does not have CREATE CATALOG`.
  If you lack it, have an admin run the catalog step or grant you the privilege.
- For **Lakebase CDF** only: a workspace admin must enable the "Lakebase Change
  Data Feed" preview, and the running identity needs `USE CATALOG` / `USE SCHEMA`
  / `CREATE TABLE` on the destination catalog and schema.

## Configure it (step by step)

All configuration is in two places:
- **`databricks.yml`** holds the scalar knobs (host, sizing, HA, DR, names) as
  variables, plus the per-environment `targets`.
- **`resources/*.yml`** holds the collections (which synced tables, which roles)
  as copy-paste blocks.

### Step 1: Point it at your workspace

In `databricks.yml`, each target has a `workspace.host` with a `CHANGE-ME`
placeholder. Replace it with your workspace URL:

```yaml
targets:
  dev:
    workspace:
      host: https://your-workspace.cloud.databricks.com   # <-- edit this
```

Alternatively, leave the placeholder and pass a CLI profile at deploy time:
`databricks bundle deploy -t dev -p your-profile`.

### Step 2: Set the main knobs

Also in `databricks.yml`, under `variables:`. Edit the value after `default:`:

```yaml
variables:
  project_name:        { default: lakebase-demo }        # Lakebase project id
  catalog_name:        { default: lakebase_demo_catalog } # Unity Catalog name
  database_name:       { default: databricks_postgres }   # Postgres database
  endpoint_min_cu:     { default: 0.5 }   # sizing: autoscale floor (min 0.5)
  endpoint_max_cu:     { default: 1 }     # sizing: autoscale ceiling
  node_count:          { default: 1 }     # HA: 1 = single, 2+ = highly available
  enable_readable_secondaries: { default: false }  # HA: read replicas
  history_retention:   { default: "604800s" }  # DR: PITR window (7 days)
  branch_protected:    { default: false }       # DR: block deletion/reset
```

(See the full variable reference table below for every knob.)

### Step 3: Tune each environment (targets)

The `targets:` block overrides those variables for `dev` / `stage` / `prod`.
This is how one bundle gives you a small dev and a hardened prod. Edit the
numbers to change sizing or the HA/DR posture per environment:

```yaml
  prod:
    workspace:
      host: https://your-prod-workspace.cloud.databricks.com
    variables:
      endpoint_min_cu: 1
      endpoint_max_cu: 4
      node_count: 2
      enable_readable_secondaries: true
      branch_protected: true
      history_retention: "1209600s"   # 14 days
```

### Step 4: Pick a catalog mode (new vs. existing)

Synced tables land in a Unity Catalog catalog registered over the Postgres
database. Choose one mode:

- **Register a new catalog (default):** keep the `postgres_catalogs` block in
  `resources/lakebase.yml` and set `catalog_name`. Requires `CREATE CATALOG` on
  the metastore.
- **Use an existing catalog:** if a Lakebase catalog is already registered over
  this project's database, **delete the `postgres_catalogs` block** in
  `resources/lakebase.yml` and set `catalog_name` (and `catalog_schema` if not
  `public`) to that catalog. No `CREATE CATALOG` privilege needed. The bundle
  cannot adopt an existing catalog (`postgres_catalogs` has no `replace_existing`),
  so you reference it rather than declare it.

Either way, `catalog_name` / `catalog_schema` drive the synced-table destination.

### Step 5: Configure the serving tables (Delta to Lakebase)

Edit `resources/synced_tables.yml`. Each block is one Delta table synced into
Postgres. **To add one, copy a block** and set these fields:

```yaml
    orders:                                                            # unique key
      synced_table_id: ${var.catalog_name}.${var.catalog_schema}.orders # destination name
      source_table_full_name: main.retail.orders                       # your 3-part Delta source
      primary_key_columns:
        - order_id
      scheduling_policy: CONTINUOUS   # SNAPSHOT | TRIGGERED | CONTINUOUS
```

Use `CONTINUOUS` for low-latency CDC. That requires change data feed on the
source Delta table:

```sql
ALTER TABLE main.retail.orders SET TBLPROPERTIES (delta.enableChangeDataFeed = true);
```

### Step 6: Configure who can connect (roles)

Edit `resources/roles.yml`. **Copy a block** per identity:

```yaml
    analyst:
      role_id: analyst                           # unique id (lowercase, hyphens)
      postgres_role: "someone@yourcompany.com"   # user email, SP app id, or group name
      identity_type: USER                        # USER | SERVICE_PRINCIPAL | GROUP
      auth_method: LAKEBASE_OAUTH_V1
```

### Step 7: Configure change-capture out of Postgres (optional)

The Lakebase CDF variables in `databricks.yml` control the capture job
(Postgres changes into Delta):

```yaml
  source_schema:    { default: public }                  # which PG schema to capture
  cdf_dest_catalog: { default: lakebase_demo_catalog }   # where Delta history lands
  cdf_dest_schema:  { default: lakebase_cdf }
```

## Deploy

```bash
cd lakebase-dab
databricks bundle validate -t dev     # catch config errors
databricks bundle deploy   -t dev     # create everything (or -t stage / -t prod)
databricks bundle run enable_cdf_job -t dev   # optional: turn on Lakebase CDF
```

Tear down with `databricks bundle destroy -t dev`. By default the project is
soft-deleted and retained for 7 days.

## Variable reference

All in `databricks.yml`. Targets override them; defaults are a small dev instance.

| Variable | Meaning | Capability |
|---|---|---|
| `project_name`, `project_display_name`, `pg_version` | Project identity, Postgres major version | - |
| `endpoint_min_cu`, `endpoint_max_cu` | Autoscale floor/ceiling in Compute Units (min 0.5; max-min <= 16; max <= 64) | **Size** |
| `node_count` | Compute group size; 1 = single, >= 2 = HA (min and max are set equal) | **HA** |
| `enable_readable_secondaries` | Read-only connections on the RW endpoint | **HA** |
| `history_retention` | PITR window as a seconds string (172800s to 3024000s) | **DR** |
| `branch_protected` | Protect the production branch from deletion/reset | **DR** |
| `catalog_name`, `database_name` | UC catalog + Postgres database | **Catalog** |
| `pipeline_storage_catalog`, `pipeline_storage_schema` | Standard UC location for synced-table pipeline checkpoints | **Synced tables** |
| `source_schema`, `cdf_dest_catalog`, `cdf_dest_schema` | Lakebase CDF source schema and Delta destination | **Lakebase CDF** |

### Target escalation (the defaults shipped in this bundle)

| Knob | dev | stage | prod |
|---|---|---|---|
| min / max CU | 0.5 / 1 | 0.5 / 2 | 1 / 4 |
| `node_count` | 1 | 1 | 2 |
| readable secondaries | off | off | on |
| branch protected | off | off | on |
| retention | 7 days | 7 days | 14 days |

## Lakebase CDF: job vs. CLI

The `enable_cdf_job` runs `src/enable_lakebase_cdf.py`, which prints the
`REPLICA IDENTITY FULL` SQL to run in the Lakebase SQL Editor and then creates
the CDF configuration via the SDK/REST. The CDF API is **Beta**; if the job
call fails, run the proven CLI equivalent from your laptop:

```bash
databricks postgres create-cdf-config \
  "projects/<project_id>/branches/production/databases/<database_name>" \
  "<cdf_dest_catalog>" "<cdf_dest_schema>" "<source_schema>" \
  --cdf-config-id "<source_schema>" -p <profile>

# Check status:
databricks postgres list-cdf-statuses \
  "projects/<project_id>/branches/production/databases/<database_name>" -p <profile>
```

CDF is scoped to a schema: every current and future table in `source_schema` is
captured. Destination tables appear as `cdf_dest_catalog.cdf_dest_schema.lb_<table>_history`.

## Notes and limitations

- The project auto-creates the `production` branch and `primary` endpoint;
  this bundle adopts both with `replace_existing: true`.
- **Roles map one Postgres role per identity.** The project owner already has an
  auto-created role on the branch, so do not add a `postgres_roles` entry that
  points at the owner's identity (it fails with "role with that name already
  exists"). Add roles for *other* users, service principals, or groups.
- Registering the catalog needs `CREATE CATALOG` on the metastore (see
  Prerequisites). Core infra (project, branch, endpoint, roles, CDF job) does not.
- `workspace.host` cannot use variables, so it is set literally per target.
- Lakebase DAB support is Beta; pin your CLI version and test in dev first.
- Autoscaling scale-to-zero is governed by endpoint suspension (on by default);
  `endpoint_min_cu` is the warm floor when active.

## Validation status

Validated end-to-end on a live FE-VM Lakebase workspace (Databricks CLI
v1.17.0), with a clean `bundle deploy` then `bundle destroy`:

- **Core infra:** project, production branch (adopted), primary endpoint
  (reached `ACTIVE`), and the CDF job created and destroyed cleanly.
- **Synced tables (Delta to Lakebase):** a SNAPSHOT table and a CONTINUOUS table
  deployed via existing-catalog mode; the Lakebase Postgres tables matched the
  source Delta tables row-for-row.
- **Catalog:** register mode (`postgres_catalogs`) needs `CREATE CATALOG` on the
  metastore. On a Default-Storage metastore, use existing-catalog mode (above),
  which needs only `CREATE TABLE` on the target schema. Both paths confirmed.
- **Lakebase CDF:** requires the workspace "Lakebase Change Data Feed" preview to
  be enabled by an admin. Until then the CDF API returns `No API found`; the
  bundle job and the `create-cdf-config` CLI call are otherwise correct.
