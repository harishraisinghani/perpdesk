# Deploy PerpDesk with GitHub and Databricks Apps

## Recommended deployment model

GitHub is the source of truth. A GitHub Actions workflow checks out `main`, runs the test suite,
deploys the complete Databricks bundle, restarts the app, and waits for the app to become healthy.
GitHub authenticates to Databricks with OIDC workload identity federation, so the repository does
not store a Databricks token or client secret.

This deliberately uses a checked-out GitHub commit as the bundle's workspace source instead of
Databricks automatic Git deployments. Automatic Git deployment currently covers the app only and
would require a separate Git credential on the app service principal for this private repository.
The bundle workflow releases the app, pipeline, and job together and keeps one deployment audit
trail.

```text
Pull request -> GitHub main -> GitHub Actions (OIDC)
                                  |
                                  v
                         Databricks bundle deploy
                           |        |         |
                           v        v         v
                     Lakeflow   History Job  Databricks App
                                                |
                                                v
                                             Lakebase
```

The app bundle source is the repository root. This is intentional: `app/main.py` imports the
top-level `perpdesk` package, and Databricks must also see the root `requirements.txt` and
`app.yaml`.

## Important sharing limitation

Databricks Apps provides a stable HTTPS URL, but it does not support anonymous public access.
Every visitor must authenticate with an identity recognized by the Databricks account. The bundle
grants `CAN USE` to the workspace `users` group, so workspace users can open the shared link.

For external collaborators, onboard them through the organization's identity provider using SCIM
or just-in-time provisioning and grant them `CAN USE`. If the requirement is a truly anonymous
internet site, the web tier must be hosted outside Databricks; Databricks Apps alone cannot meet
that requirement.

See [Databricks app permissions](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/permissions).

## Public demo deployment on Vercel

The Databricks App is the internal deployment. For a link anyone can open without a Databricks
identity, the same FastAPI app also runs on Vercel, still reading live Lakebase through the same
service principal and OAuth credential flow. Nothing about the Databricks bundle changes; the two
deployments run side by side from one repository.

Vercel resolves `app/main.py` automatically because it exports a `FastAPI` instance named `app` at a
supported entrypoint, and it promotes the `StaticFiles` mount to the CDN at build time. The only
committed configuration is [vercel.json](vercel.json), which raises `maxDuration` to cover the
Hyperliquid candle proxy, and [.vercelignore](.vercelignore), which keeps the Databricks deployment
surface out of the function bundle.

### Import the project

Point Vercel at this repository. Leave the framework preset on its detected value and set these
environment variables:

| Variable | Value |
|---|---|
| `PERPDESK_DEMO_MODE` | `false` |
| `DATABRICKS_HOST` | `https://dbc-306f45a0-f996.cloud.databricks.com` |
| `DATABRICKS_CLIENT_ID` | Application ID of the app's service principal |
| `DATABRICKS_CLIENT_SECRET` | OAuth secret for that service principal |
| `ENDPOINT_NAME` | `projects/<project-id>/branches/<branch-id>/endpoints/<endpoint-id>` |
| `PGHOST` | `ep-little-pine-d8r534ew.database.us-east-2.cloud.databricks.com` |
| `PGDATABASE` | `databricks_postgres` |
| `PGUSER` | The service principal client ID, not a username |
| `PGPORT` | `5432` |

Vercel cannot use the GitHub OIDC federation that the Actions workflow relies on, so this identity
needs a real OAuth client secret. Give it the same Lakebase grants as the app identity in
[sql/08_app_grants.sql](sql/08_app_grants.sql); it only needs to read.

### Writes are closed by default

`app/main.py` treats a serverless deployment as public and refuses writes unless told otherwise, so
`POST /api/watchlist/promote` and `PATCH /api/alerts/{id}/acknowledge` return `403` on Vercel and the
dashboard hides both controls. This is driven by `PERPDESK_READ_ONLY`, which defaults to `true` when
`VERCEL` is present and `false` everywhere else. The Databricks App and local development keep write
access with no configuration.

Set `PERPDESK_READ_ONLY=false` on Vercel only if you intend anonymous visitors to mutate Lakebase.

### Connection pooling

Serverless instances are frozen between requests and get 500ms after `SIGTERM` to clean up, which
strands pooled connections that Lakebase never sees closed. When `VERCEL` is set, the app swaps the
collector's long-lived pool for `min_size=0`, `max_size=2`, a 300-second `max_lifetime`, and a
liveness check on checkout, so a connection that died while the instance was frozen is replaced
rather than handed to a request. The collector's own pool settings are unchanged.

## One-time setup

### 1. Create the deployment identity

Create a Databricks service principal named `perpdesk-deployer`, assign it to the workspace, and
grant it the permissions needed to manage this bundle's app, job, pipeline, SQL warehouse, and
Lakebase project. Grant only those production resources rather than making it an account admin.

Create a GitHub Actions federation policy for:

```text
repo:harishraisinghani/conductor-playground:environment:prod
```

Use the GitHub OIDC issuer `https://token.actions.githubusercontent.com`. Follow the official
[GitHub Actions federation guide](https://docs.databricks.com/aws/en/dev-tools/auth/provider-github).

### 2. Configure the GitHub production environment

In the GitHub repository, open **Settings -> Environments**, create `prod`, and add these
environment variables:

| Variable | Value |
|---|---|
| `DATABRICKS_HOST` | `https://dbc-306f45a0-f996.cloud.databricks.com` |
| `DATABRICKS_CLIENT_ID` | Application ID of `perpdesk-deployer` |

These are identifiers, not secrets. The federation policy is what authorizes the workflow. Add a
required reviewer to the environment if production deploys should need approval.

### 3. Run the first deployment

This workspace already has the pipeline and history job under the development bundle target. First
grant `perpdesk-deployer` `CAN MANAGE` on both existing resources. Without this migration, the first
production deployment would create a second pipeline and a second scheduled history job.

Push the repository to `main`, then open **GitHub -> Actions -> Deploy PerpDesk to Databricks** and
run the workflow manually with **Adopt existing resources** checked. That one-time option binds the
existing pipeline and job to the production deployment state before deploying. Future pushes to
`main` deploy automatically and do not run the adoption step.

After this migration, use `prod` as the only deployment target for these resources; do not deploy
the old `dev` target again.

The workflow performs:

```text
pytest -> bundle validate -> bundle deploy -> bundle run perpdesk_app -> health wait
```

`bundle deploy` creates or updates the app but does not restart its process, which is why the
separate `bundle run` step is required.

### 4. Grant the app access to the existing Lakebase tables

Databricks creates a dedicated service principal and PostgreSQL role for the app. The Lakebase
resource binding supplies `PGHOST`, `PGDATABASE`, `PGPORT`, `PGUSER`, and app OAuth credentials at
runtime. It does not grant access to tables already owned by the database admin.

Get the app's client ID:

```bash
databricks apps get perpdesk --output json \
  | jq -r '.service_principal_client_id'
```

In the Lakebase SQL editor, open [sql/08_app_grants.sql](sql/08_app_grants.sql), replace every
`<app-client-id>` with that client ID, and run the script as the database owner. Then restart the
app:

```bash
databricks bundle run perpdesk_app --target prod
```

This is a one-time grant because the app service principal remains stable across deployments.

## Get and share the app link

```bash
databricks apps get perpdesk --output json | jq -r '.url'
```

Share that URL with a user who has `CAN USE`. To narrow access later, remove the `users` grant in
`resources/perpdesk.yml` and grant specific groups instead.

## Manual deployment and diagnostics

The same release can be performed from an authenticated terminal:

```bash
export BUNDLE_VAR_deployer_service_principal=<deployer-app-id>
databricks bundle validate --target prod
databricks bundle deploy --target prod
databricks bundle run perpdesk_app --target prod
databricks apps get perpdesk --output json
```

Inspect startup and runtime failures with:

```bash
databricks apps logs perpdesk --follow
```

The app listens on the Databricks-provided `DATABRICKS_APP_PORT`, uses its own injected identity,
and does not read `.env` in production.

## Operational boundary

This deploys the FastAPI dashboard and the existing Lakeflow/history resources. The continuous
Hyperliquid WebSocket collector is a separate ingestion service and must remain running for live
Lakebase state to advance. Do not hide it inside the Databricks App process: app deployments and
restarts would interrupt collection. If all compute must run on Databricks, package the collector
as a separate Jobs workload and choose compute that supports a long-running network process; do
not couple its lifecycle to the web app.
