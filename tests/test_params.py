import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from sqldash.models.dashboard import Dashboard
from sqldash.params import (
    ParamError,
    bind_sql,
    extract_params,
    filter_params,
    param_values,
    resolve_date_token,
    resolve_daterange_preset,
    resolve_relative_start,
)


def test_extract_params_dedupes_in_order():
    sql = "SELECT * FROM t WHERE a >= {{ start }} AND ({{ region }} = 'all' OR r = {{ region }})"
    assert extract_params(sql) == ["start", "region"]


def test_extract_params_includes_if_only_names():
    sql = "SELECT count(*) AS c FROM t {% if mode %}WHERE x = 1{% endif %}"
    assert extract_params(sql) == ["mode"]


PARAM_CORPUS = json.loads((Path(__file__).parent / "param_corpus.json").read_text())["cases"]


@pytest.mark.parametrize("case", PARAM_CORPUS, ids=[repr(c["sql"]) for c in PARAM_CORPUS])
def test_extract_params_matches_the_shared_corpus(case):
    """`paramNamesIn` in static/js/params.js reads the same file (tests/test_param_names.mjs).
    A name one side collects and the other does not is #671 or #683 again."""
    assert extract_params(case["sql"]) == case["names"]


def test_bind_qmark():
    sql, bind = bind_sql("SELECT {{ a }} + {{ b }}", {"a": 1, "b": 2}, "qmark")
    assert sql == "SELECT ? + ?"
    assert bind == [1, 2]


def test_bind_repeated_param_binds_each_occurrence():
    sql, bind = bind_sql("SELECT {{ a }}, {{ a }}", {"a": "x"}, "pyformat")
    assert sql == "SELECT %s, %s"
    assert bind == ["x", "x"]


def test_bind_list_expands():
    sql, bind = bind_sql("WHERE r IN {{ regions }}", {"regions": ["us", "eu"]}, "qmark")
    assert sql == "WHERE r IN (?, ?)"
    assert bind == ["us", "eu"]


def test_bind_missing_raises():
    with pytest.raises(ParamError, match="missing value for parameter 'a'"):
        bind_sql("SELECT {{ a }}", {}, "qmark")


def test_no_string_interpolation():
    sql, bind = bind_sql("SELECT {{ v }}", {"v": "'; DROP TABLE t; --"}, "qmark")
    assert sql == "SELECT ?"
    assert bind == ["'; DROP TABLE t; --"]


def test_render_conditionals_active_and_inactive():
    from sqldash.params import render_conditionals

    sql = "SELECT * FROM t WHERE 1=1 {% if region %}AND region = {{ region }}{% endif %}"
    assert "AND region" in render_conditionals(sql, {"region": "us"})
    assert "AND region" not in render_conditionals(sql, {})
    assert "AND region" not in render_conditionals(sql, {"region": ""})
    assert "AND region" not in render_conditionals(
        sql, {"region": "all"}, inactive=frozenset({"region"})
    )


def test_render_conditionals_rejects_nesting():
    from sqldash.params import render_conditionals

    with pytest.raises(ParamError, match="nested"):
        render_conditionals("{% if a %}x {% if b %}y{% endif %}{% endif %}", {"a": 1, "b": 1})


def test_render_conditionals_rejects_unknown_tags():
    from sqldash.params import render_conditionals

    with pytest.raises(ParamError, match="unsupported"):
        render_conditionals("{% for x in y %}nope{% endfor %}", {})


CHAIN = (
    "{% if region %}SELECT {{ region }} AS v"
    "{% elif tier %}SELECT {{ tier }} AS v"
    "{% else %}SELECT {{ fallback }} AS v{% endif %}"
)


def test_render_conditionals_picks_the_else_branch():
    from sqldash.params import render_conditionals

    sql = "{% if region %}SELECT 1 AS x{% else %}SELECT 2 AS x{% endif %}"
    assert render_conditionals(sql, {"region": "eu"}) == "SELECT 1 AS x"
    assert render_conditionals(sql, {}) == "SELECT 2 AS x"
    assert render_conditionals(sql, {"region": ""}) == "SELECT 2 AS x"
    assert render_conditionals(sql, {"region": "all"}, frozenset({"region"})) == "SELECT 2 AS x"


def test_render_conditionals_takes_the_first_active_branch():
    from sqldash.params import render_conditionals

    values = {"region": "eu", "tier": "gold", "fallback": "none"}
    assert "{{ region }}" in render_conditionals(CHAIN, values)
    assert "{{ tier }}" in render_conditionals(CHAIN, {"tier": "gold", "fallback": "none"})
    assert "{{ fallback }}" in render_conditionals(CHAIN, {"fallback": "none"})


@pytest.mark.parametrize("branch", ["region", "tier", "fallback"])
def test_every_branch_binds_its_param_rather_than_interpolating(branch):
    """A branch is a span of author SQL that rendering keeps or drops; the
    values reach `bind_sql` untouched, exactly as a bare {% if %} body does."""
    from sqldash.params import render_conditionals

    hostile = "'; DROP TABLE t; --"
    rendered = render_conditionals(CHAIN, {branch: hostile})
    bound, bind = bind_sql(rendered, {branch: hostile}, "qmark")
    assert bound == "SELECT ? AS v"
    assert bind == [hostile]
    assert hostile not in bound


def test_extract_params_includes_condition_only_elif_names():
    sql = "SELECT 1 {% if region %}WHERE a = 1{% elif mode %}WHERE b = 2{% endif %}"
    assert extract_params(sql) == ["region", "mode"]
    assert extract_params(CHAIN) == ["region", "tier", "fallback"]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "{% if a %}x {% if b %}y{% endif %}{% endif %}",
            "nested {% if b %} inside {% if a %} is not supported",
        ),
        ("{% else %}x", "{% else %} has no {% if param %} to belong to"),
        ("{% endif %}x", "{% endif %} has no {% if param %} to belong to"),
        (
            "{% if a %}x{% else %}y{% elif b %}z{% endif %}",
            "{% elif b %} comes after the {% else %} of {% if a %}",
        ),
        ("{% if a %}x", "{% if a %} is never closed"),
        ("{% for x in y %}z{% endfor %}", "unsupported template tag '{% for x in y %}'"),
    ],
)
def test_block_errors_name_the_offending_tag(sql, expected):
    """#657: every one of these used to be 'nested template tags inside
    {% if a %}' or 'unsupported template tag', blaming a tag the author wrote
    correctly."""
    from sqldash.params import render_conditionals

    with pytest.raises(ParamError) as exc:
        render_conditionals(sql, {"a": 1, "b": 1})
    assert expected in str(exc.value)


COMMENTED_OUT = [
    ("line comment", "-- {{ region }}\nSELECT 1 AS x"),
    ("trailing line comment", "SELECT 1 AS x -- keep {{ region }} here"),
    ("line comment at end of text", "SELECT 1 AS x\n-- {{ region }}"),
    ("block comment", "/* {{ region }} */ SELECT 1 AS x"),
    (
        "block comment spanning lines",
        "/* filter by {{ region }}\n   once the column lands\n   {% if region %}x{% endif %} */\n"
        "SELECT 1 AS x",
    ),
    (
        "commented-out filter line",
        "SELECT 1 AS x\n-- {% if region %}AND r = {{ region }}{% endif %}",
    ),
    ("quote inside the comment", "-- don't bind {{ region }} here\nSELECT 1 AS x"),
]


@pytest.mark.parametrize(("label", "sql"), COMMENTED_OUT, ids=[c[0] for c in COMMENTED_OUT])
def test_a_tag_inside_a_comment_is_not_a_param(label, sql):
    """#659: the warehouse drops the comment, so binding a value for a
    placeholder in it left the query one argument over."""
    from sqldash.params import render_conditionals

    assert extract_params(sql) == []
    assert render_conditionals(sql, {}) == sql
    bound, bind = bind_sql(sql, {}, "qmark")
    assert bound == sql
    assert bind == []


def test_a_comment_marker_inside_a_string_literal_is_not_a_comment():
    sql = "SELECT 'a -- b' AS note, {{ region }} AS r"
    assert extract_params(sql) == ["region"]
    bound, bind = bind_sql(sql, {"region": "eu"}, "qmark")
    assert bound == "SELECT 'a -- b' AS note, ? AS r"
    assert bind == ["eu"]


def test_a_block_comment_marker_inside_a_string_literal_is_not_a_comment():
    sql = "SELECT 'a /* b' AS note, {{ region }} AS r, 'c */ d' AS tail"
    assert extract_params(sql) == ["region"]
    bound, bind = bind_sql(sql, {"region": "eu"}, "qmark")
    assert bound == "SELECT 'a /* b' AS note, ? AS r, 'c */ d' AS tail"
    assert bind == ["eu"]


def test_an_escaped_quote_does_not_end_the_literal():
    sql = "SELECT 'it''s -- fine' AS note, {{ region }} AS r"
    bound, bind = bind_sql(sql, {"region": "eu"}, "qmark")
    assert bound == "SELECT 'it''s -- fine' AS note, ? AS r"
    assert bind == ["eu"]


def test_a_kept_branch_keeps_its_comment_verbatim():
    from sqldash.params import render_conditionals

    sql = "SELECT 1 {% if region %}-- keeping {{ region }}\nAND r = {{ region }}{% endif %}"
    rendered = render_conditionals(sql, {"region": "eu"})
    assert rendered == "SELECT 1 -- keeping {{ region }}\nAND r = {{ region }}"
    bound, bind = bind_sql(rendered, {"region": "eu"}, "qmark")
    assert bound == "SELECT 1 -- keeping {{ region }}\nAND r = ?"
    assert bind == ["eu"]


def test_a_comment_inside_an_active_elif_branch_binds_only_what_is_code():
    """#657 and #659 together: the {% elif %} branch is chosen, and the
    placeholder in the comment it carries is still not a param."""
    from sqldash.params import render_conditionals

    sql = (
        "{% if region %}SELECT {{ region }} AS v"
        "{% elif tier %}-- not {{ region }}\nSELECT {{ tier }} AS v"
        "{% else %}SELECT 0 AS v{% endif %}"
    )
    assert extract_params(sql) == ["region", "tier"]
    rendered = render_conditionals(sql, {"tier": "gold"})
    assert rendered == "-- not {{ region }}\nSELECT {{ tier }} AS v"
    bound, bind = bind_sql(rendered, {"tier": "gold"}, "qmark")
    assert bound == "-- not {{ region }}\nSELECT ? AS v"
    assert bind == ["gold"]


def test_an_else_inside_a_block_comment_is_not_a_branch():
    """A commented-out {% else %} is prose, so the {% if %} still closes
    cleanly and the else body never becomes a branch."""
    from sqldash.params import render_conditionals

    sql = "{% if region %}SELECT 1 AS x/* {% else %} */{% endif %}"
    assert render_conditionals(sql, {"region": "eu"}) == "SELECT 1 AS x/* {% else %} */"
    assert render_conditionals(sql, {}) == ""


def test_an_unreadable_tag_inside_a_comment_is_not_an_error():
    from sqldash.params import render_conditionals, validate_template

    sql = "SELECT 1 AS x -- todo {{ region.foo }} and {{}} and {% for x in y %}"
    assert validate_template(sql) is None
    assert render_conditionals(sql, {}) == sql
    assert bind_sql(sql, {}, "qmark") == (sql, [])


UNREADABLE = [
    ("empty tag", "SELECT {{}} AS x", "{{}}"),
    ("dotted path", "SELECT {{ region.foo }} AS x", "{{ region.foo }}"),
    ("nested-looking tag", "SELECT {{ {{ region }} }} AS x", "{{ {{ region }} }}"),
    ("whitespace only", "SELECT {{   }} AS x", "{{   }}"),
]


@pytest.mark.parametrize(("label", "sql", "tag"), UNREADABLE, ids=[c[0] for c in UNREADABLE])
def test_an_unreadable_placeholder_is_refused_by_name(label, sql, tag):
    """#660: these reached the warehouse as literal text and came back as a
    parser error pointing at a brace."""
    from sqldash.params import render_conditionals, validate_template

    with pytest.raises(ParamError) as excinfo:
        bind_sql(sql, {"region": "eu"}, "qmark")
    assert repr(tag) in str(excinfo.value)
    with pytest.raises(ParamError, match="unsupported template tag"):
        render_conditionals(sql, {"region": "eu"})
    assert "unsupported template tag" in (validate_template(sql) or "")


def test_an_unreadable_placeholder_in_a_dropped_branch_is_still_refused():
    """lint renders with no values, so a tag only reachable from a branch it
    drops still has to be named."""
    from sqldash.params import validate_template

    problem = validate_template("{% if region %}SELECT {{ region.foo }}{% endif %}")
    assert problem is not None
    assert "'{{ region.foo }}'" in problem


def test_braces_the_author_meant_as_data_are_left_alone():
    sql = "SELECT '{{}}' AS braces, '{{ region.foo }}' AS path, -- {{}}\n{{ region }} AS r"
    bound, bind = bind_sql(sql, {"region": "eu"}, "qmark")
    assert bound == "SELECT '{{}}' AS braces, '{{ region.foo }}' AS path, -- {{}}\n? AS r"
    assert bind == ["eu"]


QUOTED_PLACEHOLDER = [
    ("single-quoted literal", "SELECT count(*) AS n FROM t WHERE c = '{{ region }}'"),
    ("escaped quote in the same literal", "SELECT * FROM t WHERE c = 'it''s {{ region }}'"),
    ("quoted identifier", 'SELECT 1 AS "{{ region }}"'),
    ("literal spanning lines", "SELECT 'a\n{{ region }}\nb' AS note FROM t"),
    ("wrapped in a percent for LIKE", "SELECT * FROM t WHERE c LIKE '%{{ region }}%'"),
    ("dollar-quoted literal", "SELECT $$ {{ region }} $$ AS x"),
    ("tagged dollar-quoted literal", "SELECT $q$ {{ region }} $q$ AS x"),
    ("after an escaped quote in an escape string", "SELECT E'\\' {{ region }}' AS x"),
]


@pytest.mark.parametrize(
    ("label", "sql"), QUOTED_PLACEHOLDER, ids=[c[0] for c in QUOTED_PLACEHOLDER]
)
def test_a_placeholder_inside_a_string_literal_is_refused_by_name(label, sql):
    """#678: substitution put the bind marker inside the quotes, so the value
    travelled with a statement that had nothing to consume it and DuckDB answered
    'Parameter argument/count mismatch' naming nothing the author wrote."""
    from sqldash.params import validate_template

    with pytest.raises(ParamError, match="parameter 'region' is inside a string literal"):
        bind_sql(sql, {"region": "eu"}, "qmark")
    assert "inside a string literal" in (validate_template(sql) or "")


def test_a_quoted_placeholder_is_refused_beside_a_real_one_in_code():
    """The one in code is a parameter and the one in quotes is the mistake, so the
    error names the quotes rather than binding one value and interpolating the other."""
    sql = "SELECT * FROM t WHERE c = '{{ region }}' OR r = {{ region }}"
    with pytest.raises(ParamError, match="inside a string literal"):
        bind_sql(sql, {"region": "eu"}, "qmark")
    ok = "SELECT * FROM t WHERE c = {{ region }} OR r = {{ region }}"
    assert bind_sql(ok, {"region": "eu"}, "qmark") == (
        "SELECT * FROM t WHERE c = ? OR r = ?",
        ["eu", "eu"],
    )


def test_a_quoted_placeholder_inside_a_comment_is_still_prose():
    """A comment wins over the quotes inside it: the warehouse drops the whole line,
    so there is no marker to misplace and nothing to refuse."""
    from sqldash.params import validate_template

    sql = "SELECT 1 AS x\n-- WHERE c = '{{ region }}'"
    assert validate_template(sql) is None
    assert extract_params(sql) == []
    assert bind_sql(sql, {}, "qmark") == (sql, [])


def test_a_quoted_placeholder_in_a_dropped_branch_is_still_refused():
    """lint renders with no values, so the shape has to be named even in a branch
    the render drops — the same reach the unreadable-tag refusal has."""
    from sqldash.params import validate_template

    problem = validate_template("{% if region %}SELECT '{{ region }}'{% endif %}")
    assert problem is not None
    assert "parameter 'region' is inside a string literal" in problem


def test_a_quoted_placeholder_in_a_branch_not_taken_does_not_break_the_page():
    """The other half of the split. Refusing the quoted placeholder while rendering
    broke a dashboard whose `{% else %}` ran fine with its filter off: the branch
    holding the mistake never reaches the warehouse. Runtime refuses it only when
    that branch is the one about to run; lint (above) names it either way."""
    from sqldash.params import render_conditionals

    sql = "{% if region %}SELECT 1 WHERE c = '{{ region }}'{% else %}SELECT 2 AS x{% endif %}"
    assert bind_sql(render_conditionals(sql, {}), {}, "qmark") == ("SELECT 2 AS x", [])
    with pytest.raises(ParamError, match="parameter 'region' is inside a string literal"):
        bind_sql(render_conditionals(sql, {"region": "eu"}), {"region": "eu"}, "qmark")


def test_an_unclosed_placeholder_is_refused_without_quoting_the_whole_query():
    sql = "SELECT {{ x " + "y" * 200
    with pytest.raises(ParamError) as excinfo:
        bind_sql(sql, {}, "qmark")
    assert repr("{{ x " + "y" * 35) in str(excinfo.value)


def _select_dash(options, default="all"):
    from sqldash.models.dashboard import Dashboard

    filt: dict = {"name": "region", "type": "select", "options": options}
    if default is not None:
        filt["default"] = default
    return Dashboard.model_validate(
        {
            "title": "T",
            "source": {"type": "duckdb", "database": ":memory:"},
            "filters": [filt],
            "queries": {"q": "SELECT 1 AS n {% if region %}WHERE r = {{ region }}{% endif %}"},
            "tiles": [],
        }
    )


def test_select_all_is_off_even_when_not_listed():
    """#216: default: all with options: [us, eu] bound region='all' and returned 0 rows."""
    from sqldash.params import inactive_params, prepare_sql, select_choices

    dash = _select_dash(["us", "eu"])
    assert inactive_params(dash, {"region": "all"}) == frozenset({"region"})
    rendered, values, missing = prepare_sql(dash, dash.queries["q"], {})
    assert "WHERE" not in rendered
    assert missing == []
    assert "region" not in values
    assert select_choices(dash.filters[0]) == ["all", "us", "eu"]


def test_select_without_default_is_off_like_cli():
    """#236: no default meant the browser sat on the first option (us) while
    CLI/API bound nothing and returned every region."""
    from sqldash.params import filter_ui_default, prepare_sql, select_choices

    dash = _select_dash(["us", "eu", "apac"], default=None)
    assert select_choices(dash.filters[0]) == ["all", "us", "eu", "apac"]
    assert filter_ui_default(dash.filters[0]) == "all"
    rendered, values, missing = prepare_sql(dash, dash.queries["q"], {})
    assert "WHERE" not in rendered
    assert "region" not in values
    assert missing == []


def test_select_all_listed_still_off():
    from sqldash.params import prepare_sql

    dash = _select_dash(["all", "us", "eu"])
    rendered, values, _ = prepare_sql(dash, dash.queries["q"], {})
    assert "WHERE" not in rendered
    assert "region" not in values


def test_if_only_filter_activates():
    """#217: a param used only in {% if %} was never resolved, so the block
    always dropped — overrides and a non-all default were ignored."""
    from sqldash.models.dashboard import Dashboard
    from sqldash.params import prepare_sql

    dash = Dashboard.model_validate(
        {
            "title": "T",
            "source": {"type": "duckdb", "database": ":memory:"},
            "filters": [
                {"name": "mode", "type": "select", "options": ["all", "strict"], "default": "all"}
            ],
            "queries": {
                "q": "SELECT count(*) AS c FROM (VALUES (1),(2),(3)) t(x) "
                "{% if mode %}WHERE x = 1{% endif %}"
            },
            "tiles": [],
        }
    )
    off, _, _ = prepare_sql(dash, dash.queries["q"], {})
    assert "WHERE" not in off
    on, _, _ = prepare_sql(dash, dash.queries["q"], {"mode": "strict"})
    assert "WHERE x = 1" in on


def test_options_sql_select_includes_authored_default():
    """The browser sat on `all` until options_sql returned, so tiles ran unfiltered. #300."""
    from sqldash.models.dashboard import Dashboard
    from sqldash.params import filter_ui_default, select_choices

    dash = Dashboard.model_validate(
        {
            "title": "T",
            "source": {"type": "duckdb", "database": ":memory:"},
            "filters": [
                {
                    "name": "region",
                    "type": "select",
                    "options_sql": "SELECT DISTINCT region FROM orders",
                    "default": "us",
                }
            ],
            "queries": {"q": "SELECT 1"},
            "tiles": [],
        }
    )
    assert filter_ui_default(dash.filters[0]) == "us"
    assert "us" in select_choices(dash.filters[0])


def test_select_real_option_stays_on():
    from sqldash.params import prepare_sql

    dash = _select_dash(["us", "eu"])
    rendered, values, _ = prepare_sql(dash, dash.queries["q"], {"region": "us"})
    assert "WHERE r = {{ region }}" in rendered
    assert values["region"] == "us"


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-09-01", "2026-01-01"),
        ("2026-1-2", "2026-1-1"),
        ("2026-01-01T10:00", "2026-01-01"),
        ("2026-01-01 10:00:00", "2026-01-01 09:59:59.999"),
        ("2026-01-02T00:00:01", "2026-01-01T24:00:00"),
        ("2026-01-01T12:00:00+00:00", "2026-01-01T12:00:00+05:00"),
    ],
)
def test_check_date_window_refuses_an_inverted_window(start, end):
    """#361: both bounds bound in the order given, so `>= later AND <= earlier`
    was a confident empty result on every surface and nothing said why."""
    from sqldash.params import check_date_window

    with pytest.raises(ParamError, match="inverted") as excinfo:
        check_date_window(start, end)
    assert start in str(excinfo.value)
    assert end in str(excinfo.value)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-01-01", "2026-01-01"),
        ("2026-01-01", "2026-09-01"),
        ("2026-1-1", "2026-01-01T00:00:00"),
        ("2026-01-01T00:00:00", "2026-01-01T24:00:00"),
        ("2026-01-02T00:00:00", "2026-01-01T24:00:00"),
        ("2026-01-01T12:00:00+05:00", "2026-01-01T12:00:00Z"),
        ("", "2026-01-01"),
        ("2026-09-01", ""),
        (None, "2026-01-01"),
        ("2026-09-01", None),
        ("0000-01-01", "2026-01-01"),
        ("9999-12-31T24:00:00", "9999-12-31"),
        ("0001-01-01T00:00:00+05:00", "0001-01-01"),
    ],
)
def test_check_date_window_keeps_ordered_open_and_one_day_windows(start, end):
    from sqldash.params import check_date_window

    check_date_window(start, end)


def test_check_date_window_names_the_params_and_the_tokens_they_came_from():
    from sqldash.params import check_date_window

    with pytest.raises(ParamError) as excinfo:
        check_date_window(
            "2026-09-02",
            "2026-01-01",
            start_name="dates_start",
            end_name="dates_end",
            given=("today", "2026-01-01"),
        )
    message = str(excinfo.value)
    assert "dates_start '2026-09-02' is after dates_end '2026-01-01'" in message
    assert "(from 'today' and '2026-01-01')" in message


def _daterange_dash():
    from sqldash.models.dashboard import Dashboard

    return Dashboard.model_validate(
        {
            "title": "T",
            "source": {"type": "duckdb", "database": ":memory:"},
            "filters": [{"name": "dates", "type": "daterange", "default": "last_30_days"}],
            "queries": {"q": "SELECT 1 WHERE d BETWEEN {{ dates_start }} AND {{ dates_end }}"},
            "tiles": [],
        }
    )


def test_param_values_refuses_an_inverted_daterange_after_resolving_tokens():
    from sqldash.params import param_values, prepare_sql

    dash = _daterange_dash()
    names = ["dates_start", "dates_end"]
    with pytest.raises(ParamError, match=r"dates_start .* is after dates_end") as excinfo:
        param_values(dash, names, {"dates_start": "today", "dates_end": "2026-01-01"})
    assert "(from 'today' and '2026-01-01')" in str(excinfo.value)
    with pytest.raises(ParamError, match="inverted"):
        prepare_sql(
            dash, dash.queries["q"], {"dates_start": "2026-09-01", "dates_end": "2026-01-01"}
        )
    values, missing = param_values(
        dash, names, {"dates_start": "2026-01-01", "dates_end": "2026-01-01"}
    )
    assert not missing
    assert values["dates_start"] == values["dates_end"] == "2026-01-01"
    values, _ = param_values(dash, names, {})
    assert values["dates_start"] < values["dates_end"]


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (20260101, "'20260101'"),
        (20260101.0, "'20260101'"),
        (True, "True"),
        (["2026-01-01"], "['2026-01-01']"),
        ({"start": "2026-01-01"}, "{'start': '2026-01-01'}"),
    ],
)
def test_param_values_names_a_non_string_date_override(value, shown):
    """A number bound as INTEGER and failed in the warehouse; the string got a named error. #584."""
    from sqldash.params import param_values

    dash = _daterange_dash()
    with pytest.raises(ParamError, match="unrecognized date") as excinfo:
        param_values(dash, ["dates_start", "dates_end"], {"dates_start": value})
    assert shown in str(excinfo.value)


def test_param_values_accepts_a_date_object_override():
    from datetime import date

    from sqldash.params import param_values

    values, _ = param_values(
        _daterange_dash(),
        ["dates_start", "dates_end"],
        {"dates_start": date(2026, 1, 1), "dates_end": "2026-02-01"},
    )
    assert values["dates_start"] == "2026-01-01"


def test_filter_params_follows_an_authored_bind_and_param_values_agrees():
    dashboard = Dashboard.model_validate(
        {
            "title": "a",
            "source": {"type": "duckdb"},
            "tiles": [],
            "filters": [
                {
                    "name": "dates",
                    "type": "daterange",
                    "default": {"start": "2026-01-01", "end": "2026-02-01"},
                    "bind": {"start": "from_d", "end": "to_d"},
                },
                {"name": "window", "type": "daterange", "default": "last_7_days"},
                {"name": "region", "type": "text", "default": "us", "bind": {"value": "reg"}},
            ],
        }
    )
    assert set(filter_params(dashboard)) == {
        "from_d",
        "to_d",
        "window_start",
        "window_end",
        "region",
    }
    values, missing = param_values(dashboard, ["from_d", "to_d", "region", "reg"], {})
    assert values == {"from_d": "2026-01-01", "to_d": "2026-02-01", "region": "us"}
    assert missing == ["reg"]


CORPUS = json.loads((Path(__file__).parent / "daterange_presets.json").read_text())["cases"]


@pytest.mark.parametrize("case", CORPUS, ids=[f"{c['token']}@{c['today']}" for c in CORPUS])
def test_daterange_presets_match_the_shared_corpus(case):
    """`presetRange` in static/js/period.js reads the same file (tests/test_period.mjs).
    A token that means two windows is the #673 bug in a different disguise."""
    resolved = resolve_daterange_preset(case["token"], date.fromisoformat(case["today"]))
    if case["preset"] is None:
        assert resolved is None
        return
    assert resolved == {"preset": case["preset"], "start": case["start"], "end": case["end"]}


def test_a_preset_with_no_day_given_resolves_against_the_server_date():
    today = date.today()
    assert resolve_daterange_preset("last_30_days") == {
        "preset": "last_30_days",
        "start": (today - timedelta(days=30)).isoformat(),
        "end": today.isoformat(),
    }


def _number_dashboard():
    return Dashboard.model_validate(
        {
            "title": "T",
            "source": {"type": "duckdb", "database": ":memory:"},
            "filters": [{"name": "account", "type": "number"}],
            "queries": {"q": "SELECT {{ account }} AS v"},
            "tiles": [],
        }
    )


@pytest.mark.parametrize(
    ("given", "bound"),
    [
        ("9007199254740993", 9007199254740993),
        ("-9007199254740993", -9007199254740993),
        (9007199254740993, 9007199254740993),
        ("12.5", 12.5),
        ("12.0", 12),
        (" 7 ", 7),
    ],
)
def test_number_param_binds_exactly(given, bound):
    """float() rounded -p account=9007199254740993 to ...992, a different account."""
    values, _ = param_values(_number_dashboard(), ["account"], {"account": given})
    assert values["account"] == bound
    assert type(values["account"]) is type(bound)


@pytest.mark.parametrize("given", ["nan", "-inf", "inf", "Infinity", "1e400", float("nan"), "abc"])
def test_number_param_refuses_non_finite(given):
    """`>= nan` matched nothing and `>= -inf` everything, with no error anywhere."""
    with pytest.raises(ParamError, match="'account' must be a finite number"):
        param_values(_number_dashboard(), ["account"], {"account": given})


@pytest.mark.parametrize("style", ["pyformat", "format"])
def test_percent_style_doubles_literal_percent_once_a_value_is_bound(style):
    """psycopg, pymysql and the Snowflake connector read every % once params are
    passed, so LIKE 'A%' failed as soon as a filter was set."""
    sql = (
        "SELECT '100%' AS p -- 5% off\nFROM t WHERE name LIKE 'A%' AND r IN {{ r }} AND x = {{ x }}"
    )
    bound, bind = bind_sql(sql, {"r": ["us", "eu"], "x": 1}, style)
    assert bound == (
        "SELECT '100%%' AS p -- 5%% off\nFROM t WHERE name LIKE 'A%%' AND r IN (%s, %s) AND x = %s"
    )
    assert bind == ["us", "eu", 1]
    assert bound % tuple(f"<{v}>" for v in bind) == (
        "SELECT '100%' AS p -- 5% off\n"
        "FROM t WHERE name LIKE 'A%' AND r IN (<us>, <eu>) AND x = <1>"
    )


@pytest.mark.parametrize("style", ["pyformat", "format"])
def test_percent_style_leaves_percent_alone_with_nothing_bound(style):
    """With no params the driver never interpolates, so doubling would change the text."""
    sql = "SELECT '100%' AS p FROM t WHERE name LIKE 'A%' AND r IN {{ r }}"
    assert bind_sql(sql, {"r": []}, style) == (
        "SELECT '100%' AS p FROM t WHERE name LIKE 'A%' AND r IN ()",
        [],
    )


@pytest.mark.parametrize("style", ["qmark", "numeric", "numeric_dollar", "named"])
def test_non_percent_styles_keep_literal_percent(style):
    bound, _ = bind_sql("SELECT 1 FROM t WHERE name LIKE 'A%' AND x = {{ x }}", {"x": 1}, style)
    assert "LIKE 'A%' AND" in bound


@pytest.mark.parametrize(
    "token", ["-99999999d", "-2739000y", "last_99999999999_days", "-9999999999999999999999w"]
)
def test_relative_token_past_year_one_is_an_unrecognized_date(token):
    """timedelta and date subtraction raised OverflowError out of every caller."""
    assert resolve_relative_start(token) is None
    assert resolve_daterange_preset(token) is None
    with pytest.raises(ParamError, match=r"unrecognized date .* relative token: -30d"):
        resolve_date_token(token)


def test_relative_token_reaching_exactly_year_one_still_resolves():
    today = date(2026, 9, 23)
    days = (today - date.min).days
    assert resolve_relative_start(f"-{days}d", today) == (date.min, f"last_{days}_days")
    assert resolve_relative_start(f"-{days + 1}d", today) is None
