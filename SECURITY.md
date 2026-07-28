# Security

## Repository visibility

This repository contains blinded assignment files. Those files include reference answers because
the server must reveal them during Stage 2. The GitHub repository **must remain private**. Giving an
annotator repository access would unblind the study.

Never commit:

- `.env` or an administrative token;
- mutable files from `runtime/` or a Railway volume;
- raw annotation exports;
- `human_eval_key.jsonl`, `human_eval_units.jsonl`, generated datasets, or QA checkpoints.

## Administrative credential

Prefer a Railway secret named `TCRED_ADMIN_TOKEN` with at least 43 ASCII characters. If it is
omitted, the service generates a 64-character token once and stores it as `.admin-token` inside the
persistent state directory with owner-only permissions. Retrieve that generated value only through
an authenticated Railway shell:

```bash
python -m tcred_annotation_tool admin-token
```

Never put the token in a URL, screenshot, issue, browser storage, or Git history. Administrative
requests use `Authorization: Bearer <token>`.

## Data protection

Participant labels are pseudonymous and minimized. The writable state does not copy questions,
answers, evidence, or graph content. Participant write endpoints are same-origin and use signed,
expiring assignment sessions. Administrative endpoints are bearer-protected and rate-limited.
Exports exclude debug annotator 0, secrets, source assignments, local paths, and administrator logs.

Report a suspected credential or data exposure privately to the study owner. Rotate the
administrative token and archive the current annotation export before redeploying.
