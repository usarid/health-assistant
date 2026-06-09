# Backup and Restore

Off-machine, encrypted, zero-knowledge backups via [restic](https://restic.net) to [Backblaze B2](https://www.backblaze.com/cloud-storage). Cost: ~$0.04/month for ~8 GB. Restore-tested.

## What's backed up

Everything irreplaceable (~8 GB total):

| What | Source | Size |
|---|---|---|
| HAPI v1 + v2 postgres (logical dumps) | `docker exec phv-postgres{,-v2} pg_dumpall` | ~2 GB |
| App SQLite (chat, overrides, reminders, Epic tokens, prefs) | `docker exec phv-api sqlite3 /data/chat.db .dump` | ~0.5 MB |
| AHR + raw portal exports | `~/usarid@gmail.com/Medical/` | ~6 GB |
| User-uploaded pill photos | `data/pillbox/user_photos/` | ~10 MB |
| v2 generated bundles + diffs | `tools/v2/out/` | varies |
| v3 live scrape outputs (incl. Stanford notes) | `tools/v3/out/` | varies |
| Patient-specific configs | `tools/v2/patient_config/{org_mapping,patient_identity}.json` | <1 MB |
| .env (API keys, Epic credentials, etc.) | `.env` if present | <1 KB |

Skipped (regenerable from other sources): OpenSearch (rebuilt from postgres on demand), `data/pillbox/` images (separate GitHub repo `usarid/nlm-pillbox-images`), Python venvs, any code (lives in git).

## One-time setup

### 1. Create Backblaze B2 account + bucket + key

Browser steps, ~5 min:

1. Sign up at <https://www.backblaze.com/cloud-storage> (free tier: 10 GB storage, 1 GB/day download — comfortably fits 8 GB)
2. **Buckets → Create a Bucket**
   - Name: `binahealth-restic` (or anything; remember it)
   - Files private (default)
   - Encryption: B2 default is fine (restic encrypts client-side anyway)
   - Object Lock: off
3. **App Keys → Add a New Application Key**
   - Name: `binahealth-restic`
   - Allow access to bucket: just the one above
   - Type: Read and Write
   - Copy `keyID` and `applicationKey` immediately — `applicationKey` is shown ONCE

### 2. Generate your client-side encryption password

```bash
openssl rand -base64 32
```

Save the output somewhere durable (password manager). This is what restic uses to encrypt your backups; if you lose it, the backups are unrecoverable. B2's credentials let restic upload to your bucket but can't decrypt the contents — only this password can.

### 3. Configure the env file

```bash
cp scripts/backup-env.example ~/.binahealth-backup-env
chmod 600 ~/.binahealth-backup-env
$EDITOR ~/.binahealth-backup-env
```

Fill in the four values (B2 keyID, B2 applicationKey, repository URL with your bucket name, and the openssl-generated password).

### 4. Initialize the repo + run the first backup

```bash
scripts/backup-init.sh    # validates env, runs `restic init` on the bucket
scripts/backup.sh         # first backup; uploads everything, ~5-15 min for ~8 GB
```

### 5. Verify restore works (do this!)

```bash
scripts/backup-restore.sh /tmp/binahealth-restore-test
# Compare a few files between /tmp/binahealth-restore-test and their originals.
rm -rf /tmp/binahealth-restore-test
```

A backup you've never restored from is hope, not a backup.

### 6. Install the nightly schedule

```bash
scripts/install-backup-schedule.sh
```

This creates a launchd agent that runs `scripts/backup.sh` every day at 02:00 local time. Logs land in `~/Library/Logs/BinaHealth/backup.{log,err}`.

To check it's loaded: `launchctl list | grep com.binahealth`
To run on demand: `launchctl start com.binahealth.backup`
To remove: `scripts/uninstall-backup-schedule.sh`

## Day-to-day

After setup, you don't touch this. To spot-check:

```bash
source ~/.binahealth-backup-env
restic snapshots --compact    # list snapshots
restic stats                  # repo size, dedup ratio
tail ~/Library/Logs/BinaHealth/backup.log
```

## Disaster recovery

If the Mac mini dies and you need to bring this up on a new machine:

1. Install Docker + the BinaHealth repo: `git clone https://github.com/usarid/health-assistant.git`
2. `cd health-assistant && docker compose up -d` (starts empty HAPI containers)
3. Install restic: `brew install restic`
4. Recreate the env file at `~/.binahealth-backup-env` (restic password + B2 keys from your password manager)
5. Restore: `scripts/backup-restore.sh /tmp/restore`
6. Pipe the dumps back in:
   ```bash
   docker exec -i phv-postgres    psql -U hapi < /tmp/restore/<stage>/hapi-v1.sql
   docker exec -i phv-postgres-v2 psql -U hapi < /tmp/restore/<stage>/hapi-v2.sql
   docker exec -i phv-api sqlite3 /data/chat.db < /tmp/restore/<stage>/chat.sql
   ```
7. Rsync raw exports + pill photos back to their original paths.
8. `./scripts/setup-pillbox.sh` to re-clone the (separate, public) pillbox image archive.

## Retention

Default: 7 daily + 4 weekly + 12 monthly snapshots, pruned automatically. Edit `scripts/backup.sh` if you want different.

## Cost ballpark

At Backblaze B2's `$0.005/GB-month` storage and free download (first 3× monthly storage size):

| State | Storage | Monthly cost |
|---|---|---|
| 8 GB | $0.04 | ~free (under 10 GB free tier) |
| 80 GB (if data grew 10x) | $0.40 | $0.40 |

Restic deduplicates aggressively, so a year of daily snapshots typically uses 2-3× the size of one full snapshot, not 365×.
