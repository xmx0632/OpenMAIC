/**
 * ZOO-477 one-off migration: fold every stray learner partition (each browser
 * profile / incognito window mints its own `anon:<uuid>`) into the pinned
 * learner key (NEXT_PUBLIC_LEARNER_KEY_OVERRIDE in .env.local), so the
 * below-video TTS / voice-interaction sessions are visible from any browser.
 *
 * Uses the real PgRuntimeStore.mergeLearner (same code the HTTP
 * /learners/merge endpoint calls) instead of hand-rolled SQL: the learner key
 * is embedded in each session's JSON payload and mergeLearner rewrites it
 * safely. Records follow via FK; session ids keep their historical learner
 * segment (the client matches ids by stage + chat-session id only).
 *
 * Backups land in zoo477_backup_runtime_sessions first; re-running is a no-op.
 */
import { readFileSync } from 'node:fs';
import { Pool } from 'pg';

function learnerOverrideFromEnv() {
  const match = readFileSync(new URL('../.env.local', import.meta.url), 'utf8')
    .split('\n')
    .find((line) => line.startsWith('NEXT_PUBLIC_LEARNER_KEY_OVERRIDE='));
  return match?.slice('NEXT_PUBLIC_LEARNER_KEY_OVERRIDE='.length).trim();
}

const MAIN_LEARNER = learnerOverrideFromEnv();
if (!MAIN_LEARNER) {
  console.error(
    'NEXT_PUBLIC_LEARNER_KEY_OVERRIDE is not set in .env.local — nothing to merge into.',
  );
  process.exit(1);
}

const CONNECTION = 'postgres://openmaic:openmaic-dev@localhost:5432/openmaic';
const pool = new Pool({ connectionString: CONNECTION });

try {
  const { rows } = await pool.query(
    `SELECT learner_key, count(*) AS sessions FROM runtime_sessions
      WHERE learner_key <> $1
      GROUP BY learner_key ORDER BY 2 DESC`,
    [MAIN_LEARNER],
  );
  const strays = rows.map((r) => r.learner_key);
  console.log(`pinned learner: ${MAIN_LEARNER}`);
  console.log(`stray partitions: ${strays.length}`);
  for (const r of rows) console.log(`  ${r.learner_key} -> ${r.sessions} sessions`);
  if (strays.length === 0) {
    console.log('nothing to merge.');
  } else {
    await pool.query(`CREATE TABLE IF NOT EXISTS zoo477_backup_runtime_sessions AS
      SELECT * FROM runtime_sessions WHERE false`);
    await pool.query(
      `INSERT INTO zoo477_backup_runtime_sessions
         SELECT s.* FROM runtime_sessions s
          WHERE s.learner_key = ANY($1) AND NOT EXISTS (
            SELECT 1 FROM zoo477_backup_runtime_sessions b
             WHERE b.id = s.id AND b.learner_key = s.learner_key)`,
      [strays],
    );

    const { PgRuntimeStore, ensureSchema } = await import('@openmaic/storage/runtime/pg');
    const { nodePostgresTransaction } = await import('@openmaic/storage/server/reference');
    const queryable = pool;
    await ensureSchema(queryable);
    const store = new PgRuntimeStore(queryable, {
      withTransaction: nodePostgresTransaction(pool),
    });
    for (const stray of strays) {
      const moved = await store.mergeLearner(stray, MAIN_LEARNER);
      console.log(`merged ${stray} -> ${MAIN_LEARNER}: ${moved} sessions`);
    }
  }
  const learners = await pool.query(
    `SELECT learner_key, count(*) FROM runtime_sessions GROUP BY learner_key ORDER BY 2 DESC`,
  );
  console.log('runtime learners after migration:');
  for (const r of learners.rows) console.log(`  ${r.learner_key} -> ${r.count}`);
} finally {
  await pool.end();
}
