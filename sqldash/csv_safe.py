"""Spreadsheet-safe CSV cells, shared by every Python CSV writer.

The server export (api/routes_executions.py) and `sqldash query --format csv`
both write files people open in a spreadsheet, so both quote the same cells.
`static/js/csv-safe.js` is the browser's copy; tests/spreadsheet_safe.json holds
all three to one answer.
"""

import re
from typing import Any

FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")
PLAIN_NUMBER = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def spreadsheet_safe(value: Any) -> Any:
    """A cell a spreadsheet will not run as a formula when the file is opened.

    Excel, Sheets and LibreOffice evaluate a cell that starts with any of
    FORMULA_TRIGGERS, so `=HYPERLINK(...)` from the warehouse became a live
    link. A leading quote makes them show the text instead. A plain number is
    left alone because it is read as a number, not a formula; that includes
    decimals, which reach the rows as strings (`-3.50`) to keep their precision.
    """
    if not isinstance(value, str) or not value.startswith(FORMULA_TRIGGERS):
        return value
    if PLAIN_NUMBER.fullmatch(value):
        return value
    return "'" + value
