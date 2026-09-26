import csv
import math
from datetime import date, timedelta
from pathlib import Path

SCAFFOLD_DIR = Path(__file__).parent

REGIONS = [("us", 1.0), ("eu", 0.7), ("apac", 0.45)]
CATEGORIES = [
    ("electronics", 220.0),
    ("apparel", 85.0),
    ("home", 140.0),
    ("outdoors", 110.0),
    ("beauty", 45.0),
]


def _pseudo(n: int) -> float:
    return (math.sin(n * 12.9898) * 43758.5453) % 1.0


def generate_orders(days: int = 120) -> list[tuple[str, str, str, float]]:
    rows = []
    today = date.today()
    counter = 0
    for day_offset in range(days, 0, -1):
        day = today - timedelta(days=day_offset)
        weekday_boost = 1.25 if day.weekday() < 5 else 0.8
        growth = 1.0 + (days - day_offset) / days * 0.6
        for region, region_weight in REGIONS:
            for category, base_price in CATEGORIES:
                counter += 1
                n_orders = max(
                    0, round(3 * region_weight * weekday_boost * growth * (0.6 + _pseudo(counter)))
                )
                for _i in range(n_orders):
                    counter += 1
                    amount = round(base_price * (0.5 + 1.2 * _pseudo(counter)), 2)
                    rows.append((day.isoformat(), region, category, amount))
    return rows


class ScaffoldExists(FileExistsError):
    """A scaffold file is already there and would have been overwritten."""


class ScaffoldError(OSError):
    """The target path cannot hold a `.sqldash/` directory."""


def _sqldash_dir(target: Path) -> Path:
    dest = target if target.name == ".sqldash" else target / ".sqldash"
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise ScaffoldError(f"{target} is not writable") from exc
    except OSError as exc:
        bad = dest if dest.exists() and not dest.is_dir() else target
        raise ScaffoldError(f"{bad} is not a directory") from exc
    return dest


def init_project(target: Path) -> Path:
    """Create `.sqldash/` and nothing else. Demo content is `create_demo`."""
    return _sqldash_dir(target)


def create_demo(target: Path, force: bool = False) -> Path:
    """Scaffold a demo project. Refuses to clobber existing scaffold files
    unless `force` — these are the user's dashboards once they edit them."""
    target = _sqldash_dir(target)
    if not force:
        scaffolded = (
            target / "demo.yaml",
            target / "metrics.yaml",
            target / "agents.yaml",
            target / "data" / "orders.csv",
        )
        existing = [p for p in scaffolded if p.exists()]
        if existing:
            names = ", ".join(p.name for p in existing)
            raise ScaffoldExists(
                f"{target} already has {names} — refusing to overwrite. "
                "Use --force to replace them, or init a different directory."
            )
    data_dir = target / "data"
    data_dir.mkdir(exist_ok=True)
    with (data_dir / "orders.csv").open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["order_date", "region", "category", "amount"])
        writer.writerows(generate_orders())
    demo_path = target / "demo.yaml"
    demo_path.write_text((SCAFFOLD_DIR / "demo.yaml").read_text())
    (target / "metrics.yaml").write_text((SCAFFOLD_DIR / "metrics.yaml").read_text())
    (target / "agents.yaml").write_text((SCAFFOLD_DIR / "agents.yaml").read_text())
    return demo_path
