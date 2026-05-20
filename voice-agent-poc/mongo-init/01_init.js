// mongo-init/01_init.js
// Runs automatically on first container start
// Creates collections, indexes, and a default config document

db = db.getSiblingDB("voice_agent");

// ── Collections ──────────────────────────────────────────────
db.createCollection("sessions");
db.createCollection("turns");

// ── Indexes ──────────────────────────────────────────────────

// sessions: look up by session_id and user_id
db.sessions.createIndex({ session_id: 1 }, { unique: true });
db.sessions.createIndex({ user_id: 1 });
db.sessions.createIndex({ created_at: -1 });

// turns: look up by session, time, and language
db.turns.createIndex({ session_id: 1, timestamp: -1 });
db.turns.createIndex({ user_id: 1 });
db.turns.createIndex({ language: 1 });

// TTL index: auto-delete turns older than 90 days
// Remove this index if you want to keep all history
db.turns.createIndex(
    { timestamp: 1 },
    { expireAfterSeconds: 7776000 }   // 90 days
);

print("[mongo-init] Collections and indexes created successfully");
