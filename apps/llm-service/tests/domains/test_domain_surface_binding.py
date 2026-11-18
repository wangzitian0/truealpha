"""Domain surface binding gate: every OpenAPI op and MCP tool must have behavioral tests in its domain file."""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path

from llm_service.main import app
from llm_service.mcp_server import build_mcp_server

DOMAINS: tuple[str, ...] = ("panel", "strategy", "entities", "library", "auth", "admin", "system")
DOMAINS_DIR = Path(__file__).resolve().parent
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})


def _extract_http_and_mcp_calls(test_file: Path) -> tuple[list[tuple[str, str]], set[str]]:
    """Extract literal `(http_method, path)` client calls and `call_tool(name, ...)` calls from AST."""
    tree = ast.parse(test_file.read_text(encoding="utf-8"))
    http_calls: list[tuple[str, str]] = []
    mcp_calls: set[str] = set()

    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not fn.name.startswith("test_"):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _HTTP_METHODS and node.args:
                arg0 = node.args[0]
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str) and arg0.value.startswith("/"):
                    http_calls.append((func.attr, arg0.value.split("?", 1)[0]))
            if isinstance(func, ast.Attribute) and func.attr == "call_tool" and node.args:
                arg0 = node.args[0]
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                    mcp_calls.add(arg0.value)

    return http_calls, mcp_calls


def _matches_template(called_path: str, template: str, static_paths: set[str]) -> bool:
    if "{" not in template:
        return called_path == template
    if called_path in static_paths:
        return False
    pattern = "^" + re.sub(r"\{[^}]+\}", r"[^/]+", template) + "$"
    return re.match(pattern, called_path) is not None


def test_every_openapi_op_and_mcp_tool_is_bound_to_its_domain_test_suite() -> None:
    app.openapi_schema = None
    schema = app.openapi()
    paths = schema["paths"]
    static_paths = {p for p in paths if "{" not in p}

    ops_by_domain: dict[str, list[tuple[str, str, str]]] = {d: [] for d in DOMAINS}
    for path, methods in paths.items():
        for method, op in methods.items():
            if method not in _HTTP_METHODS or not isinstance(op, dict):
                continue
            tags = op.get("tags") or []
            op_id = str(op.get("operationId") or "")
            assert len(tags) == 1 and tags[0] in DOMAINS, f"{method.upper()} {path} has invalid domain tags: {tags!r}"
            domain = tags[0]
            assert op_id.startswith(f"{domain}_"), (
                f"{method.upper()} {path} operationId {op_id!r} must start with {domain}_"
            )
            ops_by_domain[domain].append((method, path, op_id))

    tools = asyncio.run(build_mcp_server().list_tools())
    tools_by_domain: dict[str, list[str]] = {d: [] for d in DOMAINS}
    for tool in tools:
        matched_domain = next((d for d in DOMAINS if tool.name.startswith(f"{d}_")), None)
        assert matched_domain is not None, f"MCP tool {tool.name!r} lacks a recognized domain prefix"
        tools_by_domain[matched_domain].append(tool.name)

    missing: list[str] = []
    for domain in DOMAINS:
        test_file = DOMAINS_DIR / f"test_{domain}_domain.py"
        if not test_file.exists():
            missing.append(f"missing domain test file: {test_file.name}")
            continue
        http_calls, mcp_calls = _extract_http_and_mcp_calls(test_file)
        for method, path, op_id in ops_by_domain[domain]:
            if not any(m == method and _matches_template(p, path, static_paths) for m, p in http_calls):
                missing.append(
                    f"{domain} HTTP {method.upper()} {path} ({op_id}) has no behavioral call in {test_file.name}"
                )
        for tool_name in tools_by_domain[domain]:
            if tool_name not in mcp_calls:
                missing.append(f"{domain} MCP tool {tool_name} has no call_tool() test in {test_file.name}")

    assert not missing, "Unbound domain operations/tools:\n" + "\n".join(missing)
