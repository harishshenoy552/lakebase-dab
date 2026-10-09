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
- For **Lakebase CDF** only: a workspace admin must enable the "Lakebase Change
  Data Feed" preview, and the running identity needs `USE CATALOG` / `USE SCHEMA`
  / `CREATE TABLE` on the destination catalog and schema.

## Quick start

```bash
# 1. Set the workspace host in each target in databricks.yml (the CHANGE-ME lines),
#    or skip that and pass a CLI profile with -p <profile>.

# 2. Validate.
databricks bundle validate -t dev

# 3. Deploy.
databricks bundle deploy -t dev        # or: -t stage / -t prod

# 4. (Optional) Enable Lakebase CDF.
databricks bundle run enable_cdf_job -t dev
```

Tear down with `databricks bundle destroy -t dev`. By default the project is
soft-deleted and retained for 7 days.

## The knobs (variables)

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

### Target escalation

| Knob | dev | stage | prod |
|---|---|---|---|
| min / max CU | 0.5 / 1 | 0.5 / 2 | 1 / 4 |
| `node_count` | 1 | 1 | 2 |
| readable secondaries | off | off | on |
| branch protected | off | off | on |
| retention | 7 days | 7 days | 14 days |

## How to customize

**Add a synced table:** copy a block in `resources/synced_tables.yml`, give it a
unique key, and set `source_table_full_name`, `primary_key_columns`, and
`scheduling_policy`. Use `CONTINUOUS` for low-latency CDC; the source Delta
table must have CDF on:

```sql
ALTER TABLE <catalog>.<schema>.<table> SET TBLPROPERTIES (delta.enableChangeDataFeed = true);
```

**Add a role:** copy a block in `resources/roles.yml`, set a unique `role_id`,
`postgres_role` (user email / SP application id / group name), `identity_type`,
and `auth_method`.

**Change size / HA / DR:** edit the variables per target in `databricks.yml`.

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
- `workspace.host` cannot use variables, so it is set literally per target.
- Lakebase DAB support is Beta; pin your CLI version and test in dev first.
- Autoscaling scale-to-zero is governed by endpoint suspension (on by default);
  `endpoint_min_cu` is the warm floor when active.
