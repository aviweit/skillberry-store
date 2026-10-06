# Copyright 2025 IBM Corp.
# Licensed under the Apache License, Version 2.0

"""Derive a tool's JSON Schema parameters from its Python type annotations.

The docstring/AST parsers map every parameter to a single JSON type, so a
parameter annotated with a Pydantic model (``List[Passenger]``) or a
``Literal`` becomes a bare ``"array"`` / ``"string"`` and the structure of
its elements is lost. This module recovers that structure the same way the
annotation's own library does: it builds a Pydantic model from the function
signature and emits ``model_json_schema()``, with every ``$ref`` inlined so
the result is self-contained. The vMCP server publishes it as a tool's MCP
``inputSchema``; the stored tool params are left unchanged.

The tool body is never run. A reduced module is built from the source -
the function signature with its body stubbed, plus only the module-level
imports, classes and assignments that signature transitively references -
and executed in a fresh namespace. Any failure returns ``None`` so callers
keep the schema they already have.
"""

import ast
import inspect
import logging
import typing
from typing import Any, Dict, Iterable, List, Optional, Set

from pydantic import create_model

logger = logging.getLogger(__name__)

# Builtin and typing names whose schema the docstring parsers already express
# well enough; a signature using only these keeps the legacy params untouched.
_PLAIN_NAMES = {
    "str", "int", "float", "bool", "bytes", "list", "dict", "tuple", "set",
    "None", "Any", "object", "List", "Dict", "Tuple", "Set", "Optional",
    "Union", "Sequence", "Mapping", "typing",
}

_SELF_NAMES = ("self", "cls")

def _model_schema(reduced_source: str, func_name: str) -> Dict[str, Any]:
    """Execute the reduced module and return the pydantic schema of the signature."""
    namespace: Dict[str, Any] = {"__name__": "__skillberry_schema__"}
    exec(compile(reduced_source, "<tool-signature>", "exec"), namespace)
    func = namespace[func_name]
    hints = typing.get_type_hints(func, globalns=namespace, include_extras=True)
    fields = {}
    for name, param in inspect.signature(func).parameters.items():
        if name in _SELF_NAMES or param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[name] = (hints.get(name, Any), default)
    return create_model("parameters", **fields).model_json_schema()


def _referenced_names(nodes: Iterable[ast.AST]) -> Set[str]:
    names: Set[str] = set()
    for node in nodes:
        if node is None:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                names.add(sub.id)
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                # string (forward-reference) annotations such as "Passenger"
                try:
                    names |= _referenced_names([ast.parse(sub.value, mode="eval")])
                except SyntaxError:
                    pass
    return names


def _bound_names(node: ast.stmt) -> Set[str]:
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return {node.name}
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return {(a.asname or a.name).split(".")[0] for a in node.names}
    if isinstance(node, ast.Assign):
        return {t.id for t in node.targets if isinstance(t, ast.Name)}
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return {node.target.id}
    return set()


def _find_function(tree: ast.Module, func_name: str) -> Optional[ast.FunctionDef]:
    # the last definition wins, as when the module is executed
    found = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            found = node
    return found


def _signature_nodes(func: ast.FunctionDef) -> List[ast.AST]:
    args = func.args
    all_args = args.posonlyargs + args.args + args.kwonlyargs
    return [a.annotation for a in all_args] + list(args.defaults) + [
        d for d in args.kw_defaults if d is not None
    ]


def needs_rich_schema(module_source: str, func_name: str) -> bool:
    """True when the signature uses a ``Literal`` or a module-defined type.

    Only those signatures lose information in the docstring parsers, so only
    those pay for the derivation.
    """
    try:
        tree = ast.parse(module_source)
    except SyntaxError:
        return False
    func = _find_function(tree, func_name)
    if func is None:
        return False
    used = _referenced_names(_signature_nodes(func))
    if "Literal" in used:
        return True
    module_defined: Set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.Assign, ast.AnnAssign)):
            module_defined |= _bound_names(node)
    return bool((used - _PLAIN_NAMES) & module_defined)


def build_signature_module(module_source: str, func_name: str) -> Optional[str]:
    """Return the reduced module: the stubbed function and what it references."""
    tree = ast.parse(module_source)
    func = _find_function(tree, func_name)
    if func is None:
        return None

    candidates = [
        n for n in tree.body
        if isinstance(n, (ast.Import, ast.ImportFrom, ast.ClassDef, ast.Assign, ast.AnnAssign))
    ]
    needed = _referenced_names(_signature_nodes(func))
    kept: List[ast.stmt] = []
    changed = True
    while changed:
        changed = False
        for node in candidates:
            if node in kept or not (_bound_names(node) & needed):
                continue
            kept.append(node)
            needed |= _referenced_names([node])
            changed = True

    future = [
        n for n in tree.body
        if isinstance(n, ast.ImportFrom) and n.module == "__future__"
    ]
    stub = ast.FunctionDef(
        name=func.name,
        args=func.args,
        body=[ast.Expr(value=ast.Constant(value=Ellipsis))],
        decorator_list=[],
        returns=None,
        type_params=[],
    )
    body = future + [n for n in tree.body if n in kept and n not in future] + [stub]
    module = ast.Module(body=body, type_ignores=[])
    return ast.unparse(ast.fix_missing_locations(module))


def inline_refs(node: Any, defs: Dict[str, Any], _seen: tuple = ()) -> Any:
    """Replace every ``$ref`` into ``$defs`` with the referenced schema."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            key = ref.split("/")[-1]
            if key in _seen:
                raise ValueError(f"recursive model '{key}' cannot be inlined")
            target = inline_refs(defs[key], defs, _seen + (key,))
            siblings = {k: inline_refs(v, defs, _seen) for k, v in node.items() if k != "$ref"}
            return {**target, **siblings}
        return {k: inline_refs(v, defs, _seen) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [inline_refs(x, defs, _seen) for x in node]
    return node


def drop_null_types(node: Any) -> Any:
    """Rewrite JSON Schema-only constructs into ones every consumer understands.

    ``Optional[X]`` (``anyOf: [X, {"type": "null"}]``) becomes ``X``, keeping its
    ``default``, and ``const`` becomes a one-value ``enum``; so the result has no
    ``null`` type and every optional parameter keeps a single ``type``.
    """
    if isinstance(node, list):
        return [drop_null_types(x) for x in node]
    if not isinstance(node, dict):
        return node
    out = {k: drop_null_types(v) for k, v in node.items()}
    for key in ("anyOf", "oneOf"):
        options = out.get(key)
        if not isinstance(options, list):
            continue
        rest = [o for o in options if o != {"type": "null"}]
        if len(rest) == len(options):
            continue
        if len(rest) == 1 and isinstance(rest[0], dict):
            del out[key]
            out = {**rest[0], **out}
        else:
            out[key] = rest
    if "const" in out:
        out.setdefault("enum", [out["const"]])
        del out["const"]
    return out


def derive_params_schema(
    module_source: str,
    func_name: str,
    descriptions: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """Build the params schema from annotations, or ``None`` to keep the legacy one.

    Args:
        module_source: The full module source (classes and aliases the
            signature uses are usually defined outside the function).
        func_name: The top-level function to describe.
        descriptions: Per-parameter descriptions taken from the docstring;
            they override the (absent) descriptions in the derived schema.

    Returns:
        ``{"type": "object", "properties": ..., "required": [...]}`` with no
        ``$ref``/``$defs``, or ``None`` when the signature is plain or the
        derivation fails for any reason.
    """
    if isinstance(module_source, bytes):
        try:
            module_source = module_source.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        if not needs_rich_schema(module_source, func_name):
            return None
        reduced = build_signature_module(module_source, func_name)
        if reduced is None:
            return None
        raw = _model_schema(reduced, func_name)
        schema = drop_null_types(inline_refs(raw, raw.get("$defs", {})))
    except Exception as e:
        logger.warning(f"Could not derive annotation schema for '{func_name}': {e}")
        return None

    properties = schema.get("properties", {})
    untyped = [n for n, p in properties.items() if not isinstance(p.get("type"), str)]
    if untyped:
        # e.g. Union[A, B] or Any: keep the flat schema rather than publish a
        # parameter with no single type
        logger.info(f"Keeping the flat schema for '{func_name}': untyped {untyped}")
        return None
    for name, desc in (descriptions or {}).items():
        if name in properties and desc:
            properties[name]["description"] = desc
    return {
        "type": "object",
        "properties": properties,
        "required": schema.get("required", []),
    }
