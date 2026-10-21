# Moving the vault and skills into Postgres (#929 stage 2)

The vault (`/app/var/vault`) and skills (`/app/var/skills`) live on one box's disk until `BSVIBE_KNOWLEDGE_STORE_BACKEND=postgres`. After that they are `vault_documents` rows that every box shares. The Markdown does not change: each row is one file, byte for byte, at its relative path. Design: `docs/design/stateless-workers.md` §5.

This is a founder step. Agents do not exec into production containers.

## Before

- The deployed `main` includes the `vault_documents` migration and `backend.knowledge.store`.
- Sizing (read-only), in the backend container:

  ```sh
  docker exec bsvibe-prod-backend-1 python -m backend.knowledge.store import --dry-run
  ```

  It prints `would write N · unchanged 0 · skipped 0`. A non-zero `skipped` lists files that are not UTF-8; look at them before going on.

## Cut-over (about a minute of downtime)

The vault is a few MB, so the import takes seconds. Stop the writers first, so that nothing lands on disk after the copy.

1. Stop the writers:

   ```sh
   docker compose -p bsvibe-prod stop backend worker
   ```

2. Import, with the stopped backend's image and volume:

   ```sh
   docker compose -p bsvibe-prod run --rm backend python -m backend.knowledge.store import
   ```

   It should print `wrote N · unchanged 0 · skipped 0`, and a second run should print `wrote 0 · unchanged N`.

3. Set `BSVIBE_KNOWLEDGE_STORE_BACKEND=postgres` in `deploy/.env.prod`.

4. Start again:

   ```sh
   docker compose -p bsvibe-prod up -d backend worker
   ```

5. Check:
   - The PWA's knowledge graph and a decision listing show the same content as before.
   - `bsvibe_skills_list` shows the same skills.
   - The logs show no `FileNotFoundError` or `store_*` warnings.

Keep `/app/var/vault` and `/app/var/skills` untouched as the backup until a week of clean runs. Nothing reads them while the setting is `postgres`.

## Rollback

Writes made after the cut-over exist only in the rows, so export them first:

```sh
docker compose -p bsvibe-prod run --rm backend python -m backend.knowledge.store export /app/var/vault-export
```

Then:

1. Stop backend and worker.
2. Move `/app/var/vault-export/vault/*` over `/app/var/vault/` and `/app/var/vault-export/skills/*` over `/app/var/skills/`.
3. Set the setting back to `filesystem` and start again.

## A second box

A second machine needs all of these, and not before:

- `postgres` here;
- stage 1 live (`BSVIBE_MACHINE_ID` set, a different value per box);
- the product bundle store on R2 (`BSVIBE_PRODUCT_BUNDLE_BACKEND=s3`);
- the stage 4 fixes (`docs/design/stateless-workers.md` §6).
