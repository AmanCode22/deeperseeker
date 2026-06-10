CREATE TABLE IF NOT EXISTS api_key_metadata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    parent_message_id INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS id_api_key ON api_key_metadata(api_key);
CREATE INDEX IF NOT EXISTS id_session_id ON api_key_metadata(session_id);
CREATE INDEX IF NOT EXISTS id_parent_message_id ON api_key_metadata(parent_message_id);
CREATE UNIQUE INDEX IF NOT EXISTS id_api_key_session ON api_key_metadata(api_key, session_id);
