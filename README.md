# T-CRED Annotation Tool

Standalone deployment of the T-CRED protocol 4.2 human-annotation workflow. It includes the
participant UI, six-card tutorial 5.2, 36 frozen blinded assignments of 20 units each, protected
progress/export/clear APIs, local autosave, server autosave, final submission locking, and the
annotator guide.

## Important

**Keep this repository private.** The server needs Stage 2 reference answers, so those answers are
present in the frozen assignment files even though the API withholds them during Stage 1. A public
repository would unblind the study.

Only deploy with persistent storage. Railway service files are otherwise ephemeral and can
disappear on redeploy. The checked-in configuration requires a volume mounted at `/data`.

## Railway

The repository is ready for Railway:

- pinned Python 3.12 container image;
- deterministic dependency versions;
- `0.0.0.0` binding and Railway-injected `PORT`;
- `/api/health` deployment health check;
- one replica, no overlapping deployment writers;
- required `/data` volume;
- automatic immutable assignment verification;
- automatic privilege drop after preparing the Railway volume;
- automatically generated persistent admin token when no secret is injected.

See [docs/RAILWAY_DEPLOYMENT.md](docs/RAILWAY_DEPLOYMENT.md) for the exact deployment and
verification procedure. Railway cannot create a volume from `railway.json`; attach `/data` once or
deploy through a Railway Template that already contains the volume. A repository deployment also
needs one **Generate Domain** action before annotators can reach it. A template can retain both
resources for later one-click deployments.

## Local run

Python 3.12 and [uv](https://docs.astral.sh/uv/) are recommended:

```powershell
uv sync --extra dev
uv run python -m tcred_annotation_tool check
uv run python -m tcred_annotation_tool serve
```

Open `http://127.0.0.1:8770`. IDs `1` through `36` are production assignments. ID `0` is reserved
for testing and is excluded from exports and label import.

The generated local admin token is stored in ignored `runtime/.admin-token`. Read it explicitly:

```powershell
uv run python -m tcred_annotation_tool admin-token
```

To inject your own token, create an ignored `.env` from `.env.example` and use at least 43 random
ASCII characters.

## Data layout

Immutable, committed deployment inputs:

```text
data/
  assignments/                 36 blinded assignment files
  deployment_manifest.json    protocol, counts, and SHA-256 checksums
```

Mutable, ignored local state or Railway `/data`:

```text
labels_raw/
  annotator_XX.jsonl           minimized labels
  submissions/                 revision-bound final submissions
  audit/                       minimized save/submission events
  .admin/                      activity and protected admin events
  .collection.json             browser/session generation
backups/                       pre-clear recovery exports
.admin-token                   generated owner credential, when needed
```

The repository intentionally excludes the private evaluation key, global unit table, generated
worlds, QA outputs, and metric code. Frozen graph paths are already enriched with readable nodes,
relations, direction, time, and evidence text, so no source dataset is required at runtime.

## Participant workflow

1. Enter the supplied annotator ID.
2. Read the study rules and complete four guided plus two independently checked tutorial cards.
3. Annotate 20 frozen units in increasing estimated complexity.
4. For each unit, complete evidence/time judgments before revealing the reference answer.
5. Complete answer/refusal judgments, review all units, and submit.

Research edits are saved to browser storage immediately and sent to the server 1.5 seconds after
the latest change. Server writes are atomic and revision checked. Final submission makes the
assignment read-only.

The detailed participant document is [docs/annotation-guidelines.pdf](docs/annotation-guidelines.pdf).

## Administration

All administrative endpoints require:

```http
Authorization: Bearer <TCRED_ADMIN_TOKEN>
```

Available endpoints:

- `GET /api/admin/progress`
- `GET /api/admin/export`
- `POST /api/admin/clear/prepare`
- `POST /api/admin/clear/confirm`
- `GET /api/health`
- `GET /api/config`
- `GET /api/docs`
- `GET /api/openapi.json`

Clearing requires a short-lived state-bound challenge, the exact confirmation phrase, and an
optional pre-clear recovery export. It never deletes assignments.

## Updating assignments

Copy only a verified release's blinded assignment files:

```powershell
uv run python scripts/prepare_assignments.py `
  C:\path\to\tcred_release `
  --destination data
uv run python -m tcred_annotation_tool check
```

The preparation script verifies source hashes and writes a minimized deployment manifest. It never
copies `human_eval_key.jsonl`, `human_eval_units.jsonl`, generated datasets, or QA checkpoints.

## Quality checks

```powershell
uv run ruff check .
uv run pytest -q
uv run python -m tcred_annotation_tool check
docker build -t tcred-annotation-tool .
```

See [SECURITY.md](SECURITY.md) before granting repository access or sharing the deployment URL.
