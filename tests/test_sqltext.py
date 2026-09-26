import duckdb
import pytest

from sqldash.params import bind_sql
from sqldash.sqlguard import read_only_violation
from sqldash.sqltext import COMMENT, STRING, blank, noise_spans

DOLLAR_QUOTED = [
    ("plain", "SELECT $$ x $$ AS v", "$$ x $$"),
    ("tagged", "SELECT $tag$ x $tag$ AS v", "$tag$ x $tag$"),
    ("tagged around a plain one", "SELECT $a$ x $$ y $a$ AS v", "$a$ x $$ y $a$"),
    ("first exact closer wins", "SELECT $a$ x $b$a$ AS v", "$a$ x $b$a$"),
    ("tag right against its body", "SELECT $a$b$a$ AS v", "$a$b$a$"),
    ("underscore and digits in the tag", "SELECT $_t1$ x $_t1$ AS v", "$_t1$ x $_t1$"),
    ("non-ascii tag", "SELECT $é$ x $é$ AS v", "$é$ x $é$"),
    ("quote and comment markers inside", "SELECT $$ it's -- /* $$ AS v", "$$ it's -- /* $$"),
    ("spanning lines", "SELECT $$\na\nb\n$$ AS v", "$$\na\nb\n$$"),
    ("after a positional parameter", "SELECT $1$$ x $$", "$$ x $$"),
    ("after a number", "SELECT 1$$ x $$", "$$ x $$"),
]


@pytest.mark.parametrize(
    ("label", "sql", "literal"), DOLLAR_QUOTED, ids=[c[0] for c in DOLLAR_QUOTED]
)
def test_a_dollar_quote_is_one_string_span(label, sql, literal):
    """#695: the scanner knew only `'` and `"`, so a dollar-quoted body was code to
    every consumer, and the span matches where DuckDB's lexer ends the literal."""
    start = sql.index(literal)
    assert (start, start + len(literal), STRING) in noise_spans(sql)


NOT_DOLLAR_QUOTED = [
    ("positional parameters", "SELECT $1, $2 FROM t"),
    ("digit-led tag is a parameter", "SELECT $1$ x $1$"),
    ("dollar inside an identifier", "SELECT a$$b, c$$d FROM t"),
    ("identifier ending in a dollar", "SELECT a$ FROM t WHERE $b"),
    ("lone dollar", "SELECT $ FROM t WHERE $"),
    ("unterminated", "SELECT $$ x FROM t"),
    ("unterminated tag", "SELECT $tag$ x FROM t"),
    ("tags are case-sensitive", "SELECT $A$ x $a$ FROM t"),
    ("inside a single-quoted literal", "SELECT ' $$ ' FROM t, ' $$ ' AS y"),
    ("inside a comment", "SELECT 1 -- $$\n, $$"),
]


@pytest.mark.parametrize(("label", "sql"), NOT_DOLLAR_QUOTED, ids=[c[0] for c in NOT_DOLLAR_QUOTED])
def test_a_dollar_that_does_not_open_a_quote_leaves_the_code_visible(label, sql):
    assert not any(sql[start] == "$" for start, _, _ in noise_spans(sql))


def test_a_dollar_quote_ends_where_duckdb_ends_it():
    """`$$a$$b$$` is the literal `a` aliased `b$$`, not one literal running to the
    last `$$`: the word after a closer carries its own dollars."""
    sql = "SELECT $$a$$b$$"
    assert blank(sql) == "SELECT      b$$"
    assert duckdb.connect().execute(sql).description[0][0] == "b$$"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT $$ it's $$; DELETE FROM t; --'",
        "SELECT $$ -- $$; DELETE FROM t",
        "SELECT $$ /* $$; DELETE FROM t; SELECT $$ */ $$",
    ],
)
def test_a_quote_or_comment_marker_in_a_dollar_quote_cannot_hide_a_second_statement(sql):
    """Before #695 the `'` or `--` inside the dollar quote opened a span of its own
    and swallowed the `;`, so this passed the guard and DuckDB ran the DELETE."""
    assert read_only_violation(sql) == "run_sql accepts exactly one statement"
    assert len(duckdb.connect().extract_statements(sql)) > 1


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT $1, $2; DELETE FROM t",
        "SELECT $1$ ; DELETE FROM t $1$",
        "SELECT a$$b ; DELETE FROM t; SELECT c$$d",
        "SELECT $ ; DELETE FROM t ; SELECT $",
        "SELECT $$ ; DELETE FROM t",
        "SELECT $A$ x $a$; DELETE FROM t",
    ],
)
def test_a_dollar_that_is_not_a_quote_does_not_hide_a_write(sql):
    assert read_only_violation(sql) == "run_sql accepts exactly one statement"


def test_a_writing_word_inside_a_real_dollar_quote_is_data():
    assert read_only_violation("SELECT $$ DELETE FROM t $$ AS note") is None
    assert read_only_violation("SELECT $a$ ; DROP TABLE t $a$ AS note") is None
    assert read_only_violation("WITH x AS (SELECT $$ s $$ AS s) DELETE FROM t") is not None


def test_a_placeholder_beside_a_dollar_identifier_still_binds():
    assert bind_sql(
        "SELECT a$$b, $1 AS p, {{ region }} AS r FROM t", {"region": "eu"}, "qmark"
    ) == (
        "SELECT a$$b, $1 AS p, ? AS r FROM t",
        ["eu"],
    )


ESCAPE_STRINGS = [
    ("escaped quote", r"SELECT E'a\'b' AS v", r"E'a\'b'"),
    ("lowercase prefix", r"SELECT e'a\'b' AS v", r"e'a\'b'"),
    ("escaped backslash before the closer", r"SELECT E'a\\' AS v", r"E'a\\'"),
    ("doubled quote still escapes", "SELECT E'a''b' AS v", "E'a''b'"),
    ("backslash then escaped quote", r"SELECT E'\\\'' AS v", r"E'\\\''"),
    ("comment and semicolon markers inside", r"SELECT E'\' ; -- /* ' AS v", r"E'\' ; -- /* '"),
    ("after an operator", r"SELECT 'x' || E'\'' AS v", r"E'\''"),
    ("after a qualifier dot", r"SELECT a.e'\'' AS v", r"e'\''"),
    ("after a positional parameter", r"SELECT $1e'\'' AS v", r"e'\''"),
    ("after a number", r"SELECT 1e'\'' AS v", r"e'\''"),
    ("after a decimal", r"SELECT 1.5E'\'' AS v", r"E'\''"),
]


@pytest.mark.parametrize(
    ("label", "sql", "literal"), ESCAPE_STRINGS, ids=[c[0] for c in ESCAPE_STRINGS]
)
def test_an_escape_string_is_one_string_span(label, sql, literal):
    """#701: a backslash-escaped quote does not end an `E'...'` literal to DuckDB,
    but ended it to the scanner, so the two disagreed about what was code."""
    start = sql.index(literal)
    assert (start, start + len(literal), STRING) in noise_spans(sql)
    assert blank(sql)[start : start + len(literal)].strip() == ""


@pytest.mark.parametrize(
    "gap",
    ["\n", "\r", "\n\n  ", " \t\n\f", " -- note\n", "\n-- note\n"],
    ids=["newline", "carriage return", "blank line", "mixed", "trailing comment", "own-line"],
)
def test_a_literal_continuing_an_escape_string_keeps_the_backslash_rule(gap):
    """`'...'` after a newline continues the literal before it, in the same mode."""
    sql = f"SELECT E'a'{gap}'\\'' AS v"
    second = sql.rindex("'\\''")
    assert (second, second + 4, STRING) in noise_spans(sql)
    assert blank(sql).rstrip().endswith("AS v")
    assert len(duckdb.connect().extract_statements(sql)) == 1


NOT_ESCAPE_STRINGS = [
    ("identifier ending in e", "SELECT name'\\' AS v"),
    ("underscore then e", "SELECT _e'\\' AS v"),
    ("non-ascii identifier ending in e", "SELECT ée'\\' AS v"),
    ("dollar identifier ending in e", "SELECT a$e'\\' AS v"),
    ("exponent number", "SELECT 1e5'\\' AS v"),
    ("space after the prefix", "SELECT E '\\' AS v"),
    ("quoted identifier e", "SELECT \"e\"'\\' AS v"),
    ("plain literal", "SELECT '\\' AS v"),
    ("hex literal", "SELECT X'\\' AS v"),
    ("continuation needs a newline", "SELECT E'a' '\\' AS v"),
    ("block comment breaks a continuation", "SELECT E'a' /* c */\n'\\' AS v"),
]


@pytest.mark.parametrize(
    ("label", "sql"), NOT_ESCAPE_STRINGS, ids=[c[0] for c in NOT_ESCAPE_STRINGS]
)
def test_a_backslash_outside_an_escape_string_does_not_escape(label, sql):
    literal = sql.rindex("'\\'")
    assert (literal, literal + 3, STRING) in noise_spans(sql)


@pytest.mark.parametrize(
    "sql",
    ["SELECT E'x\\' AS v", "SELECT E'a'\n'\\' AS v", "SELECT E'x\\"],
    ids=["unterminated", "unterminated continuation", "trailing backslash"],
)
def test_an_unterminated_escape_string_is_not_a_span(sql):
    assert not any(sql[start] in "eE" for start, _, _ in noise_spans(sql))


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT E'\\'' AS x; DELETE FROM t; SELECT '1'",
        "SELECT e'\\'' AS x; DELETE FROM t; SELECT '1'",
        "SELECT E'\\\\\\'' ; DELETE FROM t ; SELECT '1'",
        "SELECT E'x' AS a, E'\\'' AS b; DELETE FROM t; SELECT '1'",
        "SELECT E'a'\n'\\'' ; DELETE FROM t ; SELECT '1'",
        "SELECT E'a' -- c\n'\\'' ; DELETE FROM t ; SELECT '1'",
    ],
)
def test_an_escaped_quote_cannot_hide_a_second_statement(sql):
    """Before #701 the scanner closed the literal at `\\'` and opened a new one at
    its real closer, which swallowed the `;` and the statement after it."""
    assert read_only_violation(sql) == "run_sql accepts exactly one statement"
    assert len(duckdb.connect().extract_statements(sql)) == 3


def test_a_writing_word_inside_a_real_escape_string_is_data():
    sql = "SELECT E'\\' ; DELETE FROM t ; --' AS note"
    assert len(duckdb.connect().extract_statements(sql)) == 1
    assert read_only_violation(sql) is None
    assert read_only_violation("WITH x AS (SELECT E'\\'' AS s) DELETE FROM t") is not None


def test_a_line_comment_ends_at_a_carriage_return_as_duckdb_does():
    """DuckDB ends `--` at `\\r` as well as `\\n`. Ending it only at `\\n` treated
    code after a bare carriage return as comment, hiding it from the guard. #703."""
    sql = "SELECT 1 --a\rSELECT 2"
    assert blank(sql) == "SELECT 1    \rSELECT 2"
    assert COMMENT in {kind for _, _, kind in noise_spans(sql)}


def test_a_nested_block_comment_is_one_comment_as_duckdb_reads_it():
    """DuckDB nests `/* */`, so the comment runs to the matching close, not the
    first `*/`. #703."""
    sql = "SELECT /* a /* b */ c */ 3"
    assert blank(sql) == "SELECT " + " " * len("/* a /* b */ c */") + " 3"


def test_an_unterminated_nested_block_comment_is_not_a_span():
    """Same rule as every other unterminated form: SQL the warehouse rejects
    anyway, and treating it as running to the end would hide the rest."""
    sql = "SELECT /* a /* b */ 8"
    assert all(start != sql.index("/*") for start, _, _ in noise_spans(sql))
    assert blank(sql).endswith(" 8")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'x' -- y\r; SELECT 2",
        "SELECT /* a /* b */ ' */ 6; SELECT 7; --'",
    ],
    ids=["cr-ended line comment", "quote inside a nested comment"],
)
def test_the_guard_sees_the_second_statement_duckdb_sees(sql):
    """Each is two statements to DuckDB. Main's comment rules hid the `;` and the
    second SELECT from the guard, which then let the text through as one. #703."""
    assert len(duckdb.extract_statements(sql)) == 2
    assert read_only_violation(sql) is not None
