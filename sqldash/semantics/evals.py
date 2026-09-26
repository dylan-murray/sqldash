"""Agent evals without a judge model.

sqldash has the tools the agent is expected to call, so it computes the ground
truth itself: the expected call is run in-process and the host's answer must
report a number from that result. If the host happened to spawn `sqldash mcp`
with the environment `agent eval` hands it, a trace of the tool calls appears
as well and the case is also graded on what was called. Nothing is configured
either way; the report says which evidence graded each case.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqldash.connectors.base import ConnectorError
from sqldash.execution import ExecutionRegistry
from sqldash.models.agents import ANSWER_HAS_TOOLS, RESERVED_TOOL_NAMES, EvalCase, ExpectSpec
from sqldash.params import ParamError
from sqldash.period import COMPARE_MODES
from sqldash.project.store import StoreError
from sqldash.semantics.agents import ResolvedAgent, render_prompt, run_tool
from sqldash.semantics.bind import bind_metric
from sqldash.semantics.compare import compare_metric
from sqldash.semantics.layer import SemanticError

TRACE_ENV = "SQLDASH_MCP_TRACE"
PROMPT_ENV = "SQLDASH_AGENT_PROMPT_FILE"
COMPARE_KEYS = frozenset({"compare"})
STRUCTURE_KEYS = frozenset({"row_count", "truncated", "columns", "sql", "tool", "metric"})
NUMBER = re.compile(
    r"(?<![\w.])(?<!\w-)(?P<sign>[-+]?)[$€£¥]?(?P<digits>\d[\d,]*(?:\.\d+)?)"
    r"(?:[eE](?P<exponent>[-+]?\d{1,2}))?"
    r"(?:(?P<percent>%)|\s?(?P<word>(?i:thousand|million|billion|trillion))"
    r"|(?P<suffix>(?i:bn|[kmbt]))|[xX])?"
    r"(?!\w)(?!\.\d)"
)
MAGNITUDES = {
    "k": 1_000,
    "thousand": 1_000,
    "m": 1_000_000,
    "million": 1_000_000,
    "b": 1_000_000_000,
    "bn": 1_000_000_000,
    "billion": 1_000_000_000,
    "t": 1_000_000_000_000,
    "trillion": 1_000_000_000_000,
}
_TRUTH_ERRORS = (
    SemanticError,
    ParamError,
    ConnectorError,
    StoreError,
    ValueError,
    TypeError,
    KeyError,
)


@dataclass(frozen=True)
class TraceEvent:
    tool: str
    arguments: dict[str, Any]
    result: Any
    is_error: bool = False


@dataclass
class CaseResult:
    question: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    answer: str | None = None
    evidence: str = "answer"

    def payload(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "passed": self.passed,
            "evidence": self.evidence,
            "failures": self.failures,
            "warnings": self.warnings,
            "calls": self.calls,
            "answer": self.answer,
        }


def read_trace(path: Path) -> list[TraceEvent]:
    """The JSON-lines file `sqldash mcp` appends to when `SQLDASH_MCP_TRACE` is set."""
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            raw = None
        if not isinstance(raw, dict):
            events.append(
                TraceEvent("?", {}, {"error": f"unreadable trace line: {line[:80]}"}, True)
            )
            continue
        events.append(
            TraceEvent(
                raw.get("tool", "?"),
                raw.get("arguments") or {},
                raw.get("result"),
                bool(raw.get("is_error")),
            )
        )
    return events


def args_match(expected: Any, actual: Any) -> bool:
    """`expected` is a subset of `actual`: dict keys recursively, lists as sets."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and args_match(v, actual[k]) for k, v in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and all(
            any(args_match(e, a) for a in actual) for e in expected
        )
    return _same(expected, actual)


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return str(a) == str(b)


@dataclass(frozen=True, order=True)
class Figure:
    """A figure as the answer wrote it: `value` is what it means, `scale` the unit it
    was rounded in (1000 for `107.9K`, 1e6 for `6.2 million` or `6.2e6`)."""

    value: float
    percent: bool = False
    scale: float = 1.0


def numbers_in_text(text: str) -> set[Figure]:
    """Figures a person would call a reported number (at least 10, or with decimals;
    small integers like "3 regions" are prose). A magnitude suffix or exponent is part
    of the figure. Only a percent may match a fraction scaled by 100."""
    found: set[Figure] = set()
    for match in NUMBER.finditer(text):
        digits = match["digits"].replace(",", "")
        magnitude = (match["word"] or match["suffix"] or "").lower()
        scale = Decimal(MAGNITUDES.get(magnitude, 1)).scaleb(int(match["exponent"] or 0))
        value = float(Decimal(match["sign"] + digits) * scale)
        if abs(value) >= 10 or "." in digits:
            found.add(Figure(value, bool(match["percent"]), float(scale)))
    return found


def numbers_in(value: Any, *, skip: frozenset[str] = frozenset()) -> set[float]:
    """Every figure in a payload; `skip` names keys to leave out (the `compare`
    block, so last period's numbers cannot stand in for this period's)."""
    found: set[float] = set()

    def walk(v: Any) -> None:
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            found.add(float(v))
        elif isinstance(v, dict):
            for key, item in v.items():
                if key not in skip:
                    walk(item)
        elif isinstance(v, (list, tuple)):
            for item in v:
                walk(item)

    walk(value)
    return found


def sourced(
    number: float, results: set[float], *, percent: bool = False, scale: float = 1.0
) -> bool:
    """A figure is sourced if it is a result value, or that value rounded to 0-2 dp in
    the unit the figure was written in (`107.9K` is 107889.79 to 1 dp of thousands),
    or — when it was written as a percent — a fraction times 100 (same tolerance)."""
    written = number / scale
    for value in results:
        scales = (value, value * 100) if percent else (value,)
        for scaled in scales:
            if abs(number - scaled) < 1e-9:
                return True
            unit = scaled / scale
            if any(abs(written - round(unit, dp)) < 1e-9 for dp in (0, 1, 2)):
                return True
    return False


def _sourced_figure(figure: Figure, results: set[float]) -> bool:
    return sourced(figure.value, results, percent=figure.percent, scale=figure.scale)


def _shown(figures: Any) -> str:
    return ", ".join(f"{n:g}" for n in sorted({f.value for f in figures})[:5])


def _expected_tool_name(agent: ResolvedAgent, name: str | None) -> str | None:
    if name is not None and agent.repo and name not in RESERVED_TOOL_NAMES:
        return f"{agent.repo}__{name}"
    return name


def ground_truth(
    agent: ResolvedAgent, expect: ExpectSpec, registry: ExecutionRegistry, row_limit: int
) -> Any:
    """Run the expected call the way the MCP server would and return its payload.
    None for built-ins other than query_metric, including run_sql."""
    tool = _expected_tool_name(agent, expect.tool)
    if tool is None:
        return None
    if tool == "query_metric":
        args = dict(expect.args)
        name = args.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("a query_metric eval needs args.name")
        grain = args.get("grain")
        compare = args.get("compare")
        if compare is not None and compare not in COMPARE_MODES:
            raise ValueError(f"unknown compare '{compare}'")
        dashboard = args.get("dashboard")
        dash = None
        if dashboard is not None:
            if agent.repo and dashboard.startswith(f"{agent.repo}/"):
                dashboard = dashboard.split("/", 1)[1]
            dash, _, _ = agent.layer.store.load(dashboard)
        try:
            limit = args.get("limit")
            cap = row_limit if limit is None else min(int(limit), row_limit)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"limit must be a number, not {limit!r}") from exc
        if cap <= 0:
            raise ValueError("limit must be positive")
        kwargs = {
            "layer": agent.layer,
            "name": name,
            "scope": dashboard,
            "dash": dash,
            "params": args.get("filters") if dash is not None else None,
            "dimensions": args.get("dimensions") or [],
            "grain": grain,
            "filters": args.get("filters"),
            "start": args.get("start"),
            "end": args.get("end"),
        }
        bound = bind_metric(**kwargs)
        result = registry.run_bound(bound, cap)
        payload: dict[str, Any] = {"rows": result.rows}
        if compare is not None:
            payload["compare"] = compare_metric(
                compare,
                bound,
                result,
                rebind=lambda s, e: bind_metric(**{**kwargs, "start": s, "end": e}),
                run=lambda prev: registry.run_bound(prev, cap),
                grain=grain,
                dimensions=kwargs["dimensions"],
                dash=dash,
            ).payload()
        return payload
    if tool in RESERVED_TOOL_NAMES:
        return None
    resolved = next((t for t in agent.tools if t.name == tool), None)
    if resolved is None:
        raise ValueError(f"agent has no tool '{tool}'")
    return run_tool(resolved, dict(expect.args), registry, row_limit)


def grade(
    agent: ResolvedAgent,
    case: EvalCase,
    answer: str | None,
    *,
    truth: Any = None,
    events: list[TraceEvent] | None = None,
) -> CaseResult:
    """Grade one case. `truth` is the expected call's real result; `events` is the
    MCP trace when the host produced one (None means no trace, an empty list means
    a trace with no calls in it)."""
    traced = events is not None
    events = events or []
    result = CaseResult(
        case.question,
        True,
        calls=[_describe(e) for e in events],
        answer=answer,
        evidence="trace+answer" if traced else "answer",
    )
    expect = case.expect
    reported = numbers_in_text(answer) if answer else set()
    if expect is not None and expect.refuses:
        if traced and events:
            result.failures.append(f"expected no tool call, saw {len(events)}")
        if reported:
            result.failures.append(
                f"expected a refusal but the answer reports figures: {_shown(reported)}"
            )
    elif expect is not None:
        expected_numbers = (
            numbers_in(truth, skip=COMPARE_KEYS | STRUCTURE_KEYS) if truth is not None else set()
        )
        if expected_numbers and not any(_sourced_figure(f, expected_numbers) for f in reported):
            sample = ", ".join(f"{n:g}" for n in sorted(expected_numbers)[:3])
            result.failures.append(
                f"answer reports none of the figures {expect.tool} actually returns (e.g. {sample})"
            )
        elif truth is not None and not expected_numbers and not case.answer_has:
            result.failures.append(
                f"{expect.tool} returns no figures for these args (a filter that matches "
                f"nothing?), so there is nothing to grade against; fix the args or add answer_has"
            )
        elif expect.tool in ANSWER_HAS_TOOLS and not traced and not case.answer_has:
            result.failures.append(
                f"{expect.tool} has no computed ground truth and no trace was produced, "
                f"so there is nothing to grade; add answer_has"
            )
        if traced:
            tool = _expected_tool_name(agent, expect.tool)
            hits = [e for e in events if e.tool == tool and not e.is_error]
            if not any(args_match(expect.args, e.arguments) for e in hits):
                want = f"{tool}({json.dumps(expect.args, default=str)})"
                seen = ", ".join(result.calls) or "no calls"
                result.failures.append(f"expected a call to {want}; saw {seen}")
    if traced and not agent.definition.sql and any(e.tool == "run_sql" for e in events):
        result.failures.append("run_sql was called but the agent has sql: false")
    if case.answer_has:
        if answer is None:
            result.failures.append("no answer to check answer_has against")
        else:
            folded = answer.casefold()
            for needle in case.answer_has:
                if needle.casefold() not in folded:
                    result.failures.append(f"answer does not contain {needle!r}")
    if reported and (truth is not None or events):
        known = numbers_in(truth, skip=STRUCTURE_KEYS) | numbers_in(
            [e.result for e in events], skip=STRUCTURE_KEYS
        )
        unsourced = [f for f in reported if not _sourced_figure(f, known)]
        if unsourced:
            result.warnings.append(
                f"figures in the answer that no result contains: {_shown(unsourced)}"
            )
    result.passed = not result.failures
    return result


def _describe(event: TraceEvent) -> str:
    args = json.dumps(event.arguments, default=str, sort_keys=True)
    return f"{event.tool}({args})" + (" [error]" if event.is_error else "")


def run_case(
    agent: ResolvedAgent,
    case: EvalCase,
    runner: str,
    registry: ExecutionRegistry,
    *,
    row_limit: int = 1000,
    timeout: float = 300.0,
    env: dict[str, str] | None = None,
) -> CaseResult:
    """Run one question through `runner` and grade the answer.

    The runner is a shell command; the question is appended as its last argument
    and piped to stdin. The environment carries `SQLDASH_AGENT_PROMPT_FILE` (the
    rendered prompt, for runners that take a system prompt), `SQLDASH_AGENT`, and
    `SQLDASH_MCP_TRACE`, which any `sqldash mcp` the host spawns appends its
    calls to. Whether that file appears decides the evidence the case is graded on."""
    truth = None
    if case.expect is not None and not case.expect.refuses:
        try:
            truth = ground_truth(agent, case.expect, registry, row_limit)
        except _TRUTH_ERRORS as exc:
            return CaseResult(case.question, False, [f"cannot compute the expected result: {exc}"])
    with tempfile.TemporaryDirectory(prefix="sqldash-eval-") as tmp:
        trace = Path(tmp) / "trace.jsonl"
        prompt_file = Path(tmp) / "prompt.md"
        prompt_file.write_text(render_prompt(agent), encoding="utf-8")
        child_env = {
            **os.environ,
            **(env or {}),
            TRACE_ENV: str(trace),
            PROMPT_ENV: str(prompt_file),
            "SQLDASH_AGENT": agent.name,
        }
        try:
            proc = subprocess.run(
                f"{runner} {shlex.quote(case.question)}",
                # semgrep: runner is the eval author's command; the question is shlex-quoted
                # nosemgrep: subprocess-shell-true
                shell=True,
                input=case.question,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=child_env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CaseResult(case.question, False, [f"runner timed out after {timeout:g}s"])
        events = read_trace(trace) if trace.exists() else None
        result = grade(agent, case, proc.stdout, truth=truth, events=events)
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no stderr"]
            result.failures.append(f"runner exited {proc.returncode}: {tail[0]}")
            result.passed = False
        return result


def run_evals(
    agent: ResolvedAgent, runner: str, *, row_limit: int = 1000, timeout: float = 300.0
) -> list[CaseResult]:
    registry = ExecutionRegistry(max_workers=2)
    try:
        return [
            run_case(agent, case, runner, registry, row_limit=row_limit, timeout=timeout)
            for case in agent.definition.evals
        ]
    finally:
        registry.shutdown()
