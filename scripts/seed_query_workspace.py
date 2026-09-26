"""Create a disposable, labelled two-dashboard query-workspace review fixture."""

import argparse
from copy import deepcopy
from pathlib import Path

import duckdb
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import LiteralScalarString

from sqldash.project.query_library import LibraryQuery, QueryLibrary
from sqldash.scaffold import create_demo


def seed(target: Path) -> None:
    if (target / ".sqldash").exists():
        raise SystemExit(
            "Choose a new directory; this fixture does not overwrite an existing project."
        )
    create_demo(target)
    root = target / ".sqldash"
    csv = next(target.rglob("orders.csv"))
    with duckdb.connect(str(root / "shop.duckdb")) as conn:
        conn.execute(
            "CREATE TABLE orders AS SELECT row_number() OVER () AS order_id, * "
            "FROM read_csv_auto(?)",
            [str(csv)],
        )
        conn.execute("CREATE TABLE order_items AS SELECT order_id, category, amount FROM orders")
        conn.execute(
            "CREATE TABLE products AS SELECT row_number() OVER () AS product_id, category "
            "FROM orders GROUP BY category"
        )
        conn.execute(
            "CREATE TABLE customers AS SELECT row_number() OVER () AS customer_id, region, "
            "count(*) AS order_count FROM orders GROUP BY region"
        )
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 100
    path = root / "demo.yaml"
    doc = yaml.load(path)
    doc["description"] = "Synthetic SQL-client review fixture; generated orders and dimensions."
    doc["source"] = {"type": "duckdb", "database": "shop.duckdb", "attach_files": False}
    sql = (
        "SELECT region, ROUND(SUM(amount), 2) AS revenue\n"
        "FROM orders\nGROUP BY region\nORDER BY revenue DESC\n"
    )
    doc["queries"] = {"regional_revenue": LiteralScalarString(sql)}
    with path.open("w") as stream:
        yaml.dump(doc, stream)
    other = deepcopy(doc)
    other["title"] = "Executive review"
    other["tiles"] = []
    with (root / "executive.yaml").open("w") as stream:
        yaml.dump(other, stream)
    QueryLibrary(root).save(
        LibraryQuery(
            id="revenue_by_region", title="Revenue by region", sql=sql, source="demo.source"
        ),
        "*",
    )
    print(f"Review fixture ready: {target}")
    print("customers is a synthetic region aggregate, not a production customer dimension.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    seed(parser.parse_args().directory)
