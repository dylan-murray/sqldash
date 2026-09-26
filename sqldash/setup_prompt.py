"""TTY wizard for `sqldash setup`. apply_setup and the flag path stay unchanged."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import questionary
import typer
from questionary import Choice

from sqldash.secrets import SecretError, load_profiles
from sqldash.setup import TYPE_SPECS, SetupPlan, SetupResult, default_profile_name, env_var_name

Choose = Callable[..., str]
Ask = Callable[..., str | None]
Echo = Callable[..., Any]

_STYLE = questionary.Style(
    [
        ("qmark", "fg:cyan bold"),
        ("question", "bold"),
        ("answer", "fg:cyan"),
        ("pointer", "fg:cyan bold"),
        ("highlighted", "fg:cyan bold"),
        ("selected", "fg:cyan"),
        ("instruction", "fg:#6b7280"),
        ("text", ""),
        ("error", "fg:red bold"),
        ("validation-toolbar", "fg:red bold"),
    ]
)

WAREHOUSES: list[tuple[str, str]] = [
    ("duckdb", "duckdb — local files"),
    ("postgres", "postgres"),
    ("snowflake", "snowflake"),
    ("bigquery", "bigquery"),
    ("databricks", "databricks"),
    ("mysql", "mysql"),
    ("url", "url — SQLAlchemy URL"),
]

AUTH: list[tuple[str, str]] = [
    ("externalbrowser", "browser SSO"),
    ("password", "password (env var)"),
    ("pat", "token (env var)"),
    ("keypair", "key pair"),
]

EXISTING_PROFILE: list[tuple[str, str]] = [
    ("keep", "keep existing values"),
    ("overwrite", "overwrite with new values"),
]


def _cancelled(value: Any) -> Any:
    if value is None:
        raise typer.Exit(1)
    return value


def _menu_choose(title: str, options: list[tuple[str, str]], default: str, **_: Any) -> str:
    choices = [Choice(label, value=value) for value, label in options]
    selected = next((c for c in choices if c.value == default), choices[0])
    return _cancelled(
        questionary.select(
            title,
            choices=choices,
            default=selected,
            style=_STYLE,
            use_shortcuts=True,
            use_arrow_keys=True,
            use_jk_keys=True,
        ).ask()
    )


def _menu_ask(
    label: str, *, required: bool = True, default: str | None = None, **_: Any
) -> str | None:
    kwargs: dict[str, Any] = {"style": _STYLE, "default": default or ""}
    if required:
        kwargs["validate"] = lambda text: True if str(text).strip() else f"{label} is required"
    value = _cancelled(questionary.text(label, **kwargs).ask())
    value = str(value).strip()
    return value or None


def _named_profile(name: str) -> dict[str, Any] | None:
    try:
        return load_profiles().get(name)
    except SecretError:
        return None


def _profile_hint(existing: dict[str, Any]) -> str:
    bits = []
    user = existing.get("username") or existing.get("user")
    if user:
        bits.append(str(user))
    auth = existing.get("authentication")
    if auth:
        bits.append(str(auth))
    return f" ({', '.join(bits)})" if bits else ""


def prompt_setup(
    directory: Path,
    plan: SetupPlan | None = None,
    *,
    choose: Choose | None = None,
    ask: Ask | None = None,
    echo: Echo | None = None,
    existing_profile: Callable[[str], dict[str, Any] | None] | None = None,
) -> SetupPlan:
    """Full wizard when plan is None; otherwise only the fields still missing."""
    echo = echo or typer.echo
    choose = choose or _menu_choose
    ask = ask or _menu_ask
    lookup = existing_profile or _named_profile
    if plan is None:
        if echo is typer.echo:
            typer.secho("sqldash setup", bold=True)
            typer.secho("secrets stay in ${env:VAR} refs, never in the repo.", dim=True)
            typer.echo()
        else:
            echo("sqldash setup")
            echo("secrets stay in ${env:VAR} refs, never in the repo.")
            echo()
        kind = choose("warehouse", WAREHOUSES, "duckdb")
        plan = SetupPlan(source_type=kind)
        return _fill(directory, plan, full=True, choose=choose, ask=ask, existing_profile=lookup)
    return _fill(directory, plan, full=False, choose=choose, ask=ask, existing_profile=lookup)


def _fill(
    directory: Path,
    plan: SetupPlan,
    *,
    full: bool,
    choose: Choose,
    ask: Ask,
    existing_profile: Callable[[str], dict[str, Any] | None],
) -> SetupPlan:
    kind = plan.source_type
    spec = TYPE_SPECS.get(kind)
    if spec is None:
        return plan

    if kind == "duckdb":
        return plan
    if kind == "url":
        if not plan.url:
            plan.url = ask("SQLAlchemy URL (secrets as ${env:VAR})")
        return plan
    if kind == "bigquery":
        if not plan.project and not plan.host:
            plan.project = ask("project")
        if full and not plan.database:
            plan.database = ask("dataset (optional)", required=False)
        return plan

    if spec.uses_profile and full and not plan.profile:
        plan.profile = ask("profile name", default=default_profile_name(directory))
    profile = plan.profile or default_profile_name(directory)
    plan.profile = profile
    existing = existing_profile(profile)
    if existing and not plan.keep_profile:
        hint = _profile_hint(existing)
        action = choose(
            f"profile '{profile}' already exists{hint}",
            EXISTING_PROFILE,
            "keep",
        )
        if action == "keep":
            plan.keep_profile = True
            if not plan.username:
                plan.username = existing.get("username") or existing.get("user")
    keep = plan.keep_profile

    if kind == "snowflake":
        if not plan.account:
            plan.account = ask("account")
        if full and not plan.warehouse:
            plan.warehouse = ask("warehouse (optional)", required=False)
        if full and not plan.database:
            plan.database = ask("database (optional)", required=False)
        if not keep and not plan.username:
            plan.username = ask("username")
        if not keep and full and not plan.authentication:
            plan.authentication = choose("how to sign in", AUTH, "externalbrowser")
        auth = plan.authentication or "externalbrowser"
        if not keep and auth == "password" and not plan.password_env:
            plan.password_env = ask(
                "env var that will hold the password",
                default=env_var_name(profile, "PASSWORD"),
            )
        elif not keep and auth == "pat" and not plan.token_env:
            plan.token_env = ask(
                "env var that will hold the token",
                default=env_var_name(profile, "TOKEN"),
            )
        elif not keep and auth == "keypair" and not plan.private_key_path:
            plan.private_key_path = ask("private key path")
        return plan

    if kind == "databricks":
        if not plan.host:
            plan.host = ask("host")
        if not plan.http_path:
            plan.http_path = ask("HTTP path")
        if full and not plan.catalog:
            plan.catalog = ask("catalog (optional)", required=False)
        if not keep and not plan.token_env:
            plan.token_env = ask(
                "env var that will hold the token",
                default=env_var_name(profile, "TOKEN"),
            )
        return plan

    if not plan.host:
        plan.host = ask("host")
    if not plan.database:
        plan.database = ask("database")
    if not keep and full and not plan.username:
        plan.username = ask("username (optional)", required=False)
    if not keep and full and not plan.password_env:
        plan.password_env = ask(
            "env var that will hold the password (optional)",
            required=False,
        )
    return plan


def render_setup_result(
    result: SetupResult,
    directory: Path,
    *,
    env: dict[str, str] | None = None,
) -> list[str]:
    env = os.environ if env is None else env
    if result.test_ok is False:
        lines = [f"wrote  {result.metrics_path.parent}"]
        if result.profiles_path and result.profile:
            lines.append(f"  profile  {result.profiles_path}  ({result.profile})")
        lines.extend(_dashboard_lines(result))
        lines.append("")
        lines.append("FAILED  could not connect")
        if result.test_error:
            lines.append(f"  {result.test_error}")
        if result.restored_files:
            names = ", ".join(result.restored_files)
            lines.append(f"  restored  {names} to what they were; nothing points at the new source")
        lines.append("")
        lines.append("re-run:  sqldash setup")
        return lines

    lines = [f"ready  {result.metrics_path.parent}"]
    if result.profiles_path and result.profile:
        lines.append(f"  profile  {result.profiles_path}  ({result.profile})")
    lines.extend(_dashboard_lines(result))
    unset = [name for name in result.needed_env if name not in env]
    for name in unset:
        lines.append(f"  set      export {name}=…")
    if result.test_ok is True:
        lines.append("  connected")
    elif any("externalbrowser" in n for n in result.notes):
        lines.append("  skipped connection test (browser SSO) — sqldash source test")
    for note in result.notes:
        if "registered" in note:
            lines.append(f"  {note}")
    lines.append("")
    prefix = f"cd {directory} && " if directory != Path(".") else ""
    if result.created_metrics:
        lines.append(f"next:  {prefix}sqldash source describe   # your tables")
        lines.append("       add relations and metrics to metrics.yaml, then")
        lines.append(f"       {prefix}sqldash serve")
    elif result.no_dashboards:
        lines.append(f"next:  {prefix}sqldash init --demo   # the sample dashboard")
        lines.append("       or write a dashboard yaml in .sqldash/, then")
        lines.append(f"       {prefix}sqldash serve")
    else:
        lines.append(f"next:  {prefix}sqldash serve")
    return lines


def _dashboard_lines(result: SetupResult) -> list[str]:
    lines = [f"  updated  {name} source" for name in result.updated_dashboards]
    lines.extend(
        f"  warning  {name} still points at a different source; edit its source: by hand"
        for name in result.stale_dashboards
    )
    if result.sample_schema_files:
        names = ", ".join(result.sample_schema_files)
        lines.append(
            f"  warning  {names} still use the sample orders schema; rewrite or delete "
            "them (sqldash source describe lists your tables)"
        )
    lines.extend(
        f"  warning  {name} could not be parsed; left alone (sqldash lint names the problem)"
        for name in result.unreadable_dashboards
    )
    return lines


def print_setup_result(result: SetupResult, directory: Path) -> None:
    failed = result.test_ok is False
    for line in render_setup_result(result, directory):
        if line.startswith("  warning"):
            typer.secho(line, fg="yellow", err=failed)
        elif line.startswith("FAILED"):
            typer.secho(line, fg="red", bold=True, err=True)
        elif failed and result.test_error and result.test_error in line:
            typer.secho(line, fg="red", err=True)
        elif failed and line.startswith("re-run:"):
            typer.secho(line, fg="yellow", err=True)
        elif failed:
            typer.echo(line, err=True)
        elif line.startswith("ready") or line.strip() == "connected":
            typer.secho(line, fg="green", bold=True)
        elif line.startswith(("next:", "       ")):
            typer.secho(line, fg="cyan")
        elif line.startswith("  set"):
            typer.secho(line, fg="yellow")
        else:
            typer.echo(line)
