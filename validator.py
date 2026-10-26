"""AST-backed validation for polycracker-dyno invariants and workload retention."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Literal

LoadStage = Literal["stage1", "stage2", "outlaw"]

import sqlglot
import yaml
from pydantic import BaseModel, Field
from sqlglot import exp
from sqlglot.errors import ParseError

SQL_BLOCK_PATTERN = re.compile(r"```sql\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
PYTHON_BLOCK_PATTERN = re.compile(r"```python\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
STATUS_IN_CHECK_PATTERN = re.compile(
    r"status\s+in\s*\((.*?)\)",
    re.IGNORECASE | re.DOTALL,
)

MONETARY_COLUMNS = frozenset(
    {"total_cents", "amount_cents", "discount_cents", "shipping_cents", "unit_cents"}
)
FORBIDDEN_MONEY_TYPES = frozenset(
    {
        exp.DataType.Type.FLOAT,
        exp.DataType.Type.DOUBLE,
        exp.DataType.Type.DECIMAL,
        exp.DataType.Type.BIGDECIMAL,
        exp.DataType.Type.DECFLOAT,
        exp.DataType.Type.UDECIMAL,
        exp.DataType.Type.UDOUBLE,
    }
)
ALLOWED_MONEY_TYPES = frozenset(
    {
        exp.DataType.Type.BIGINT,
        exp.DataType.Type.INT,
        exp.DataType.Type.SMALLINT,
        exp.DataType.Type.TINYINT,
    }
)


class ValidationResult(BaseModel):
    passed: bool
    ast_valid: bool = False
    invariants_intact: bool = False
    retention_score: str = "0/0"
    violated_invariants: list[str] = Field(default_factory=list)
    diagnostic_reason: str = ""
    thermal_efficiency_ratio: float = 0.0
    token_multiplier: float = 0.0
    thermal_bloat: bool = False


class InvariantSpec(BaseModel):
    id: int
    name: str
    category: str | None = None
    scope: str
    rule: str
    enforcement: dict[str, Any] = Field(default_factory=dict)


class SchemaAST:
    def __init__(self, statements: list[exp.Expression]) -> None:
        self.statements = statements
        self.tables: dict[str, exp.Create] = {}
        self.indexes: set[str] = set()
        self.alter_columns: set[str] = set()
        self.alter_constraints: set[str] = set()
        self.has_comment: bool = False
        self._collect()

    def _collect(self) -> None:
        for stmt in self.statements:
            if isinstance(stmt, exp.Create):
                if stmt.kind == "TABLE":
                    table_name = _table_name(stmt.this)
                    if table_name:
                        self.tables[table_name.lower()] = stmt
                elif stmt.kind == "INDEX":
                    index = stmt.find(exp.Index)
                    if index and index.name:
                        self.indexes.add(index.name.lower())
            elif isinstance(stmt, exp.Alter):
                self._collect_alter(stmt)
            elif isinstance(stmt, exp.Comment):
                self.has_comment = True

    def _collect_alter(self, stmt: exp.Alter) -> None:
        table = _table_name(stmt.this)
        if not table:
            return
        table = table.lower()
        for action in stmt.args.get("actions") or []:
            if isinstance(action, exp.ColumnDef):
                self.alter_columns.add(f"column:{table}.{action.name.lower()}")
                continue
            constraints: list[exp.Constraint] = []
            if isinstance(action, exp.Constraint):
                constraints.append(action)
            else:
                constraints.extend(action.find_all(exp.Constraint))
            for constraint in constraints:
                if constraint.name:
                    self.alter_constraints.add(f"constraint:{table}.{constraint.name.lower()}")
                check = constraint.find(exp.Check)
                check_sql = check.sql().lower() if check else constraint.sql().lower()
                if table == "users" and "email" in check_sql and "@" in check_sql:
                    self.alter_constraints.add("constraint:users.email_check")
                if table == "orders" and "status" in check_sql:
                    self.alter_constraints.add("constraint:orders.status_check")


def _load_invariants(invariants_path: str) -> list[InvariantSpec]:
    path = Path(invariants_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw = data.get("invariants") or []
    return [InvariantSpec.model_validate(item) for item in raw]


def extract_sql_block(raw_response: str) -> str | None:
    matches = SQL_BLOCK_PATTERN.findall(raw_response)
    if not matches:
        return None
    return matches[-1].strip()


def extract_python_block(raw_response: str) -> str | None:
    matches = PYTHON_BLOCK_PATTERN.findall(raw_response)
    if not matches:
        return None
    return matches[-1].strip()


def compute_thermal_metrics(
    *,
    input_tokens: int,
    output_tokens: int,
    schema_sql: str | None,
) -> tuple[float, float, bool]:
    artifact_size = max(len(schema_sql or ""), 1)
    useful = max(output_tokens, 0)
    total_in = max(input_tokens, 1)
    thermal_efficiency_ratio = useful / total_in
    token_multiplier = total_in / artifact_size
    thermal_bloat = total_in > (50 * artifact_size)
    return thermal_efficiency_ratio, token_multiplier, thermal_bloat


def _status_literals_from_sql(sql: str) -> set[str]:
    literals: set[str] = set()
    for match in STATUS_IN_CHECK_PATTERN.finditer(sql.lower()):
        chunk = match.group(1)
        for token in re.findall(r"'([^']+)'", chunk):
            literals.add(token.lower())
    return literals


def _check_temporal_state_consistency(schema: SchemaAST, sql: str, turn: int, spec: InvariantSpec) -> list[str]:
    if turn < 12:
        return []
    literals = _status_literals_from_sql(sql)
    violations: list[str] = []
    if turn >= 12 and turn < 28:
        if "disputed" not in literals:
            violations.append(f"{spec.name}: turn {turn} requires 'disputed' in orders.status CHECK")
    if turn >= 28:
        if "chargeback_pending" not in literals:
            violations.append(f"{spec.name}: turn {turn} requires 'chargeback_pending' in orders.status CHECK")
        if "disputed" in literals:
            violations.append(f"{spec.name}: turn {turn} forbids ghost 'disputed' after supersession")
    return violations


def _annotation_type_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.Subscript):
        return ast.unparse(node)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return f"{_annotation_type_name(node.left)} | {_annotation_type_name(node.right)}"
    if isinstance(node, ast.Attribute):
        return ast.unparse(node)
    return ast.unparse(node)


def _model_fields(py_source: str, class_name: str) -> dict[str, str]:
    tree = ast.parse(py_source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            fields: dict[str, str] = {}
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields[stmt.target.id] = _annotation_type_name(stmt.annotation)
            return fields
    return {}


def _check_cross_artifact_type_sync(
    schema: SchemaAST,
    py_source: str | None,
    turn: int,
    spec: InvariantSpec,
) -> list[str]:
    violations: list[str] = []
    if py_source is None:
        return [f"{spec.name}: missing ```python block with OrderModel/UserModel"]

    from workload import orders_columns_for_turn

    try:
        ast.parse(py_source)
    except SyntaxError as exc:
        return [f"{spec.name}: python block syntax error: {exc}"]

    order_fields = _model_fields(py_source, "OrderModel")
    user_fields = _model_fields(py_source, "UserModel")
    if not order_fields:
        violations.append(f"{spec.name}: OrderModel missing or has no annotated fields")
    if not user_fields:
        violations.append(f"{spec.name}: UserModel missing or has no annotated fields")

    expected_cols = set(orders_columns_for_turn(turn))
    for col in expected_cols:
        if col not in order_fields:
            violations.append(f"{spec.name}: OrderModel missing orders.{col}")

    user_id_type = order_fields.get("user_id", "")
    if "Optional" in user_id_type or user_id_type.strip().endswith("| None"):
        violations.append(f"{spec.name}: OrderModel.user_id must not be Optional")

    for name, typ in order_fields.items():
        if name.endswith("_cents") and ("float" in typ.lower() or "Float" in typ):
            violations.append(f"{spec.name}: OrderModel.{name} must not be float")

    return violations


def _table_name(table_exp: exp.Expression | None) -> str | None:
    if table_exp is None:
        return None
    if isinstance(table_exp, exp.Schema):
        table_exp = table_exp.this
    if isinstance(table_exp, exp.Table):
        return table_exp.name
    if hasattr(table_exp, "name"):
        return table_exp.name
    return str(table_exp)


def _column_is_not_null(column: exp.ColumnDef) -> bool:
    for constraint in column.args.get("constraints") or []:
        kind = constraint.args.get("kind")
        if isinstance(kind, exp.NotNullColumnConstraint):
            return True
    return False


def _column_is_unique(column: exp.ColumnDef) -> bool:
    for constraint in column.args.get("constraints") or []:
        kind = constraint.args.get("kind")
        if isinstance(kind, exp.UniqueColumnConstraint):
            return True
    return False


def _table_has_unique_email(create_stmt: exp.Create) -> bool:
    schema = create_stmt.find(exp.Schema)
    if not schema:
        return False
    email_not_null = False
    email_unique = False
    for expr in schema.expressions:
        if isinstance(expr, exp.ColumnDef) and expr.name.lower() == "email":
            email_not_null = _column_is_not_null(expr)
            email_unique = _column_is_unique(expr)
        if isinstance(expr, exp.Constraint):
            unique = expr.find(exp.Unique)
            if unique:
                cols = [c.name.lower() for c in unique.find_all(exp.Column)]
                if "email" in cols:
                    email_unique = True
            check = expr.find(exp.Check)
            if check and "email" in check.sql().lower():
                email_not_null = email_not_null or True
    return email_not_null and email_unique


def _find_fk_user_id(create_stmt: exp.Create) -> exp.Constraint | None:
    schema = create_stmt.find(exp.Schema)
    if not schema:
        return None
    for expr in schema.expressions:
        if isinstance(expr, exp.Constraint) and expr.name and expr.name.lower() == "fk_user_id":
            return expr
    return None


def _fk_references_users_id(constraint: exp.Constraint) -> bool:
    fk = constraint.find(exp.ForeignKey)
    if not fk:
        return False
    reference = fk.args.get("reference")
    if not isinstance(reference, exp.Reference):
        return False
    ref_schema = reference.this
    if not isinstance(ref_schema, exp.Schema):
        return False
    ref_table = _table_name(ref_schema.this)
    ref_cols = [c.name.lower() for c in ref_schema.expressions if isinstance(c, exp.Identifier)]
    if ref_table != "users" or ref_cols != ["id"]:
        return False
    options = reference.args.get("options") or []
    return any(str(option).upper().replace("_", " ") == "ON DELETE CASCADE" for option in options)


def _column_dtype(column: exp.ColumnDef) -> exp.DataType.Type | None:
    kind = column.args.get("kind")
    if isinstance(kind, exp.DataType):
        return kind.this
    return None


def _is_integer_money_type(dtype: exp.DataType.Type | None) -> bool:
    if dtype is None:
        return False
    if dtype in ALLOWED_MONEY_TYPES:
        return True
    # sqlglot may label INTEGER as UNKNOWN in some dialect paths; inspect rendered type.
    return False


def _check_preserve_fk_user_id(schema: SchemaAST, spec: InvariantSpec) -> list[str]:
    violations: list[str] = []
    orders = schema.tables.get("orders")
    if orders is None:
        return [f"{spec.name}: missing CREATE TABLE orders"]

    user_id_col = None
    order_schema = orders.find(exp.Schema)
    if order_schema:
        for expr in order_schema.expressions:
            if isinstance(expr, exp.ColumnDef) and expr.name.lower() == "user_id":
                user_id_col = expr
                break

    if user_id_col is None:
        violations.append(f"{spec.name}: orders.user_id column missing")
    elif not _column_is_not_null(user_id_col):
        violations.append(f"{spec.name}: orders.user_id is nullable")

    fk = _find_fk_user_id(orders)
    if fk is None:
        violations.append(f"{spec.name}: CONSTRAINT fk_user_id missing on orders")
    elif not _fk_references_users_id(fk):
        violations.append(
            f"{spec.name}: fk_user_id must reference users(id) ON DELETE CASCADE"
        )
    return violations


def _check_preserve_monetary_integer_precision(schema: SchemaAST, spec: InvariantSpec) -> list[str]:
    violations: list[str] = []
    for table_name, create_stmt in schema.tables.items():
        order_schema = create_stmt.find(exp.Schema)
        if not order_schema:
            continue
        for expr in order_schema.expressions:
            if not isinstance(expr, exp.ColumnDef):
                continue
            if expr.name.lower() not in MONETARY_COLUMNS:
                continue
            dtype = _column_dtype(expr)
            rendered = expr.kind.sql().upper() if isinstance(expr.kind, exp.DataType) else ""
            if dtype in FORBIDDEN_MONEY_TYPES or any(
                token in rendered for token in ("FLOAT", "REAL", "DOUBLE", "NUMERIC", "DECIMAL")
            ):
                violations.append(
                    f"{spec.name}: {table_name}.{expr.name} uses non-integer type ({rendered or dtype})"
                )
            elif not (_is_integer_money_type(dtype) or any(
                token in rendered for token in ("INT", "BIGINT", "SMALLINT", "INTEGER")
            )):
                violations.append(
                    f"{spec.name}: {table_name}.{expr.name} must use integer storage (found {rendered or dtype})"
                )
    return violations


def _check_preserve_users_email_uniqueness(schema: SchemaAST, spec: InvariantSpec) -> list[str]:
    users = schema.tables.get("users")
    if users is None:
        return [f"{spec.name}: missing CREATE TABLE users"]
    if not _table_has_unique_email(users):
        return [f"{spec.name}: users.email must be NOT NULL and UNIQUE"]
    return []


def _users_email_check_present(schema: SchemaAST) -> bool:
    users = schema.tables.get("users")
    if users is None:
        return False
    order_schema = users.find(exp.Schema)
    if not order_schema:
        return False
    for expr in order_schema.expressions:
        if isinstance(expr, exp.Constraint):
            check = expr.find(exp.Check)
            if check and "email" in check.sql().lower() and "@" in check.sql():
                return True
    return False


def _orders_status_check_present(schema: SchemaAST) -> bool:
    orders = schema.tables.get("orders")
    if orders is None:
        return False
    order_schema = orders.find(exp.Schema)
    if not order_schema:
        return False
    for expr in order_schema.expressions:
        if isinstance(expr, exp.Constraint):
            check = expr.find(exp.Check)
            if check and "status" in check.sql().lower():
                return True
    return False


def extract_schema_artifacts(schema: SchemaAST, statements: list[exp.Expression]) -> set[str]:
    found: set[str] = set()
    for table_name in schema.tables:
        found.add(f"table:{table_name}")
    for table_name, create_stmt in schema.tables.items():
        order_schema = create_stmt.find(exp.Schema)
        if not order_schema:
            continue
        for expr in order_schema.expressions:
            if isinstance(expr, exp.ColumnDef):
                found.add(f"column:{table_name}.{expr.name.lower()}")
            if isinstance(expr, exp.Constraint) and expr.name:
                found.add(f"constraint:{table_name}.{expr.name.lower()}")
    found.update(schema.alter_columns)
    found.update(schema.alter_constraints)
    for index_name in schema.indexes:
        found.add(f"index:{index_name.lower()}")
    if _users_email_check_present(schema):
        found.add("constraint:users.email_check")
    if _orders_status_check_present(schema):
        found.add("constraint:orders.status_check")
    if schema.has_comment:
        found.add("comment:schema_hardening")
    return found


def check_workload_retention(sql: str, expected_artifacts: list[str]) -> tuple[str, list[str]]:
    """Return retention score and missing artifacts using parsed DDL."""
    if not expected_artifacts:
        return "skipped", []

    try:
        statements = sqlglot.parse(sql, read="postgres")
    except ParseError:
        return f"0/{len(expected_artifacts)}", list(expected_artifacts)

    schema = SchemaAST(statements)
    present = extract_schema_artifacts(schema, statements)
    missing = [artifact for artifact in expected_artifacts if artifact not in present]
    kept = len(expected_artifacts) - len(missing)
    return f"{kept}/{len(expected_artifacts)}", missing


_INVARIANT_CHECKERS = {
    "preserve_fk_user_id": _check_preserve_fk_user_id,
    "preserve_monetary_integer_precision": _check_preserve_monetary_integer_precision,
    "preserve_users_email_uniqueness": _check_preserve_users_email_uniqueness,
}


def _build_diagnostic(violated: list[str]) -> str:
    if not violated:
        return "All invariants satisfied."
    if len(violated) == 1:
        return violated[0]
    return f"{len(violated)} violations: " + "; ".join(violated)


def validate_response(
    raw_response: str,
    invariants_path: str,
    expected_artifacts: list[str] | None = None,
    turn_milestone: str | None = None,
    *,
    turn_number: int = 1,
    load_stage: LoadStage = "stage2",
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> ValidationResult:
    invariants = _load_invariants(invariants_path)
    sql = extract_sql_block(raw_response)
    if sql is None:
        eff, mult, bloat = compute_thermal_metrics(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            schema_sql=None,
        )
        return ValidationResult(
            passed=False,
            ast_valid=False,
            invariants_intact=False,
            retention_score="0/0",
            violated_invariants=["schema_output"],
            diagnostic_reason="No ```sql code block found in model response.",
            thermal_efficiency_ratio=eff,
            token_multiplier=mult,
            thermal_bloat=bloat,
        )

    violated: list[str] = []
    ast_valid = False
    schema: SchemaAST | None = None

    try:
        statements = sqlglot.parse(sql, read="postgres")
        ast_valid = True
        schema = SchemaAST(statements)
    except ParseError as exc:
        violated.append(f"ast_syntax_validity: {exc}")
        retention_score, missing = check_workload_retention(sql, expected_artifacts or [])
        if missing:
            violated.append("workload_retention")
        diagnostic = _build_diagnostic(violated)
        if missing:
            diagnostic += f" Retention {retention_score} missing: {', '.join(missing[:5])}"
        return ValidationResult(
            passed=False,
            ast_valid=False,
            invariants_intact=False,
            retention_score=retention_score,
            violated_invariants=violated,
            diagnostic_reason=diagnostic,
        )

    assert schema is not None
    users_ok = schema.tables.get("users") is not None
    orders_ok = schema.tables.get("orders") is not None
    if not users_ok:
        violated.append("preserve_fk_user_id: missing CREATE TABLE users")
    if not orders_ok:
        violated.append("preserve_fk_user_id: missing CREATE TABLE orders")

    py_block = extract_python_block(raw_response)

    for spec in invariants:
        if spec.name == "ast_syntax_validity":
            continue
        if spec.name == "temporal_state_consistency":
            violated.extend(_check_temporal_state_consistency(schema, sql, turn_number, spec))
            continue
        if spec.name == "cross_artifact_type_sync":
            if load_stage == "outlaw":
                violated.extend(_check_cross_artifact_type_sync(schema, py_block, turn_number, spec))
            continue
        if spec.name == "thermal_efficiency":
            continue
        checker = _INVARIANT_CHECKERS.get(spec.name)
        if checker is None:
            continue
        violated.extend(checker(schema, spec))

    invariant_names = {
        spec.name
        for spec in invariants
        if spec.name not in {"ast_syntax_validity", "thermal_efficiency"}
    }
    invariant_violations = [v for v in violated if v.split(":", 1)[0] in invariant_names]
    invariants_intact = len(invariant_violations) == 0

    present = extract_schema_artifacts(schema, statements)
    if turn_milestone:
        from workload import milestone_retention_keys

        for key in milestone_retention_keys(turn_milestone):
            if key not in present:
                violated.append(f"workload_milestone: turn artifact '{turn_milestone}' missing ({key})")

    if expected_artifacts is None:
        retention_score, missing = "skipped", []
    else:
        retention_score, missing = check_workload_retention(sql, expected_artifacts)
    retention_ok = not missing
    if missing:
        violated.append("workload_retention")

    milestone_ok = not any(v.startswith("workload_milestone:") for v in violated)
    thermal_efficiency_ratio, token_multiplier, thermal_bloat = compute_thermal_metrics(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        schema_sql=sql,
    )
    passed = ast_valid and invariants_intact and retention_ok and milestone_ok
    diagnostic = _build_diagnostic([v for v in violated if v not in {"workload_retention", "thermal_efficiency: THERMAL BLOAT"}])
    if missing:
        preview = ", ".join(missing[:6])
        if len(missing) > 6:
            preview += ", ..."
        diagnostic += f" Retention {retention_score}; missing artifacts: {preview}"
    if thermal_bloat:
        diagnostic += (
            f" 🟡 THERMAL BLOAT: input/context {token_multiplier:.1f}x schema artifact size "
            f"(efficiency {thermal_efficiency_ratio:.4f})."
        )

    return ValidationResult(
        passed=passed,
        ast_valid=ast_valid,
        invariants_intact=invariants_intact,
        retention_score=retention_score,
        violated_invariants=violated,
        diagnostic_reason=diagnostic,
        thermal_efficiency_ratio=thermal_efficiency_ratio,
        token_multiplier=token_multiplier,
        thermal_bloat=thermal_bloat,
    )
