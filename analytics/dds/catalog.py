"""Load the analytical contract and render its PostgreSQL schema."""

import json
import re
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name("catalog.json")
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*\Z")
SQL_TYPES = {"bigint", "boolean", "date", "integer", "text", "timestamptz", "uuid"}
MANAGED_COLUMNS = {
    "row_hash": ("text", False, "Хеш выбранных значений строки DDS для определения изменений."),
    "loaded_at": ("timestamptz", False, "Момент последней записи версии строки в DDS."),
    "deleted_at": (
        "timestamptz",
        True,
        "Момент обнаружения исчезновения из выбранного среза (удаление или отзыв видимости). NULL у активной строки.",
    ),
}
SENSITIVE_COLUMNS = {
    "content",
    "custom_attributes",
    "email",
    "phone_number",
    "activation_code",
    "recipient_hash",
    "feedback_message",
    "source_event_keys",
    "attachment",
    "attachments",
}


def _unique_fields(pairs):
    fields = {}
    for name, value in pairs:
        if name in fields:
            raise ValueError(f"Duplicate catalog field: {name}")
        fields[name] = value
    return fields


def load_catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"), object_pairs_hook=_unique_fields)


def _validate_relationship(source, table, relationship, tables, errors):
    target = relationship["target_table"]
    target_table = tables.get(target, {})
    target_columns = relationship["target_columns"]
    local_columns = relationship["columns"]
    label = f"{source} relationship to {target}.{','.join(target_columns)}"
    if target_columns != target_table.get("primary_key"):
        errors.append(f"{label} must reference the target primary key")
    if len(local_columns) != len(target_columns):
        errors.append(f"{label} has mismatched key arity")
    for local, remote in zip(local_columns, target_columns, strict=False):
        left = table["columns"].get(local, {}).get("type")
        right = target_table.get("columns", {}).get(remote, {}).get("type")
        if left is None or left != right:
            errors.append(f"{label} has incompatible column types")
    if relationship["cardinality"] != "many-to-one" or not relationship["description"]:
        errors.append(f"{label} must document a many-to-one join")


def _validate(catalog: dict) -> list[str]:
    errors = []
    tables = catalog["tables"]
    if catalog["version"] != 1 or not tables:
        errors.append("Expected a nonempty version 1 catalog")
    for name, table in tables.items():
        columns = table["columns"]
        keys = table["primary_key"]
        if not IDENTIFIER.fullmatch(name) or not table["grain"] or not table["source"]:
            errors.append(f"{name} requires a valid identifier, grain and source")
        if (
            not keys
            or len(keys) != len(set(keys))
            or any(key not in columns or columns[key]["nullable"] for key in keys)
        ):
            errors.append(f"{name} requires a valid non-null primary key")
        account_id = columns.get("account_id", {})
        if account_id.get("type") != "bigint" or account_id.get("nullable") is not False:
            errors.append(f"{name}.account_id must be non-null bigint")
        for column, spec in columns.items():
            if not IDENTIFIER.fullmatch(column) or (
                column in MANAGED_COLUMNS or column in SENSITIVE_COLUMNS
            ):
                errors.append(f"{name}.{column} is not an allowed analytical column")
            if spec["type"] not in SQL_TYPES or not isinstance(spec["nullable"], bool):
                errors.append(f"{name}.{column} has invalid type or nullability")
            for property_name in ("description", "transformation", "null_meaning", "history"):
                if not spec.get(property_name):
                    errors.append(f"{name}.{column} requires {property_name}")
        for relationship in table["relationships"]:
            _validate_relationship(name, table, relationship, tables, errors)

    requirements = catalog["requirements"]
    if sorted(item["id"] for item in requirements) != list(range(2, 107)):
        errors.append("Expected exactly the 105 approved mapping identifiers")
    sources = [item["source_requirement"] for item in requirements]
    if not all(sources) or len(sources) != len(set(sources)):
        errors.append("Each mapping must identify a unique source requirement")
    for item in requirements:
        if not item["targets"]:
            errors.append(f"{item['source_requirement']} requires a DDS target")
        for target in item["targets"]:
            table, _, column = target.partition(".")
            if column not in tables.get(table, {}).get("columns", {}):
                errors.append(f"{item['source_requirement']} references missing {target}")
    return errors


def validate_catalog() -> list[str]:
    try:
        return _validate(load_catalog())
    except (KeyError, OSError, TypeError, ValueError, AttributeError) as error:
        return [f"Invalid DDS catalog: {error}"]


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _column_sql(name: str, sql_type: str, nullable: bool) -> str:
    return f"    {name} {sql_type}" + ("" if nullable else " NOT NULL")


def schema_sql() -> str:
    """Generate idempotent DDL and comments from the analytical contract."""
    catalog = load_catalog()
    if errors := _validate(catalog):
        raise ValueError("\n".join(errors))
    statements = ["CREATE SCHEMA IF NOT EXISTS dds;"]
    for name, table in catalog["tables"].items():
        qualified = f"dds.{name}"
        columns = [
            _column_sql(column, spec["type"], spec["nullable"])
            for column, spec in table["columns"].items()
        ]
        columns.extend(
            _column_sql(column, sql_type, nullable)
            for column, (sql_type, nullable, _) in MANAGED_COLUMNS.items()
        )
        columns.append(f"    PRIMARY KEY ({', '.join(table['primary_key'])})")
        definitions = ",\n".join(columns)
        statements.append(f"CREATE TABLE IF NOT EXISTS {qualified} (\n{definitions}\n);")
        table_comment = _literal(f"{table['grain']} Источник — {table['source']}.")
        statements.append(f"COMMENT ON TABLE {qualified} IS {table_comment};")
        for column, spec in table["columns"].items():
            comment = _literal(f"{spec['description']} {spec['null_meaning']}")
            statements.append(f"COMMENT ON COLUMN {qualified}.{column} IS {comment};")
        for column, (_, _, description) in MANAGED_COLUMNS.items():
            statements.append(f"COMMENT ON COLUMN {qualified}.{column} IS {_literal(description)};")
    return "\n\n".join(statements) + "\n"
