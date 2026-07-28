# Annotation Admin Dashboard

This module is the loopback-only owner client for the T-CRED annotation service. It displays
privacy-minimized progress, downloads current exports, stores verified backups, configures a
periodic backup scheduler, and exposes the deliberately guarded clear-data workflow.

It is a separate process from the participant-facing annotation service:

```text
browser -> 127.0.0.1:8772 owner dashboard
owner dashboard -> configured annotation base URL on port 8770
owner dashboard -> .annotations-backup
```

The browser never receives or stores `TCRED_ADMIN_TOKEN`. The dashboard backend reads the token,
sends it to the annotation server through the `Authorization` header, and returns only sanitized
results. The dashboard refuses a non-loopback bind, rejects non-loopback `Host` headers, and
requires both same-origin requests and an ephemeral CSRF token for every mutation.

## Configuration

The default annotation base URL is:

```text
http://127.0.0.1:8770
```

For a deployed server, change it in either of these places:

1. set `TCRED_ANNOTATION_BASE_URL=https://annotations.example.org` in the process environment or
   `src/tcred/annotation_admin/.env`; or
2. pass `--base-url https://annotations.example.org` to the command.

A remote URL must use HTTPS. Do not include credentials, a query, or a fragment in the URL.

Copy `.env.example` to `.env` only for a private single-host installation. The token lookup order
is:

1. process environment;
2. `src/tcred/annotation_admin/.env`;
3. `src/tcred/annotation_ui/.env`, so the local dashboard can reuse the annotation service token
   without duplicating it.

The real `.env`, generated ZIP files, and `.annotations-backup` directory are ignored and excluded
from package builds.

## Run

Start the annotation service first:

```powershell
uv run tcred annotation-ui --no-open-browser
```

Then start the owner dashboard:

```powershell
uv run tcred annotation-admin-ui
```

Open `http://127.0.0.1:8772/`.

Example for a deployed annotation API:

```powershell
uv run tcred annotation-admin-ui `
  --base-url https://annotations.example.org `
  --backup-dir .annotations-backup `
  --backup-retention 168
```

The dashboard itself should remain on the administrator's computer or another loopback-only
management host. Do not publish port 8772 through a reverse proxy.

## Features

- aggregate cards for started, tutorial-complete, active, ready, finished, and completed-answer
  counts;
- two completion bars and a 36-row pseudonymous annotator table;
- service connectivity and the remote API documentation link;
- browser download of the current checksum-manifested ZIP;
- immediate server-side backup into `.annotations-backup`;
- disabled, one-hour, two-hour, six-hour, and daily scheduler settings;
- persisted non-secret scheduler state, recent-backup list, checksums, ZIP validation, atomic writes,
  and configurable retention;
- a two-step clear-data dialog with an exact phrase and explicit acknowledgement.

The scheduler runs only while the owner-dashboard process is running. Its interval survives a
restart, but the process still needs to be managed by the operating system if uninterrupted backups
are required.

## Clear-Data Safety

The dashboard does not send a direct delete request. It first asks the annotation service for a
five-minute challenge bound to a SHA-256 fingerprint of the current participant state. The final
request must contain:

- the bearer credential, held only by the dashboard backend;
- the challenge token;
- the matching state fingerprint;
- the exact phrase `DELETE ALL ANNOTATION DATA`;
- an explicit acknowledgement in the browser.

If an annotator saves anything after the warning appears, the state fingerprint changes and the
server rejects deletion. The dialog must then be prepared again.

On success, the server rotates its collection generation. This invalidates every outstanding
assignment save token and changes the annotator browser-storage namespace, preventing a stale tab
or pilot-era local draft from recreating deleted records.

The operation deletes only known mutable files inside the configured annotation output directory:

- `annotator_*.jsonl` live labels, including debug ID 0;
- save-audit records;
- final-submission records;
- tutorial/assignment activity state;
- old administrator-operation audit records.

It never deletes assignments, generated datasets, QA outputs, private gold keys, source documents,
or files outside the live annotation output directory. When recovery backup is selected, a
privacy-minimized ZIP is written outside the live store before deletion. The server writes a new
administrative success receipt after clearing.

## Backup Semantics

Every downloaded archive must:

- be smaller than the configured 64 MiB safety limit;
- match the `X-Content-SHA256` header;
- be a valid ZIP with no absolute or parent-traversal paths.

The dashboard writes a temporary file, verifies it again, atomically renames it, and applies private
file permissions where the platform supports them. Retention applies only to ordinary
`tcred-annotations-*.zip` scheduler exports. It does not automatically delete `pre-clear-*.zip`
recovery archives.

Backups contain pseudonymous labels and therefore remain research data. Store the directory on
encrypted storage, restrict access, test restoration, and apply the study's retention policy.
