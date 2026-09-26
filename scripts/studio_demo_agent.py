"""The scripted "Demo agent" behind the README walkthrough.

A deterministic stand-in for a coding agent: it reads the Studio prompt from
argv, edits revenue.yaml in the working directory, and narrates what it did.
Three requests are understood: section headings, a dark glassy look (the
Neon observatory example), and a bright acid-lime look (Electric citrus).
"""

import sys
import time
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

HEADINGS = [
    (0, "Sales at a glance", "Revenue, orders, customers and basket size for the selected period."),
    (
        3,
        "Where revenue comes from",
        "The trajectory against the previous period, then the split by category, channel "
        "and product.",
    ),
    (10, "Rhythm of the week", "Order volume by weekday, to spot the quiet days."),
]


PLANS = {
    "citrus": "Brighter and punchier: acid lime on black with angular cards.",
    "quartz": "Softer: rose and lavender on paper.",
    "terminal": "Black glass and amber phosphor, monospace throughout.",
    "broadsheet": "Cream paper and black ink: serif headlines, thin rules, square corners.",
    "ember": "Sunset warmth: coral and orange on deep plum, revenue glowing.",
}
LOOKS = {
    "citrus": "Acid lime on black, angular cards, offset shadows, revenue in the lime block.",
    "quartz": "Rose and lavender on paper, rounded cards, a serif title.",
    "terminal": "Black and amber, monospace numbers, a prompt before the title.",
    "broadsheet": "Cream newsprint, serif headlines, a double rule under the masthead.",
    "ember": "Coral and orange on deep plum, revenue lit like coals.",
}


def flow(**fields):
    mapping = CommentedMap(list(fields.items()))
    mapping.fa.set_flow_style()
    return mapping


def add_heading(doc, y, text, body):
    for tile in doc["tiles"]:
        if "position" in tile and tile["position"].get("y", 0) >= y:
            tile["position"]["y"] += 1
    index = next(
        (i for i, t in enumerate(doc["tiles"]) if t.get("position", {}).get("y", 0) > y),
        len(doc["tiles"]),
    )
    tile = CommentedMap(
        [("markdown", f"## {text}\n{body}"), ("position", flow(x=0, y=y, w=12, h=1))]
    )
    doc["tiles"].insert(index, tile)


def say(text):
    print(text, flush=True)


def main():
    prompt = sys.argv[1].lower()

    def wants(*words):
        return any(word in prompt for word in words)

    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096
    path = Path("revenue.yaml")
    if not path.exists():
        path = Path(".sqldash/revenue.yaml")
    doc = yaml.load(path.read_text())

    if wants("heading", "header", "section"):
        say("Reading revenue.yaml to see where the sections fall.")
        time.sleep(1.4)
        say("Adding three heading tiles and moving the rows down to make room.")
        time.sleep(1.2)
        for y, text, body in HEADINGS:
            add_heading(doc, y, text, body)
        with path.open("w") as stream:
            yaml.dump(doc, stream)
        time.sleep(0.5)
        say(
            "\nUpdated **revenue.yaml**.\n\n"
            "- Three section headings as text tiles: Sales at a glance, "
            "Where revenue comes from, Rhythm of the week.\n"
            "- Each has one line of context. Every other tile kept its place."
        )
        return

    if wants("terminal", "monospace", "mono", "amber", "console"):
        theme = "terminal"
    elif wants("newspaper", "newsprint", "editorial", "broadsheet"):
        theme = "broadsheet"
    elif wants("sunset", "ember", "coral", "warm", "fire"):
        theme = "ember"
    elif wants("lime", "acid", "punch", "bright", "citrus", "electric"):
        theme = "citrus"
    elif wants("soft", "pastel", "rose", "quartz", "paper"):
        theme = "quartz"
    else:
        theme = "neon"
    skin = yaml.load((path.parent / f"{theme}.yaml").read_text())
    say("Reading revenue.yaml and the pinned tiles.")
    time.sleep(1.3)
    weekday = [t for t in doc["tiles"] if str(t.get("title", "")).startswith("Orders by weekday")]
    if theme == "neon":
        say(
            "Going dark and glassy: violet cards, cyan accents, a glow behind revenue."
            + (" Widening the weekday tile." if weekday else "")
        )
    else:
        say(PLANS[theme])
    time.sleep(1.6)
    doc["css"] = skin["css"]
    doc["title"] = skin["title"]
    for tile in weekday:
        tile["position"] = flow(x=0, y=tile["position"]["y"], w=12, h=3)
    with path.open("w") as stream:
        yaml.dump(doc, stream)
    time.sleep(0.6)
    if theme == "neon":
        say(
            "\nUpdated **revenue.yaml**.\n\n"
            "- Violet glass cards on a deep grid, cyan accents, revenue lit as the focal point.\n"
            + ("- Orders by weekday is now full width, three rows tall.\n" if weekday else "")
            + "- Metrics, filters and queries are unchanged."
        )
    else:
        look = LOOKS[theme]
        kept = "Kept the layout" + (
            " and the section headings." if any("markdown" in t for t in doc["tiles"]) else "."
        )
        say(f"\nUpdated **revenue.yaml**.\n\n- {look}\n- {kept}")


if __name__ == "__main__":
    main()
