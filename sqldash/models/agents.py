"""Agents-as-code contract: agents.yaml — agents served to a host over MCP, and the
data tools they may call. sqldash never runs a model; it defines and serves."""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sqldash.models.semantics import Grain, validate_identifier
from sqldash.params import extract_params
from sqldash.period import COMPARE_MODES

RESERVED_TOOL_NAMES = frozenset(
    {
        "list_metrics",
        "get_metric",
        "query_metric",
        "list_sources",
        "get_schema",
        "validate_metrics",
        "validate_dashboard",
        "get_dashboards",
        "run_sql",
    }
)
ANSWER_HAS_TOOLS = RESERVED_TOOL_NAMES - {"query_metric"}
PARAM_REF = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")
ParamType = Literal["text", "number", "select", "date"]


def param_ref(value: Any) -> str | None:
    """The param a `{{ name }}` value points at, or None for a literal."""
    if not isinstance(value, str):
        return None
    match = PARAM_REF.match(value)
    return match[1] if match else None


class ToolParam(BaseModel):
    """One argument of a tool. Values bind natively; `options` constrains a select."""

    model_config = ConfigDict(extra="forbid")

    type: ParamType = "text"
    description: str | None = None
    default: Any = None
    options: list[Any] | None = None
    options_sql: str | None = None

    @model_validator(mode="after")
    def select_shapes(self) -> "ToolParam":
        if (self.options is not None or self.options_sql is not None) and self.type != "select":
            raise ValueError("options and options_sql only apply to select params")
        if self.options is not None and self.options_sql is not None:
            raise ValueError("use either 'options' or 'options_sql', not both")
        return self


class ToolQuery(BaseModel):
    """One governed-metric evaluation inside a metric bundle. Filter, start, and end
    values may be `{{ param }}` references, bound from the tool's arguments."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    dimensions: list[str] = []
    grain: Grain | None = None
    filters: dict[str, Any] = {}
    start: Any = None
    end: Any = None
    compare: Literal["previous_period", "yoy"] | None = None
    limit: int = 100

    @field_validator("limit")
    @classmethod
    def limit_is_positive(cls, v: int) -> int:
        """The cap is applied when the rows are fetched, and the executor refuses a
        negative one at run time; refusing it here names the file instead."""
        if v <= 0:
            raise ValueError("limit must be positive")
        return v

    @field_validator("metric")
    @classmethod
    def metric_is_identifier(cls, v: str) -> str:
        return validate_identifier(v, "metric name")

    @field_validator("dimensions")
    @classmethod
    def dimensions_are_identifiers(cls, v: list[str]) -> list[str]:
        return [validate_identifier(d, "dimension name") for d in v]

    @field_validator("compare")
    @classmethod
    def compare_is_known(cls, v):
        if v is not None and v not in COMPARE_MODES:
            raise ValueError(f"compare must be one of {', '.join(COMPARE_MODES)}")
        return v

    def param_refs(self) -> set[str]:
        refs = {r for r in (param_ref(v) for v in self.filters.values()) if r}
        refs |= {r for r in (param_ref(self.start), param_ref(self.end)) if r}
        return refs


class ToolDef(BaseModel):
    """A data tool sqldash executes itself: exactly one of `queries` (a metric bundle)
    or `sql` (author-written, `{{ param }}` placeholders bound natively). Nothing
    else — a tool that runs outside the warehouse is a runtime, not a definition."""

    model_config = ConfigDict(extra="forbid")

    description: str
    params: dict[str, ToolParam] = {}
    queries: list[ToolQuery] = []
    sql: str | None = None

    @model_validator(mode="after")
    def exactly_one_body(self) -> "ToolDef":
        if bool(self.queries) == bool(self.sql):
            raise ValueError("tool requires exactly one of 'queries' or 'sql'")
        for name in self.params:
            validate_identifier(name, "param name")
        if self.sql is not None and "{%" in self.sql:
            raise ValueError("tool sql cannot use {% if %} blocks; declare params instead")
        used = self.placeholders()
        undeclared = sorted(used - set(self.params))
        if undeclared:
            raise ValueError(f"tool uses undeclared param(s): {', '.join(undeclared)}")
        unused = sorted(set(self.params) - used)
        if unused:
            raise ValueError(f"tool declares param(s) it never uses: {', '.join(unused)}")
        return self

    def placeholders(self) -> set[str]:
        if self.sql is not None:
            return set(extract_params(self.sql))
        refs: set[str] = set()
        for query in self.queries:
            refs |= query.param_refs()
        return refs

    def required_params(self) -> list[str]:
        return [name for name, p in self.params.items() if p.default is None]


class ExternalUse(BaseModel):
    """A declared external MCP server the agent relies on. sqldash does not wire it;
    the host attaches it. `for` says what the agent uses it for."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    server: str
    purpose: str = Field(alias="for")


class ExpectSpec(BaseModel):
    """What a graded run must show in the trace: a call to `tool` whose arguments
    include `args` (subset match, lists order-insensitive), or no tool call at all."""

    model_config = ConfigDict(extra="forbid")

    tool: str | None = None
    args: dict[str, Any] = {}
    refuses: bool = False

    @model_validator(mode="after")
    def tool_or_refusal(self) -> "ExpectSpec":
        if bool(self.tool) == self.refuses:
            raise ValueError("expect needs exactly one of 'tool' or 'refuses: true'")
        if self.refuses and self.args:
            raise ValueError("a refusal expects no call, so it cannot carry args")
        return self


class EvalCase(BaseModel):
    """One question and what a good run looks like. `expect` is graded from the MCP
    trace; `answer_has` are plain substrings the host's final answer must contain."""

    model_config = ConfigDict(extra="forbid")

    question: str
    expect: ExpectSpec | None = None
    answer_has: list[str] = []

    @model_validator(mode="after")
    def something_to_grade(self) -> "EvalCase":
        if self.expect is None and not self.answer_has:
            raise ValueError("an eval needs 'expect' or 'answer_has', or it grades nothing")
        tool = self.expect.tool if self.expect else None
        if tool in ANSWER_HAS_TOOLS and not self.answer_has:
            raise ValueError(
                f"expecting {tool} grades nothing on its own (no computed ground truth); "
                f"add answer_has"
            )
        return self


class VerifiedExample(BaseModel):
    """An author-approved question and concrete invocation of a metric bundle."""

    model_config = ConfigDict(extra="forbid")

    name: str
    question: str = Field(min_length=1)
    tool: str
    args: dict[str, Any] = {}
    verified_by: str | None = None
    verified_at: int | None = Field(default=None, ge=0)

    @field_validator("name", "tool")
    @classmethod
    def identifiers(cls, value: str) -> str:
        return validate_identifier(value, "verified example name/tool")

    @field_validator("question")
    @classmethod
    def nonblank_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("verified question cannot be blank")
        return value


class AgentDef(BaseModel):
    """An agent: instructions plus what it may touch. Empty `metrics` means every
    metric in the layer. `sql: false` (the default) tells the host raw SQL is off."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str
    instructions: str
    response: str | None = None
    metrics: list[str] = []
    dimensions: list[str] = []
    tools: list[str] = []
    sql: bool = False
    sample_questions: list[str] = []
    uses: list[ExternalUse] = []
    evals: list[EvalCase] = []
    verified: list[VerifiedExample] = []

    @field_validator("metrics", "dimensions", "tools")
    @classmethod
    def names_are_identifiers(cls, v: list[str]) -> list[str]:
        for name in v:
            validate_identifier(name, "name")
        if len(set(v)) != len(v):
            raise ValueError("names must be unique")
        return v


class AgentsFile(BaseModel):
    """The project-level agents.yaml: shared tools, and agents that list them by name."""

    model_config = ConfigDict(extra="forbid")

    tools: dict[str, ToolDef] = {}
    agents: dict[str, AgentDef] = {}

    @model_validator(mode="after")
    def check_references(self) -> "AgentsFile":
        for name in self.tools:
            validate_identifier(name, "tool name")
            if name in RESERVED_TOOL_NAMES:
                raise ValueError(f"tool '{name}' collides with a built-in MCP tool")
        for name, agent in self.agents.items():
            validate_identifier(name, "agent name")
            names = [example.name for example in agent.verified]
            if len(set(names)) != len(names):
                raise ValueError(f"agent '{name}' verified example names must be unique")
            for example in agent.verified:
                if example.tool not in agent.tools:
                    raise ValueError(
                        f"verified '{example.name}' must name one of the agent's tools"
                    )
                tool_def = self.tools.get(example.tool)
                if tool_def is not None and tool_def.sql is not None:
                    raise ValueError(f"verified '{example.name}' requires a metric bundle, not SQL")
            for tool in agent.tools:
                if tool not in self.tools:
                    raise ValueError(f"agent '{name}' references unknown tool '{tool}'")
        return self
