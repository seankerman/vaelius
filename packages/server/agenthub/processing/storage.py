"""PostgreSQL query helpers. Schema ownership belongs to migrations."""
import re




def table_exists(db, name):
    return bool(db.execute("SELECT to_regclass(?)", (name,)).fetchone()[0])


def columns(db, table):
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", table):
        raise ValueError("invalid_storage_identifier")
    return [r[0] for r in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=? ORDER BY ordinal_position", (table,))]


def begin_write(db):
    if not db.in_transaction:
        db.execute("BEGIN")


def json_value(db, column, path):
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.]*", column) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.]*", path):
        raise ValueError("invalid_json_path")
    return "(" + column + "::jsonb #>> '{" + path.replace(".", ",") + "}')"


def fulltext_predicate(db, table="knowledge_fts"):
    return "to_tsvector('simple'," + table + ".body) @@ websearch_to_tsquery('simple',?)"


def fulltext_rank(db, table="knowledge_fts"):
    # PostgreSQL's positive score becomes a descending priority represented as
    # a negative rank so canonical selectors keep their existing ascending sort.
    return "-ts_rank_cd(to_tsvector('simple'," + table + ".body),websearch_to_tsquery('simple',?))"


def lexical_query(db, words):
    return " OR ".join('"' + word.replace('"', '""') + '"' for word in words)


def create_fulltext(db, table):
    if table not in {"knowledge_fts_staging", "knowledge_fts"}:
        raise ValueError("invalid_fulltext_table")
    db.execute("CREATE TABLE IF NOT EXISTS " + table + "(document_id TEXT NOT NULL,revision_id TEXT NOT NULL,body TEXT NOT NULL)")
