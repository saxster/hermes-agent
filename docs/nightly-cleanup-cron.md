# Nightly cleanup cron

`cron.output_retention.nightly_cleanup()` is a shipped maintenance helper
that:

- rotates every job's output dir under `~/.hermes/cron/output/{job_id}/`,
  honoring `MAX_AGE_DAYS` (30) and `MAX_SIZE_BYTES_PER_JOB` (1 GB)
- prunes ended sessions older than 90 days from `~/.hermes/state.db`
- reports `{files_deleted, bytes_reclaimed, sessions_pruned}` in the cron
  output

Without it, long-running single-user deployments accumulate:

- thousands of empty session stubs (FTS5 search slows down)
- multi-GB cron output trees (disk exhaustion)

## Recommended cron entry

```bash
hermes cron add "0 3 * * *" \
  "[SILENT] Run nightly cleanup: rotate cron output, prune old sessions." \
  --script "python -c 'from cron.output_retention import nightly_cleanup; import json; print(json.dumps(nightly_cleanup()))'"
```

The `[SILENT]` prefix suppresses delivery to messaging platforms when
nothing interesting happened. The `--script` flag routes the script's
stdout into the LLM prompt (see memory entry
`cron_script_injection_automation` for the contract).

## Operator verification

```bash
# Dry run: no destructive changes
python -c 'from cron.output_retention import nightly_cleanup; print(nightly_cleanup(dry_run=True))'

# Real run (only when you explicitly want to trigger it)
python -c 'from cron.output_retention import nightly_cleanup; print(nightly_cleanup())'
```

## Configuration

- Change the retention thresholds by editing `MAX_AGE_DAYS` /
  `MAX_SIZE_BYTES_PER_JOB` at the top of `cron/output_retention.py`, or
  by passing a custom `prune_older_than_days` to `nightly_cleanup`.
- Per-job rotation state is persisted to
  `~/.hermes/cron/output/.rotation_state.json` (F-M1) so gateway
  restarts do not reset cooldowns.

## Audit reference

F-M1 in the 2026-04-15 production audit. Rotation persistence shipped
in commit `d07b01c2`; this cleanup helper and the cron recipe were
added as the final piece.
