"""Keyword classifier for SQL a caller typed, and for authored tile SQL openers.

Shared by MCP ``run_sql``, the ad-hoc ``sql`` path of ``POST /api/run``, and
the named ``query:`` path that dashboard tiles run on load. It is a keyword
check, not a parser, and not a security boundary: the credential's own grants
are the real guardrail.

Which means it says nothing about what a permitted read can reach. A duckdb
source has no credential and no grants — its reach is the server user's
filesystem — so that bound is set on the connection instead, by confining it to
the project directory (``connectors/engine.py::_confine``, #622).
"""

import re

from sqldash.sqltext import STRING, blank, noise_spans

_SQL_TEMPLATE = re.compile(r"{%.*?%}|{{.*?}}", re.DOTALL)
_SQL_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
READ_STATEMENTS = frozenset(
    {
        "select",
        "with",
        "from",
        "values",
        "table",
        "show",
        "describe",
        "desc",
        "explain",
        "summarize",
        "pivot",
        "unpivot",
    }
)
WRITE_WORDS = frozenset(
    {
        "insert",
        "update",
        "delete",
        "merge",
        "upsert",
        "copy",
        "create",
        "drop",
        "alter",
        "truncate",
        "into",
        "attach",
        "detach",
        "export",
        "import",
        "set",
        "pragma",
        "call",
        "install",
        "load",
        "checkpoint",
    }
)
SESSION_OPENERS = frozenset({"use", "unset", "reset", "discard"})
WRITE_OPENERS = frozenset(
    {
        "replace",
        "vacuum",
        "reindex",
        "analyze",
        "analyse",
        "optimize",
        "force",
        "refresh",
        "cluster",
        "grant",
        "revoke",
        "deny",
        "comment",
        "rename",
        "undrop",
        "put",
        "get",
        "remove",
        "rm",
        "execute",
        "exec",
        "do",
        "kill",
        "flush",
        "purge",
        "repair",
        "msck",
        "restore",
        "backup",
        "exchange",
        "lock",
        "system",
        "shutdown",
        "dbcc",
        "unload",
    }
)
# WITH can carry DELETE/INSERT in its body; DuckDB EXPLAIN ANALYZE executes
# the statement it explains. Authored tiles skip the full body scan so
# `SELECT k AS copy` still runs, but these two openers still have to.
_NESTING_READ_OPENERS = frozenset({"explain", "with"})
_STATE = "changes logging or profiling for every later query on the pooled connection"
_POINTER = "dereferences a raw memory address"
_SMUGGLED = "runs SQL from a string the guard cannot check"
SIDE_EFFECT_FUNCTIONS = {
    "enable_logging": _STATE,
    "disable_logging": _STATE,
    "truncate_duckdb_logs": "deletes the DuckDB log",
    "write_log": "writes to the DuckDB log",
    "enable_profiling": _STATE,
    "disable_profiling": _STATE,
    "checkpoint": "writes the database file",
    "force_checkpoint": "writes the database file",
    "nextval": "advances a sequence",
    "setseed": "reseeds random() for every later query on the pooled connection",
    "arrow_scan": _POINTER,
    "arrow_scan_dumb": _POINTER,
    "pandas_scan": _POINTER,
    "python_map_function": _POINTER,
    "query": _SMUGGLED,
    "json_execute_serialized_sql": _SMUGGLED,
}
SNOWFLAKE_SYSTEM_READS = frozenset(
    {
        "system$allowlist",
        "system$allowlist_privatelink",
        "system$behavior_change_bundle_status",
        "system$client_version_info",
        "system$clustering_depth",
        "system$clustering_information",
        "system$clustering_ratio",
        "system$current_user_task_name",
        "system$estimate_automatic_clustering_costs",
        "system$estimate_query_acceleration",
        "system$estimate_search_optimization_costs",
        "system$explain_json_to_text",
        "system$explain_plan_json",
        "system$external_table_pipe_status",
        "system$get_compute_pool_status",
        "system$get_directory_table_status",
        "system$get_predecessor_return_value",
        "system$get_service_status",
        "system$get_tag",
        "system$get_tag_allowed_values",
        "system$get_tag_on_current_column",
        "system$get_tag_on_current_table",
        "system$get_task_graph_config",
        "system$last_change_commit_time",
        "system$pipe_status",
        "system$show_active_behavior_change_bundles",
        "system$stage_pipe_status",
        "system$stream_get_table_timestamp",
        "system$stream_has_data",
        "system$tag_value_contains_on_current_column",
        "system$tag_value_contains_on_current_table",
        "system$task_runtime_info",
        "system$typeof",
    }
)
SNOWFLAKE_SYSTEM_EFFECTS = {
    "system$abort_session": "ends any session of the source's user",
    "system$abort_transaction": "aborts a transaction in any session of the source's user",
    "system$cancel_all_queries": "cancels every query in any session of the source's user",
    "system$cancel_query": "cancels a query from any session of the source's user",
    "system$user_task_cancel_ongoing_executions": "cancels running task executions",
    "system$wait": "holds the pooled connection and the warehouse for as long as it asks",
}
_UNREVIEWED_SYSTEM = "is not a Snowflake system function known to be a read"
_INDIRECT_NAME = re.compile(r"(?<![\w$])identifier\s*\(", re.IGNORECASE)
_CALLED_NAME = re.compile(r"(?<![\w$])([A-Za-z_][A-Za-z0-9_$]*)\s*\(")
_OPENS_CALL = re.compile(r"\s*\(")
_COLUMN_LIST = re.compile(r"\s*\(\s*[A-Za-z_]\w*(\s*,\s*[A-Za-z_]\w*)*\s*\)")
_ALIAS_SHAPED = frozenset({"query"})


def read_only_violation(
    sql: str, surface: str = "run_sql", *, scan_body: bool = True
) -> str | None:
    """Why ``sql`` is not one plain read, or None when it looks like one.

    Comments and quoted literals are blanked first (``sqltext.blank`` — the same
    scanner ``params`` reads comment spans from), so a ``;`` or a writing
    keyword inside a string does not count. Then: exactly one statement, the
    first word must open a read, and — when ``scan_body`` is true — no writing
    keyword may appear after it (a WITH or EXPLAIN can carry INSERT/COPY/CREATE
    in its body; DuckDB EXPLAIN ANALYZE executes SET/PRAGMA/CALL/INSTALL/LOAD/
    CHECKPOINT; Postgres SELECT INTO creates a table). REPLACE is not on the
    list because REPLACE() is an everyday read function; REPLACE INTO is caught
    by INTO and, on the tile path, as one of the ``WRITE_OPENERS``: statements
    that write (VACUUM, FORCE CHECKPOINT, GRANT, Snowflake GET/PUT, Postgres DO)
    but whose first word is too common a column name to scan the body for. A call to a
    function in ``SIDE_EFFECT_FUNCTIONS``, or a Snowflake ``SYSTEM$`` function
    not in ``SNOWFLAKE_SYSTEM_READS``, is refused on every path, quoted or
    schema-qualified; other engine functions pass through. ``surface`` names the caller in
    the verdict so an agent or the query editor can read which path refused it.

    Authored tile SQL uses ``scan_body=False``: a DELETE/COPY/VACUUM opener is a
    write, a USE/UNSET/RESET/DISCARD opener would change the pooled session
    for every later caller, but ``SELECT k AS copy`` is a read and an unknown
    opener (``SELEKT``) is left for the warehouse probe. An EXPLAIN is refused on
    either path when the statement it wraps opens with a write opener. ``EXPLAIN`` and ``WITH``
    still scan the body — DuckDB ``EXPLAIN ANALYZE DELETE`` executes the
    delete (#500 review). Template tags are blanked on that path so
    ``{% if %}DELETE{% endif %}`` still classifies as a write.
    """
    text = _SQL_TEMPLATE.sub(" ", sql) if not scan_body else sql
    stripped = blank(text).strip().rstrip(";")
    if ";" in stripped:
        return f"{surface} accepts exactly one statement"
    words = [w.lower() for w in _SQL_WORD.findall(stripped)]
    if not words:
        return f"{surface} needs a statement"
    for name in _called_functions(text):
        reason = _side_effect(name)
        if reason is not None:
            return f"{surface} is read-only; {name}() is refused because it {reason}"
    if _calls_through_identifier(text):
        return (
            f"{surface} is read-only; IDENTIFIER(...)() is refused because it names "
            "the called function in a string the guard cannot check"
        )
    if words[0] not in READ_STATEMENTS:
        if scan_body:
            return f"{surface} is read-only; {words[0].upper()} statements are refused"
        if words[0] in WRITE_WORDS or words[0] in WRITE_OPENERS:
            return f"{surface} is a write statement"
        if words[0] in SESSION_OPENERS:
            return (
                f"{surface} changes session state that later queries on the pooled "
                f"connection would inherit; {words[0].upper()} statements are refused"
            )
        return None
    explained = _explained_opener(words)
    if explained in WRITE_OPENERS:
        if scan_body:
            return f"{surface} is read-only; a statement containing {explained.upper()} is refused"
        return f"{surface} is a write statement"
    if scan_body or words[0] in _NESTING_READ_OPENERS:
        for word in words[1:]:
            if word in WRITE_WORDS:
                if scan_body:
                    return (
                        f"{surface} is read-only; a statement containing {word.upper()} is refused"
                    )
                return f"{surface} is a write statement"
    return None


def _side_effect(name: str) -> str | None:
    """What calling ``name`` does beyond reading, or None for a read.

    Snowflake's ``SYSTEM$`` functions are refused unless listed as reads: several
    reach other sessions of the credential's user (``SYSTEM$ABORT_SESSION`` ends
    one), ``SHOW FUNCTIONS`` does not enumerate all of them, and Snowflake adds
    more, so an unreviewed one is refused rather than let through.
    """
    if name in SIDE_EFFECT_FUNCTIONS:
        return SIDE_EFFECT_FUNCTIONS[name]
    if name.startswith("system$") and name not in SNOWFLAKE_SYSTEM_READS:
        return SNOWFLAKE_SYSTEM_EFFECTS.get(name, _UNREVIEWED_SYSTEM)
    return None


def _calls_through_identifier(sql: str) -> bool:
    """Whether a Snowflake ``IDENTIFIER(...)`` is itself called, as in
    ``IDENTIFIER('SYSTEM$ABORT_SESSION')(1)``. The name sits in a literal the
    guard blanks, so the call is refused whatever it names; ``FROM
    IDENTIFIER('orders')`` is a table reference and still runs."""
    code = blank(sql)
    for match in _INDIRECT_NAME.finditer(code):
        depth, index = 1, match.end()
        while index < len(code) and depth:
            depth += {"(": 1, ")": -1}.get(code[index], 0)
            index += 1
        if depth == 0 and _OPENS_CALL.match(code, index):
            return True
    return False


def _explained_opener(words: list[str]) -> str | None:
    """The first word of the statement an EXPLAIN wraps, past its options.

    DuckDB ``EXPLAIN ANALYZE VACUUM`` runs the vacuum, and ``ANALYZE`` is both
    an EXPLAIN option and a write opener, so it is skipped here; Postgres
    ``EXPLAIN (ANALYZE, FORMAT JSON) SELECT`` reaches ``select``.
    """
    if words[0] != "explain":
        return None
    for word in words[1:]:
        if word != "analyze" and (
            word in READ_STATEMENTS or word in WRITE_WORDS or word in WRITE_OPENERS
        ):
            return word
    return None


def _called_functions(sql: str) -> list[str]:
    """Lowercased names written directly before a `(`, bare or double-quoted.

    DuckDB resolves `"Enable_Logging"()` to the same function, and the scanner
    reports a double-quoted identifier as a string span, so blanking alone hides it.
    A name in ``_ALIAS_SHAPED`` followed by a bare identifier list is a column
    alias (`FROM (...) query(a)`, `WITH query(a) AS`), not a call: a real
    `query()` takes a string, which blanking empties.
    """
    code = blank(sql)
    called = [(m.group(1).lower(), m.end(1)) for m in _CALLED_NAME.finditer(code)]
    for start, end, kind in noise_spans(sql):
        if kind == STRING and sql[start] == '"' and _OPENS_CALL.match(code, end):
            called.append((sql[start + 1 : end - 1].replace('""', '"').lower(), end))
    return [
        name
        for name, after in called
        if not (name in _ALIAS_SHAPED and _COLUMN_LIST.match(code, after))
    ]
