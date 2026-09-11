/**
 * ZOO-459 one-off migration: fold stray anonymous owners/learners (created by
 * incognito windows / other browser profiles) back into the main browser's
 * identity.
 *
 * - document_stages + stage_meta: owner_id is a plain column (not embedded in
 *   the JSONB payload), so a guarded UPDATE moves them.
 * - runtime_sessions: the learner key IS embedded in each session's JSON
 *   payload, so this uses the real PgRuntimeStore.mergeLearner (same code the
 *   HTTP /learners/merge endpoint calls) instead of hand-rolled SQL.
 *
 * Backups land in zoo459_backup_* tables first; re-running is a no-op.
 */
import { Pool } from 'pg';

const CONNECTION = 'postgres://openmaic:openmaic-dev@localhost:5432/openmaic';
const MAIN_OWNER = 'anon:33a45246-abf3-4675-b526-04a53222a9c0';
const STRAY_OWNERS = [
  'anon:17992467-8807-4eef-9807-e455acd9a0b7',
  'anon:c1d2d999-d7eb-4715-8f8a-eb0a7307fb77',
];
const MAIN_LEARNER = 'anon:039f7df7-a42c-4bb2-8c47-722a12d7f86c';
const STRAY_LEARNERS = ['anon:b33e9990-d209-4ed7-b168-85b6e9074b9a'];

const pool = new Pool({ connectionString: CONNECTION });
const placeholders = (n) => Array.from({ length: n }, (_, i) => `$${i + 1}`).join(', ');

async function migrateDocuments() {
  await pool.query(`CREATE TABLE IF NOT EXISTS zoo459_backup_document_stages AS
    SELECT * FROM document_stages WHERE false`);
  await pool.query(`CREATE TABLE IF NOT EXISTS zoo459_backup_stage_meta AS
    SELECT * FROM stage_meta WHERE false`);
  await pool.query(
    `INSERT INTO zoo459_backup_document_stages
       SELECT d.* FROM document_stages d
        WHERE d.owner_id = ANY($1) AND NOT EXISTS (
          SELECT 1 FROM zoo459_backup_document_stages b WHERE b.id = d.id AND b.owner_id = d.owner_id)`,
    [STRAY_OWNERS],
  );
  await pool.query(
    `INSERT INTO zoo459_backup_stage_meta
       SELECT m.* FROM stage_meta m
        WHERE m.owner_id = ANY($1) AND NOT EXISTS (
          SELECT 1 FROM zoo459_backup_stage_meta b WHERE b.stage_id = m.stage_id)`,
    [STRAY_OWNERS],
  );

  // Name collision guard: PK is (owner_id, id) and folders have
  // UNIQUE (owner_id, normalized_name) — stage ids are freshly minted so a
  // collision here would mean the same id already exists under MAIN_OWNER.
  const clash = await pool.query(
    `SELECT d.id FROM document_stages d
       WHERE d.owner_id = ANY($1)
         AND EXISTS (SELECT 1 FROM document_stages m WHERE m.owner_id = $2 AND m.id = d.id)`,
    [STRAY_OWNERS, MAIN_OWNER],
  );
  if (clash.rows.length > 0) {
    throw new Error(`stage id collision with main owner: ${clash.rows.map((r) => r.id).join(', ')}`);
  }

  const stages = await pool.query(
    `UPDATE document_stages SET owner_id = $2 WHERE owner_id = ANY($1) RETURNING id`,
    [STRAY_OWNERS, MAIN_OWNER],
  );
  const metas = await pool.query(
    `UPDATE stage_meta SET owner_id = $2 WHERE owner_id = ANY($1) RETURNING stage_id`,
    [STRAY_OWNERS, MAIN_OWNER],
  );
  console.log(`document_stages moved: ${stages.rows.length} (${stages.rows.map((r) => r.id).join(', ')})`);
  console.log(`stage_meta moved: ${metas.rows.length} (${metas.rows.map((r) => r.stage_id).join(', ')})`);
}

async function migrateRuntime() {
  await pool.query(`CREATE TABLE IF NOT EXISTS zoo459_backup_runtime_sessions AS
    SELECT * FROM runtime_sessions WHERE false`);
  await pool.query(
    `INSERT INTO zoo459_backup_runtime_sessions
       SELECT s.* FROM runtime_sessions s
        WHERE s.learner_key = ANY($1) AND NOT EXISTS (
          SELECT 1 FROM zoo459_backup_runtime_sessions b
           WHERE b.id = s.id AND b.learner_key = s.learner_key)`,
    [STRAY_LEARNERS],
  );

  const { PgRuntimeStore, ensureSchema } = await import('@openmaic/storage/runtime/pg');
  const { nodePostgresTransaction } = await import('@openmaic/storage/server/reference');
  const queryable = pool;
  await ensureSchema(queryable);
  const store = new PgRuntimeStore(queryable, { withTransaction: nodePostgresTransaction(pool) });
  for (const stray of STRAY_LEARNERS) {
    const moved = await store.mergeLearner(stray, MAIN_LEARNER);
    console.log(`runtime sessions merged ${stray} -> ${MAIN_LEARNER}: ${moved}`);
  }
}

try {
  await migrateDocuments();
  await migrateRuntime();
  const owners = await pool.query(
    `SELECT owner_id, count(*) FROM document_stages GROUP BY owner_id ORDER BY 2 DESC`,
  );
  console.log('stage owners after migration:');
  for (const r of owners.rows) console.log(`  ${r.owner_id} -> ${r.count}`);
  const learners = await pool.query(
    `SELECT learner_key, count(*) FROM runtime_sessions GROUP BY learner_key ORDER BY 2 DESC`,
  );
  console.log('runtime learners after migration:');
  for (const r of learners.rows) console.log(`  ${r.learner_key} -> ${r.count}`);
} finally {
  await pool.end();
}
