import json

import pytest
from ruamel.yaml.constructor import SafeConstructor

from sqldash.studio.review import COMPLEXITY_LIMIT, changes, safe_document


@pytest.mark.parametrize(
    "document",
    [
        "!!pairs\n- source:\n    url: postgresql://u:SECRET_SENTINEL@host/db\n",
        "x: !!pairs\n- password: SECRET_SENTINEL\n",
        "x: !!set\n  ? [password, SECRET_SENTINEL]\n",
        "? [password, SECRET_SENTINEL]\n: ordinary\n",
        "!!binary cGFzc3dvcmQ=: SECRET_SENTINEL\n",
        "blob: !!binary U0VDUkVUX1NFTlRJTkVM\n",
    ],
)
def test_review_rejects_yaml_types_that_cannot_be_safely_redacted(document):
    result = safe_document(document.encode())
    assert result.startswith("[Text unavailable:")
    assert "SECRET_SENTINEL" not in result
    diff = changes({"metrics.yaml": b"{}\n"}, {"metrics.yaml": document.encode()})
    assert "SECRET_SENTINEL" not in json.dumps(diff)


def test_review_preserves_supported_scalars_ordered_maps_and_aliases():
    document = b"""title: Demo
count: 4
ratio: 1.5
on: true
nothing: null
day: 2026-09-10
stamp: 2026-09-10T01:02:03Z
metadata: !!omap
- password: SECRET_SENTINEL
- title: Ordered
defaults: &defaults {title: Shared, source: {url: SECRET_SENTINEL}}
copy: *defaults
merged: {<<: *defaults, title: Override}
recursive: &recursive [*recursive]
"""
    rendered = safe_document(document)
    assert "SECRET_SENTINEL" not in rendered
    result = json.loads(rendered)
    assert result["count"] == 4
    assert result["ratio"] == 1.5
    assert result["on"] is True
    assert result["nothing"] is None
    assert result["day"] == "2026-09-10"
    assert result["stamp"] == "2026-09-10 01:02:03+00:00"
    assert result["metadata"] == {"password": "[omitted]", "title": "Ordered"}
    assert result["merged"] == {"title": "Override", "source": "[omitted]"}
    assert result["copy"] == "[Repeated YAML alias omitted]"
    assert result["recursive"] == ["[Repeated YAML alias omitted]"]


@pytest.mark.parametrize("kind", ["merge", "alias", "depth", "recursive_merge"])
def test_review_rejects_expansion_before_yaml_construction(monkeypatch, kind):
    if kind == "depth":
        document = "a: " + "[" * 45 + "0" + "]" * 45
    elif kind == "recursive_merge":
        document = "a: &a {<<: *a, title: Recursive}\n"
    else:
        lines = ["a0: &a0 {title: Small}"]
        for index in range(1, 14):
            aliases = f"[*a{index - 1}, *a{index - 1}]"
            value = "{<<: " + aliases + "}" if kind == "merge" else aliases
            lines.append(f"a{index}: &a{index} {value}")
        document = "\n".join(lines)
    constructed = []
    original = SafeConstructor.construct_document

    def construct(self, node):
        constructed.append(node)
        return original(self, node)

    monkeypatch.setattr(SafeConstructor, "construct_document", construct)
    assert safe_document(document.encode()) == COMPLEXITY_LIMIT
    assert constructed == []


def test_review_measures_cached_alias_depth_at_each_reference():
    document = "shared: &shared " + "[" * 20 + "0" + "]" * 20
    document += "\ndeep: " + "[" * 25 + "*shared" + "]" * 25
    assert safe_document(document.encode()) == COMPLEXITY_LIMIT


def test_unsupported_yaml_changes_are_not_reported_as_formatting():
    before = {"metrics.yaml": b"x: !!pairs [{password: first}]\n"}
    after = {"metrics.yaml": b"x: !!pairs [{password: second}]\n"}
    result = changes(before, after)
    assert "Content changed" in result[0]["diff"]
    assert "Formatting-only" not in result[0]["diff"]
    assert "first" not in result[0]["diff"]
    assert "second" not in result[0]["diff"]
