# Railway Deployment

## Non-negotiable prerequisites

1. Keep the GitHub repository **private**. Assignment JSONL files contain hidden Stage 2 reference
   answers.
2. Attach exactly one Railway volume to the service at `/data`.
3. Keep the service at one replica. The current store uses revision-checked atomic files and is not
   a distributed database.

Railway's repository-level Config as Code controls one service's build and deploy settings, but it
does not create project resources. A volume must therefore be attached once in the Railway project
or captured in a Railway Template. The checked-in `requiredMountPath` prevents an unsafe deployment
from accepting annotations on ephemeral storage.

Official references:

- [Config as Code](https://docs.railway.com/config-as-code/reference)
- [Railway volumes](https://docs.railway.com/volumes)
- [Working with domains](https://docs.railway.com/networking/domains/working-with-domains)
- [Creating a Railway Template](https://docs.railway.com/templates/create)
- [Deploying from GitHub](https://docs.railway.com/services)

## First deployment from GitHub

1. Push this repository privately to GitHub.
2. In Railway, choose **New Project > Deploy from GitHub repo** and select this repository.
3. Attach a volume to the generated service with mount path `/data`.
4. Deploy. Railway detects the root `Dockerfile`, reads `railway.json`, injects `PORT`, and waits
   for `GET /api/health`.
5. In **Settings > Networking > Public Networking**, choose **Generate Domain**.

No build command, start command, port, assignment path, database, or application variable is
required. The service generates its administrative credential inside `/data` when
`TCRED_ADMIN_TOKEN` is absent.

The two unavoidable dashboard actions are attaching the volume and generating the domain. A plain
GitHub repository cannot provision either project resource. To make later installations one-click,
convert the configured Railway project into a private template. In the template composer retain:

- GitHub source: this private repository;
- public HTTP networking: enabled;
- volume mount: `/data`;
- replicas: `1`;
- health check: `/api/health`;
- optional variable: `TCRED_ADMIN_TOKEN=${{secret(64)}}`.

After that, the template URL provisions the service, volume, networking, and generated secret
together.

## Verification before sharing

Run these checks against the deployed domain:

```bash
curl -fsS https://YOUR-DOMAIN.up.railway.app/api/health
curl -fsS https://YOUR-DOMAIN.up.railway.app/api/config
```

Expected responses:

```json
{"status":"ok"}
{"annotator_count":36}
```

Open the root URL and test annotator `0`. Production annotators are IDs `1` through `36`.

Retrieve the generated admin token from an authenticated Railway shell:

```bash
python -m tcred_annotation_tool admin-token
```

Then verify protected progress:

```bash
curl -fsS \
  -H "Authorization: Bearer $TCRED_ADMIN_TOKEN" \
  https://YOUR-DOMAIN.up.railway.app/api/admin/progress
```

## Backups and updates

Download `GET /api/admin/export` before every code or assignment update and at least daily while
annotation is active. Also enable Railway volume backups if the selected plan supports them.

The service deliberately sets deployment overlap to zero. Two deployments must not write to the
same file store concurrently. Assignment checksums are verified before the HTTP server starts; a
corrupt or incomplete bundle fails before any participant can load it.

Do not replace `data/assignments` manually. Use:

```bash
python scripts/prepare_assignments.py PATH/TO/tcred_release --destination data
python -m tcred_annotation_tool check
```

Changing assignments during an active campaign invalidates comparability. Archive and clear the
previous collection first.
