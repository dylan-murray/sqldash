from sqldash.models.results import QueryResult, ResultColumn
from sqldash.period import compare_window, delta_from_results, shift_iso


def test_yoy_on_a_leap_day_start_keeps_the_prior_feb_28():
    window = compare_window("yoy", "2024-02-29", "2024-03-31")
    assert window["start"] == "2023-02-28"
    assert window["end"] == "2023-03-31"


def test_yoy_on_a_non_leap_start_is_a_straight_year_back():
    window = compare_window("yoy", "2024-03-01", "2024-03-31")
    assert window["start"] == "2023-03-01"
    assert window["end"] == "2023-03-31"


def test_previous_period_uses_the_day_count():
    window = compare_window("previous_period", "2024-02-29", "2024-03-31")
    assert window["start"] == "2024-01-28"
    assert window["end"] == "2024-02-28"


def test_shift_iso_clamps_feb_29():
    assert shift_iso("2024-02-29", years=1) == "2023-02-28"


def _result(columns, rows):
    return QueryResult(
        columns=[ResultColumn(name=n, type=t) for n, t in columns],
        rows=rows,
        row_count=len(rows),
    )


def test_delta_skips_a_numeric_dimension_column():
    current = _result([("order_year", "integer"), ("revenue", "float")], [[2026, 100.0]])
    previous = _result([("order_year", "integer"), ("revenue", "float")], [[2026, 80.0]])
    assert delta_from_results(current, previous) is None
    assert delta_from_results(current, previous, dimensions=["order_year"]) is None


def test_delta_skips_grained_or_multi_row_results():
    current = _result([("revenue", "float")], [[100.0], [110.0]])
    previous = _result([("revenue", "float")], [[80.0], [90.0]])
    assert delta_from_results(current, previous) is None
    one = _result([("revenue", "float")], [[100.0]])
    other = _result([("revenue", "float")], [[80.0]])
    assert delta_from_results(one, other, grain="month") is None


def test_delta_skips_a_non_finite_value():
    """A NaN value now reaches the rows as "NaN"; float() reads it back and the
    delta was NaN, which `-f json` refuses to write."""
    for cur, prev in [("NaN", 80.0), (100.0, "Infinity"), ("-Infinity", 80.0)]:
        current = _result([("revenue", "float")], [[cur]])
        previous = _result([("revenue", "float")], [[prev]])
        assert delta_from_results(current, previous) is None


def test_delta_on_a_single_measure_row():
    current = _result([("revenue", "float")], [[100.0]])
    previous = _result([("revenue", "float")], [[80.0]])
    delta = delta_from_results(current, previous)
    assert delta == {"current": 100.0, "previous": 80.0, "pct": 0.25}
