#!/usr/bin/env python3
"""
Convert the GenomeNote JSON schema files into flat, readable TSV files.

The converter walks each schema and emits one row per property, resolving
``$ref`` references to the shared ``_author_schema.json`` /
``_protocol_schema.json`` definitions along the way.

It understands the constructs actually used by these schemas:

* array-style ``required`` lists declared on the enclosing object (the
  standard JSON Schema form),
* ``items`` sub-schemas for arrays,
* ``if``/``then``/``else`` and ``allOf``/``anyOf``/``oneOf`` branches, which are
  reported in the ``condition`` column,
* ``enum``/``const`` value lists,
* the project-specific ``help``, ``recommended`` and ``source`` annotations,
* the shorthand ``"field": "string"`` form, and properties that have been
  declared outside a ``properties`` block (these are still reported, but a
  warning is printed so the schema can be tidied up).
"""

import csv
import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

FIELDNAMES = [
    "property_name",
    "parent_property",
    "path",
    "type",
    "description",
    "enum",
    "pattern",
    "required",
    "recommended",
    "source",
    "condition",
]

# Keys that are schema keywords (or project annotations) rather than the name of
# a child property.
SCHEMA_KEYWORDS = {
    "$comment",
    "$defs",
    "$id",
    "$ref",
    "$schema",
    "additionalProperties",
    "allOf",
    "anyOf",
    "const",
    "contains",
    "default",
    "dependentRequired",
    "dependentSchemas",
    "description",
    "else",
    "enum",
    "examples",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "format",
    "help",
    "if",
    "items",
    "maxItems",
    "maxLength",
    "maximum",
    "minItems",
    "minLength",
    "minimum",
    "multipleOf",
    "not",
    "oneOf",
    "patternProperties",
    "pattern",
    "prefixItems",
    "properties",
    "propertyNames",
    "recommended",
    "required",
    "source",
    "then",
    "title",
    "type",
    "uniqueItemProperties",
    "uniqueItems",
}

# If a value carries one of these keys it is almost certainly a schema, so it can
# be reported as a property even when it sits outside a ``properties`` block.
SCHEMA_HINTS = (
    "$ref",
    "const",
    "enum",
    "format",
    "help",
    "items",
    "maximum",
    "minimum",
    "pattern",
    "properties",
    "recommended",
    "required",
    "source",
    "type",
)

BRANCH_KEYWORDS = ("allOf", "anyOf", "oneOf")


def resolve_schema_refs(
    schema: Dict[str, Any],
    schema_dir: Path,
    loaded_schemas: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Recursively inline every ``$ref`` in ``schema``."""
    if loaded_schemas is None:
        loaded_schemas = {}

    def resolve_value(value: Any) -> Any:
        if isinstance(value, dict):
            if "$ref" in value:
                ref = value["$ref"]
                file_ref, _, json_ptr = ref.partition("#")

                ref_path = schema_dir / file_ref
                if str(ref_path) not in loaded_schemas:
                    try:
                        with open(ref_path, "r", encoding="utf-8") as handle:
                            loaded_schemas[str(ref_path)] = json.load(handle)
                    except FileNotFoundError:
                        print(f"  ! Could not find referenced schema {ref_path}")
                        return value

                ref_schema = loaded_schemas[str(ref_path)]

                for part in [p for p in json_ptr.strip("/").split("/") if p]:
                    if isinstance(ref_schema, dict) and part in ref_schema:
                        ref_schema = ref_schema[part]
                    else:
                        print(f"  ! Invalid JSON pointer {ref} in {file_ref}")
                        return value

                resolved = resolve_value(ref_schema)
                if isinstance(resolved, dict):
                    # Keep any sibling keywords declared alongside the $ref.
                    merged = dict(resolved)
                    merged.update({k: resolve_value(v) for k, v in value.items() if k != "$ref"})
                    return merged
                return resolved

            return {key: resolve_value(val) for key, val in value.items()}
        if isinstance(value, list):
            return [resolve_value(item) for item in value]
        return value

    return resolve_value(schema)


def _flatten(value: Any) -> str:
    """Render a schema value as a single-line, TSV-safe string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ", ".join(_flatten(item) for item in value)
    return " ".join(str(value).split())


def _type_label(node: Dict[str, Any]) -> str:
    node_type = node.get("type")
    if isinstance(node_type, list):
        return " | ".join(str(item) for item in node_type)
    return _flatten(node_type)


def _is_property_like(value: Any) -> bool:
    """Is ``value`` plausibly a schema declared outside a ``properties`` block?"""
    if isinstance(value, str):
        return True
    if isinstance(value, dict):
        return any(hint in value for hint in SCHEMA_HINTS)
    return False


def _is_property_definition(value: Any) -> bool:
    """Is ``value`` usable as a property definition inside a ``properties`` block?

    Anything listed under ``properties`` is a property by definition, so dicts
    (including bare objects whose children use the shorthand form) and the
    ``"field": "string"`` shorthand are both accepted. Lists are not: they are
    almost always a ``required`` array that has been nested by mistake.
    """
    return isinstance(value, (str, dict))


def _label(condition: str, branch: str) -> str:
    return f"{condition} > {branch}" if condition else branch


def _collect_containers(
    node: Any,
    condition: str,
    containers: List[Tuple[Dict[str, Any], str]],
    visited: Set[int],
) -> None:
    """Gather every sub-schema that describes children of ``node``.

    For an array this includes the ``items`` schema, and for a conditional it
    includes the ``then``/``else``/``allOf``/``anyOf``/``oneOf`` branches. The
    ``if`` schema is deliberately skipped: it constrains the branch rather than
    introducing new properties.
    """
    if not isinstance(node, dict) or id(node) in visited:
        return
    visited.add(id(node))
    containers.append((node, condition))

    _collect_containers(node.get("items"), condition, containers, visited)
    _collect_containers(node.get("then"), _label(condition, "if/then"), containers, visited)
    _collect_containers(node.get("else"), _label(condition, "if/else"), containers, visited)

    for keyword in BRANCH_KEYWORDS:
        branches = node.get(keyword)
        if not isinstance(branches, list):
            continue
        for index, branch in enumerate(branches):
            _collect_containers(branch, _label(condition, f"{keyword}[{index}]"), containers, visited)


def _iter_children(
    node: Dict[str, Any],
    condition: str,
    path: str,
    warn: Callable[[str], None],
) -> Iterator[Tuple[str, Any, bool, str, bool]]:
    """Yield ``(name, schema, is_required, condition, declared_outside_properties)``."""
    containers: List[Tuple[Dict[str, Any], str]] = []
    _collect_containers(node, condition, containers, set())

    # ``required`` may name a property that a sibling branch declares, so the
    # cross-check is done against the names known to the whole node.
    known_names: Set[str] = set()
    required_names_seen: Set[str] = set()

    for container, container_condition in containers:
        declared_required = container.get("required")
        required_names = set(declared_required) if isinstance(declared_required, list) else set()
        required_names_seen |= required_names

        properties = container.get("properties")
        if isinstance(properties, dict):
            for name, sub_schema in properties.items():
                if not _is_property_definition(sub_schema):
                    warn(f"{path}.{name} is inside 'properties' but is not a schema; skipped")
                    continue
                known_names.add(name)
                yield name, sub_schema, name in required_names, container_condition, False

        for name, sub_schema in container.items():
            if name in SCHEMA_KEYWORDS or not _is_property_like(sub_schema):
                continue
            known_names.add(name)
            yield name, sub_schema, name in required_names, container_condition, True

    unknown_required = required_names_seen - known_names
    if unknown_required:
        names = ", ".join(sorted(unknown_required))
        warn(f"{path or '<root>'} lists required propert(ies) that are not defined: {names}")


def extract_schema_metadata(
    schema: Dict[str, Any],
    warn: Callable[[str], None] = lambda message: None,
) -> List[Dict[str, Any]]:
    """Flatten ``schema`` into one row per property."""
    rows: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str]] = set()

    def visit(
        name: str,
        node: Any,
        parent: str,
        path: str,
        is_required: bool,
        condition: str,
        inherited_source: Any,
        declared_outside_properties: bool,
    ) -> None:
        if isinstance(node, str):
            # Shorthand form, e.g. "assembly_id": "string".
            node = {"type": node}
        if not isinstance(node, dict):
            return

        key = (path, condition)
        if key in seen:
            return
        seen.add(key)

        if declared_outside_properties:
            warn(f"{path} is declared outside a 'properties' block")

        source = node.get("source", inherited_source)
        enum_values = node.get("enum")
        if enum_values is None and "const" in node:
            enum_values = [node["const"]]

        if is_required:
            required = "Conditional" if condition else "Yes"
        else:
            required = "No"

        rows.append(
            {
                "property_name": name,
                "parent_property": parent,
                "path": path,
                "type": _type_label(node),
                "description": _flatten(node.get("help") or node.get("description")),
                "enum": _flatten(enum_values),
                "pattern": _flatten(node.get("pattern")),
                "required": required,
                "recommended": "Yes" if node.get("recommended") else "No",
                "source": _flatten(source),
                "condition": condition,
            }
        )

        for child in _iter_children(node, condition, path, warn):
            child_name, child_schema, child_required, child_condition, child_implicit = child
            visit(
                child_name,
                child_schema,
                name,
                f"{path}.{child_name}",
                child_required,
                child_condition,
                source,
                child_implicit,
            )

    for child in _iter_children(schema, "", "", warn):
        name, sub_schema, required, condition, implicit = child
        visit(name, sub_schema, "", name, required, condition, schema.get("source"), implicit)

    return rows


def write_tsv_file(filepath: Path, rows: List[Dict[str, Any]]) -> None:
    """Write ``rows`` to ``filepath`` as a TSV."""
    with open(filepath, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def process_json_files(input_dir: Path, output_dir: Path) -> None:
    """Convert every ``*_schema.json`` in ``input_dir`` into a TSV."""
    output_dir.mkdir(parents=True, exist_ok=True)

    json_files = sorted(input_dir.glob("*_schema.json"))
    if not json_files:
        print(f"No JSON schema files found in {input_dir}")
        return

    print(f"Found {len(json_files)} JSON files to process\n")

    for json_file in json_files:
        print(f"Processing: {json_file.name}")

        try:
            with open(json_file, "r", encoding="utf-8") as handle:
                schema = json.load(handle)
        except json.JSONDecodeError as error:
            print(f"  x Error decoding JSON: {error}")
            continue

        warnings: List[str] = []
        schema = resolve_schema_refs(schema, input_dir)
        rows = extract_schema_metadata(schema, warnings.append)

        for warning in dict.fromkeys(warnings):
            print(f"  ! {warning}")

        if not rows:
            print("  - No top-level properties, skipping (shared definitions are inlined elsewhere)")
            continue

        output_path = output_dir / (json_file.stem.replace("_schema", "") + ".tsv")
        write_tsv_file(output_path, rows)
        print(f"  + Written to: {output_path.name} ({len(rows)} rows)")

    print(f"\nConversion complete! TSV files written to: {output_dir}")


def main() -> None:
    script_dir = Path(__file__).parent

    print("=" * 60)
    print("JSON to TSV Converter for Genome Note Schemas")
    print("=" * 60)
    print()

    process_json_files(script_dir, script_dir / "tsv_output")


if __name__ == "__main__":
    main()
