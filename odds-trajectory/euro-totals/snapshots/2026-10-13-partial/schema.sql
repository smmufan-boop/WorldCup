-- Frozen euro/totals data; no worker, triggers, or live dispatch.
CREATE TABLE matches(match_id TEXT PRIMARY KEY,kickoff REAL,priority INTEGER NOT NULL,metadata_json TEXT NOT NULL);

CREATE TABLE tasks(id INTEGER PRIMARY KEY,match_id TEXT NOT NULL,market TEXT NOT NULL,source_cid TEXT NOT NULL,company TEXT NOT NULL,
 kickoff REAL,priority INTEGER NOT NULL,state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,next_retry REAL NOT NULL DEFAULT 0,
 last_error TEXT,latest_attempt INTEGER,raw_rows INTEGER NOT NULL DEFAULT 0,valid_rows INTEGER NOT NULL DEFAULT 0,t10_rows INTEGER NOT NULL DEFAULT 0,
 t10_strict_rows INTEGER NOT NULL DEFAULT 0,summary_json TEXT,updated_at TEXT,
 UNIQUE(match_id,market,source_cid),FOREIGN KEY(match_id) REFERENCES matches(match_id));

CREATE TABLE attempts(task_id INTEGER NOT NULL,attempt_no INTEGER NOT NULL,requested_at TEXT NOT NULL,response_at TEXT,
 source_url TEXT NOT NULL,http_status INTEGER,final_url TEXT,content_type TEXT,raw_path TEXT,raw_sha256 TEXT,body_bytes INTEGER,body_truncated INTEGER NOT NULL DEFAULT 0,
 elapsed_seconds REAL,status TEXT NOT NULL,reason TEXT,PRIMARY KEY(task_id,attempt_no),FOREIGN KEY(task_id) REFERENCES tasks(id));

CREATE TABLE quotes(task_id INTEGER NOT NULL,source_order INTEGER NOT NULL,changed_at REAL,source_time TEXT,source_score TEXT,
 home_decimal REAL,draw_decimal REAL,away_decimal REAL,over_hk REAL,total_line REAL,under_hk REAL,
 quote_valid INTEGER NOT NULL,prematch INTEGER NOT NULL,t10_eligible INTEGER NOT NULL,t10_strict_eligible INTEGER NOT NULL,
 t10_cutoff_minute_ambiguous INTEGER NOT NULL,same_minute_conflict INTEGER NOT NULL,consecutive_duplicate INTEGER NOT NULL,
 invalid_reason TEXT,home_source TEXT,draw_source TEXT,away_source TEXT,
 PRIMARY KEY(task_id,source_order),FOREIGN KEY(task_id) REFERENCES tasks(id)) WITHOUT ROWID;

CREATE TABLE meta(key TEXT PRIMARY KEY,value_json TEXT NOT NULL);

CREATE INDEX dispatch_ready ON tasks(state,priority,kickoff DESC,id);

CREATE INDEX dispatch_retry ON tasks(state,next_retry);
