-- Trajectory snapshot: tables and source indices only.
CREATE TABLE matches ("match_id" TEXT NOT NULL DEFAULT '',"cmp_type" TEXT NOT NULL DEFAULT '',"competition_id" TEXT NOT NULL DEFAULT '',"competition_name" TEXT NOT NULL DEFAULT '',"team_A_id" TEXT NOT NULL DEFAULT '',"team_A_name" TEXT NOT NULL DEFAULT '',"team_B_id" TEXT NOT NULL DEFAULT '',"team_B_name" TEXT NOT NULL DEFAULT '',"kickoff_utc" TEXT NOT NULL DEFAULT '',"kickoff_local" TEXT NOT NULL DEFAULT '',"status" TEXT NOT NULL DEFAULT '',"minute" TEXT NOT NULL DEFAULT '',"score_home" TEXT NOT NULL DEFAULT '',"score_away" TEXT NOT NULL DEFAULT '',"fetched_at_utc" TEXT NOT NULL DEFAULT '', "period" TEXT NOT NULL DEFAULT '', "score_note" TEXT NOT NULL DEFAULT '', "team_A_logo" TEXT NOT NULL DEFAULT '', "team_B_logo" TEXT NOT NULL DEFAULT '', PRIMARY KEY (match_id));

CREATE TABLE odds_history (dedupe_key TEXT PRIMARY KEY,"match_id" TEXT NOT NULL DEFAULT '',"cmp_type" TEXT NOT NULL DEFAULT '',"competition_name" TEXT NOT NULL DEFAULT '',"home_team" TEXT NOT NULL DEFAULT '',"away_team" TEXT NOT NULL DEFAULT '',"kickoff_utc" TEXT NOT NULL DEFAULT '',"kickoff_local" TEXT NOT NULL DEFAULT '',"company_id" TEXT NOT NULL DEFAULT '',"company_name" TEXT NOT NULL DEFAULT '',"company_abbr" TEXT NOT NULL DEFAULT '',"market_type" TEXT NOT NULL DEFAULT '',"source_time" TEXT NOT NULL DEFAULT '',"change_time_local" TEXT NOT NULL DEFAULT '',"home_water" TEXT NOT NULL DEFAULT '',"handicap_text" TEXT NOT NULL DEFAULT '',"handicap_value" TEXT NOT NULL DEFAULT '',"away_water" TEXT NOT NULL DEFAULT '',"score" TEXT NOT NULL DEFAULT '',"fetched_at_utc" TEXT NOT NULL DEFAULT '',"source_url" TEXT NOT NULL DEFAULT '');

CREATE TABLE odds_minute_order (
        match_id TEXT NOT NULL, company_id TEXT NOT NULL, market_type TEXT NOT NULL,
        change_time_local TEXT NOT NULL, sequence_hash TEXT NOT NULL,
        sequence_json TEXT NOT NULL, source_url TEXT NOT NULL, fetched_at_utc TEXT NOT NULL,
        PRIMARY KEY(match_id,company_id,market_type,change_time_local,sequence_hash));

CREATE TABLE match_score_aliases(
        alias_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, identity_json TEXT NOT NULL,
        first_verified_utc TEXT NOT NULL, last_verified_utc TEXT NOT NULL,
        original_json TEXT NOT NULL, source_json TEXT NOT NULL, basis TEXT NOT NULL DEFAULT 'exact-sport-league-team-ids-kickoff',
        CHECK(alias_id<>source_id));

CREATE INDEX idx_matches_kickoff ON matches(kickoff_local, match_id);

CREATE INDEX idx_odds_match_company_time ON odds_history(match_id, company_id, change_time_local, source_time);

CREATE INDEX similarity_fixture_schedule ON matches(cmp_type,status,kickoff_local,match_id);

CREATE INDEX similarity_odds_company_match ON odds_history(company_id,market_type,match_id);
