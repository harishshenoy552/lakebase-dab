# Databricks notebook source
# MAGIC %md
# MAGIC # Enable Lakebase Change Data Feed (CDF)
# MAGIC
# MAGIC Turns on Lakebase CDF for a Postgres schema so every insert/update/delete is
# MAGIC captured into open-format Delta `lb_<table>_history` tables in Unity Catalog.
# MAGIC **Direction: Lakebase (Postgres) -> Delta.** This is the opposite of synced tables.
# MAGIC
# MAGIC Lakebase CDF has no declarative bundle resource, so this notebook configures it
# MAGIC programmatically. It is a **Beta** API; if a call fails, use the CLI fallback shown
# MAGIC in the repo README (`databricks postgres create-cdf-config ...`).
# MAGIC
# MAGIC ### Prerequisites
# MAGIC - A workspace admin enabled the **Lakebase Change Data Feed** preview.
# MAGIC - This identity has `USE CATALOG` / `USE SCHEMA` / `CREATE TABLE` on the destination,
# MAGIC   and `CAN MANAGE` on the Lakebase project.
# MAGIC - Source tables have `REPLICA IDENTITY FULL` (see the SQL cell below).

# COMMAND ----------

dbutils.widgets.text("project_id", "lakebase-demo-dev")
dbutils.widgets.text("branch_id", "production")
dbutils.widgets.text("database_name", "databricks_postgres")
dbutils.widgets.text("source_schema", "public")
dbutils.widgets.text("dest_catalog", "lakebase_demo_catalog")
dbutils.widgets.text("dest_schema", "lakebase_cdf")

project_id    = dbutils.widgets.get("project_id")
branch_id     = dbutils.widgets.get("branch_id")
database_name = dbutils.widgets.get("database_name")
source_schema = dbutils.widgets.get("source_schema")
dest_catalog  = dbutils.widgets.get("dest_catalog")
dest_schema   = dbutils.widgets.get("dest_schema")

parent = f"projects/{project_id}/branches/{branch_id}/databases/{database_name}"
print("CDF parent:        ", parent)
print("Source PG schema:  ", source_schema)
print("Destination UC:    ", f"{dest_catalog}.{dest_schema}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Set REPLICA IDENTITY FULL on the source tables
# MAGIC
# MAGIC CDF needs the full before/after row image in the WAL. Run the SQL below in the
# MAGIC **Lakebase SQL Editor** against your source database. It sets replica identity on
# MAGIC every existing table in the schema and installs a trigger so future tables inherit it.
# MAGIC
# MAGIC (Printed here rather than executed, because it must run inside Postgres. Copy/paste it.)

# COMMAND ----------

replica_identity_sql = f"""
-- Existing tables in schema "{source_schema}":
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT table_schema, table_name
    FROM information_schema.tables
    WHERE table_schema = '{source_schema}' AND table_type = 'BASE TABLE'
  LOOP
    EXECUTE format('ALTER TABLE %I.%I REPLICA IDENTITY FULL;', r.table_schema, r.table_name);
  END LOOP;
END $$;

-- Future tables inherit REPLICA IDENTITY FULL automatically:
CREATE OR REPLACE FUNCTION {source_schema}.set_full_replica_identity()
RETURNS event_trigger LANGUAGE plpgsql AS $$
DECLARE obj record;
BEGIN
  FOR obj IN
    SELECT * FROM pg_event_trigger_ddl_commands() WHERE command_tag = 'CREATE TABLE'
  LOOP
    EXECUTE format('ALTER TABLE %s REPLICA IDENTITY FULL;', obj.object_identity);
  END LOOP;
END $$;

DROP EVENT TRIGGER IF EXISTS set_full_replica_identity_on_create;
CREATE EVENT TRIGGER set_full_replica_identity_on_create
  ON ddl_command_end WHEN TAG IN ('CREATE TABLE')
  EXECUTE FUNCTION {source_schema}.set_full_replica_identity();
"""
print(replica_identity_sql)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Create the CDF configuration (Lakebase -> Delta)
# MAGIC
# MAGIC Scoped to the schema: every current and future table in `source_schema` is captured.
# MAGIC The config is immutable once created.

# COMMAND ----------

from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
cdf_config_id = source_schema  # defaults to the Postgres schema name

body = {
    "catalog": dest_catalog,
    "schema": dest_schema,
    "postgres_schema": source_schema,
    "cdf_config_id": cdf_config_id,
}


def create_cdf_config():
    # Preferred: typed SDK method (name may vary by SDK version).
    pg = getattr(w, "postgres", None)
    if pg is not None and hasattr(pg, "create_cdf_config"):
        return pg.create_cdf_config(
            parent=parent,
            catalog=dest_catalog,
            schema=dest_schema,
            postgres_schema=source_schema,
            cdf_config_id=cdf_config_id,
        )
    # Fallback: raw REST call (stable across SDK versions).
    return w.api_client.do("POST", f"/api/2.0/postgres/{parent}/cdf-configs", body=body)


try:
    result = create_cdf_config()
    print("CDF configuration created:")
    print(result)
except Exception as e:
    print("Could not create the CDF config via SDK/REST.")
    print("Error:", repr(e))
    print()
    print("Run this from your laptop with the Databricks CLI instead:")
    print(
        f'  databricks postgres create-cdf-config "{parent}" '
        f'"{dest_catalog}" "{dest_schema}" "{source_schema}" '
        f'--cdf-config-id "{cdf_config_id}"'
    )
    raise

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Check status
# MAGIC Destination tables appear as `dest_catalog.dest_schema.lb_<table>_history`.
# MAGIC A table is only synced once it has at least one row.

# COMMAND ----------

try:
    pg = getattr(w, "postgres", None)
    if pg is not None and hasattr(pg, "list_cdf_statuses"):
        for s in pg.list_cdf_statuses(parent=parent):
            print(s)
    else:
        print(w.api_client.do("GET", f"/api/2.0/postgres/{parent}/cdf-statuses"))
except Exception as e:
    print("Status check skipped:", repr(e))
    print(f'CLI:  databricks postgres list-cdf-statuses "{parent}"')
