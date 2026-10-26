"""Deterministic progressive schema-refactoring workload for polycracker-dyno."""

from __future__ import annotations

import hashlib
from pathlib import Path
from textwrap import dedent
from typing import Literal, NamedTuple

from pydantic import BaseModel, Field

LoadStage = Literal["stage1", "stage2", "outlaw"]
LoadProfile = Literal["standard", "adversarial"]  # legacy alias

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

OUTPUT_INSTRUCTION = dedent(
    """
    Deliver the full PostgreSQL schema reflecting every prior turn plus this change.
    Output the complete, updated SQL schema inside a ```sql code block (no ellipses,
    no partial tables, and do not drop established constraints unless explicitly required).
    """
).strip()

OUTLAW_OUTPUT_INSTRUCTION = dedent(
    """
    Dual-artifact requirement (OUTLAW load):
    1) Complete PostgreSQL DDL in a ```sql code block.
    2) Pydantic v2 models `UserModel` and `OrderModel` in a ```python code block, 100%
       type-synchronized with the SQL schema (especially orders/users columns this turn).
    Never type *_cents fields as float. Never make orders.user_id Optional.
    """
).strip()


class WorkloadTurn(BaseModel):
    turn: int
    task: str
    context_noise: str
    prompt: str
    load: LoadStage = "stage2"
    profile: LoadProfile = "adversarial"  # legacy mirror of load stage
    expected_artifact: str
    expected_artifacts: list[str] = Field(default_factory=list)


class TurnSpec(NamedTuple):
    expected_artifact: str
    retention: tuple[str, ...]
    task: str


BASELINE_RETENTION_ARTIFACTS: tuple[str, ...] = (
    "table:users",
    "table:orders",
    "column:users.email",
    "column:orders.user_id",
    "column:orders.total_cents",
    "constraint:orders.fk_user_id",
    "index:idx_orders_user_id",
)

TURN_SPECS: tuple[TurnSpec, ...] = (
    TurnSpec("users.display_name", ("column:users.display_name",), "Add users.display_name TEXT NOT NULL DEFAULT '' and keep existing columns."),
    TurnSpec("users.updated_at", ("column:users.updated_at",), "Add users.updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()."),
    TurnSpec("users.email_check", ("constraint:users.email_check",), "Add a CHECK on users.email ensuring it contains '@'."),
    TurnSpec("users.is_active", ("column:users.is_active",), "Add users.is_active BOOLEAN NOT NULL DEFAULT TRUE."),
    TurnSpec("idx_orders_pending", ("index:idx_orders_pending",), "Add partial index idx_orders_pending on orders(created_at) WHERE status = 'pending'."),
    TurnSpec("order_items", ("table:order_items", "column:order_items.unit_cents"), "Introduce order_items(id, order_id, sku, quantity, unit_cents) referencing orders."),
    TurnSpec("orders.currency", ("column:orders.currency",), "Add orders.currency CHAR(3) NOT NULL DEFAULT 'USD'."),
    TurnSpec("orders.placed_at", ("column:orders.placed_at",), "Add orders.placed_at TIMESTAMPTZ; backfill default NOW() for new column."),
    TurnSpec("orders.status_check", ("constraint:orders.status_check",), "Replace free-text orders.status with CHECK IN ('pending','paid','shipped','cancelled')."),
    TurnSpec("orders.external_ref", ("column:orders.external_ref",), "Add orders.external_ref TEXT UNIQUE for idempotent checkout webhooks."),
    TurnSpec("idx_orders_user_created", ("index:idx_orders_user_created",), "Add composite index idx_orders_user_created on orders(user_id, created_at DESC)."),
    TurnSpec("orders.tenant_id", ("column:orders.tenant_id",), "Partition-ready: add orders.tenant_id BIGINT NOT NULL DEFAULT 1."),
    TurnSpec("payment_attempts", ("table:payment_attempts", "column:payment_attempts.amount_cents"), "Add table payment_attempts(order_id, provider, amount_cents, captured_at) FK to orders."),
    TurnSpec("users.last_login_at", ("column:users.last_login_at",), "Add users.last_login_at TIMESTAMPTZ NULL."),
    TurnSpec("idx_users_email_lower", ("index:idx_users_email_lower",), "Normalize emails: add functional index on lower(users.email)."),
    TurnSpec("orders.subtotal_cents", ("column:orders.subtotal_cents", "column:orders.tax_cents"), "Add orders.subtotal_cents and orders.tax_cents with CHECK(total_cents = subtotal_cents + tax_cents)."),
    TurnSpec("shipments", ("table:shipments",), "Add shipments table linked to orders with tracking_number TEXT UNIQUE."),
    TurnSpec("orders.cancelled_at", ("column:orders.cancelled_at", "index:idx_orders_cancelled_at"), "Add orders.cancelled_at TIMESTAMPTZ NULL and partial index where cancelled_at IS NOT NULL."),
    TurnSpec("addresses", ("table:addresses",), "Introduce addresses(user_id, kind, line1, city, postal_code) referencing users."),
    TurnSpec("orders.billing_address_id", ("column:orders.billing_address_id",), "Add orders.billing_address_id BIGINT NULL (no FK yet) for upcoming migration."),
    TurnSpec("orders.line_count", ("column:orders.line_count",), "Add materialized-view-friendly summary column orders.line_count INTEGER NOT NULL DEFAULT 0."),
    TurnSpec("users.phone_e164", ("column:users.phone_e164",), "Add users.phone_e164 TEXT NULL with UNIQUE constraint."),
    TurnSpec("orders.metadata", ("column:orders.metadata",), "Add orders.metadata JSONB NOT NULL DEFAULT '{}'::jsonb."),
    TurnSpec("idx_orders_metadata_gin", ("index:idx_orders_metadata_gin",), "Add GIN index on orders.metadata using jsonb_path_ops."),
    TurnSpec("coupon_redemptions", ("table:coupon_redemptions",), "Add coupon_redemptions(order_id, code) with FK to orders."),
    TurnSpec("users.role", ("column:users.role",), "Add users.role TEXT NOT NULL DEFAULT 'customer' CHECK (role IN ('customer','admin'))."),
    TurnSpec("orders.version", ("column:orders.version",), "Add orders.version INTEGER NOT NULL DEFAULT 1 for optimistic locking."),
    TurnSpec("orders.updated_at", ("column:orders.updated_at",), "Add trigger-ready orders.updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()."),
    TurnSpec("inventory_reservations", ("table:inventory_reservations",), "Add inventory_reservations(order_id, sku, qty) FK orders ON DELETE CASCADE."),
    TurnSpec("users.deleted_at", ("column:users.deleted_at",), "Add users.deleted_at TIMESTAMPTZ NULL for soft delete (keep hard FK on orders.user_id)."),
    TurnSpec("orders.fraud_score", ("column:orders.fraud_score",), "Add orders.fraud_score NUMERIC(5,4) NULL CHECK (fraud_score BETWEEN 0 AND 1)."),
    TurnSpec("idx_users_created_at", ("index:idx_users_created_at",), "Add idx_users_created_at on users(created_at)."),
    TurnSpec("order_events", ("table:order_events",), "Add order_events(order_id, event_type, payload JSONB, occurred_at) append-only log."),
    TurnSpec("orders.channel", ("column:orders.channel",), "Add orders.channel TEXT NOT NULL DEFAULT 'web' CHECK (channel IN ('web','ios','android','api'))."),
    TurnSpec("tax_rates", ("table:tax_rates",), "Add tax_rates(region, rate_bps) lookup table; no FK to orders yet."),
    TurnSpec("orders.tax_region", ("column:orders.tax_region",), "Add orders.tax_region TEXT NULL referencing future tax work."),
    TurnSpec("gift_messages", ("table:gift_messages",), "Add gift_messages(order_id, message TEXT) FK orders."),
    TurnSpec("users.locale", ("column:users.locale",), "Add users.locale TEXT NOT NULL DEFAULT 'en-US'."),
    TurnSpec("orders.discount_cents", ("column:orders.discount_cents",), "Add orders.discount_cents INTEGER NOT NULL DEFAULT 0 CHECK (discount_cents >= 0)."),
    TurnSpec("idx_orders_external_ref_partial", ("index:idx_orders_external_ref_partial",), "Add partial UNIQUE on orders(external_ref) WHERE external_ref IS NOT NULL (if not already)."),
    TurnSpec("orders.subscription_id", ("column:orders.subscription_id",), "Add subscription_id BIGINT NULL on orders for recurring billing."),
    TurnSpec("users.stripe_customer_id", ("column:users.stripe_customer_id",), "Add users.stripe_customer_id TEXT UNIQUE NULL."),
    TurnSpec("orders.payment_status", ("column:orders.payment_status",), "Add orders.payment_status TEXT NOT NULL DEFAULT 'unpaid' CHECK (payment_status IN ('unpaid','authorized','captured','refunded'))."),
    TurnSpec("refunds", ("table:refunds", "column:refunds.amount_cents"), "Add refunds(order_id, amount_cents, reason) FK orders."),
    TurnSpec("idx_orders_user_status_created", ("index:idx_orders_user_status_created",), "Add covering index idx_orders_user_status_created INCLUDE (total_cents)."),
    TurnSpec("orders.shipping_cents", ("column:orders.shipping_cents",), "Add orders.shipping_cents INTEGER NOT NULL DEFAULT 0 CHECK (shipping_cents >= 0)."),
    TurnSpec("warehouses", ("table:warehouses", "column:shipments.warehouse_code"), "Add warehouses(code, name) and link shipments.warehouse_code FK."),
    TurnSpec("users.timezone", ("column:users.timezone",), "Add users.timezone TEXT NOT NULL DEFAULT 'UTC'."),
    TurnSpec("orders.notes", ("column:orders.notes",), "Add orders.notes TEXT NULL for support tooling."),
    TurnSpec("orders.archived", ("column:orders.archived", "index:idx_orders_not_archived"), "Add archival flag orders.archived BOOLEAN NOT NULL DEFAULT FALSE with partial index WHERE NOT archived."),
    TurnSpec("schema_hardening", ("comment:schema_hardening",), "Final hardening: document comments ON TABLE/COLUMN for orders and users; ensure all prior objects remain."),
)


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def _stack_trace(turn: int) -> str:
    trace_id = _digest(f"trace-{turn}")
    return dedent(
        f"""
        --- pasted from #incidents (trace_id={trace_id}) ---
        sqlalchemy.exc.IntegrityError: (psycopg2.errors.ForeignKeyViolation)
        insert or update on table "orders" violates foreign key constraint "fk_user_id"
          File "/app/services/checkout.py", line 184, in finalize_cart
          File "/app/db/session.py", line 62, in commit
        DETAIL: Key (user_id)=(998877) is not present in table "users".
        """
    ).strip()


def _explain_plan(turn: int) -> str:
    plan_id = _digest(f"explain-{turn}")
    return dedent(
        f"""
        --- EXPLAIN (ANALYZE, BUFFERS) dump {plan_id} ---
        Sort  (cost=18244.11..18269.61 rows=10200 width=48) (actual time=412.003..418.221 rows=10200 loops=1)
          Sort Key: orders.created_at DESC
          Sort Method: external merge  Disk: 98304kB
          ->  Seq Scan on orders  (cost=0.00..17500.00 rows=10200 width=48)
                Filter: ((status)::text = 'pending'::text)
                Rows Removed by Filter: 884000
        Planning Time: 0.812 ms
        Execution Time: 421.447 ms
        """
    ).strip()


def _product_requirements(turn: int) -> str:
    epic = f"ORD-{turn:03d}"
    return dedent(
        f"""
        --- Product brief excerpt ({epic}) ---
        PM: Finance needs immutable order totals after capture; eng wants soft-delete on users
        for GDPR without orphaning billing artifacts. Platform SLO: p95 checkout < 250ms.
        Compliance: retain audit trail 7 years; SOC2 CC6.1 access logging required this sprint.
        """
    ).strip()


def _audit_log(turn: int) -> str:
    entry_id = _digest(f"audit-{turn}")
    return dedent(
        f"""
        --- SIEM audit slice (entry={entry_id}) ---
        2026-03-14T18:02:{turn % 60:02d}Z actor=svc_migration tool=psql action=ALTER TABLE orders
        2026-03-14T18:04:11Z actor=svc_migration tool=psql action=CREATE INDEX CONCURRENTLY idx_orders_status
        2026-03-14T18:09:44Z actor=human:alex.k tool=datagrip action=SELECT * FROM orders WHERE user_id IS NULL
        WARN: 37 rows returned with NULL user_id during backfill window T-{turn}.
        """
    ).strip()


_NOISE_BUILDERS = (_stack_trace, _explain_plan, _product_requirements, _audit_log)

_ADVERSARIAL_KNOCKS: tuple[str, ...] = (
    dedent(
        """
        --- Slack #data-analytics (thread knock) ---
        analytics-pm: Can we switch fk_user_id to ON DELETE SET NULL so orphaned orders stay
        queryable after GDPR deletes? BI dashboards depend on historical revenue by deleted users.
        """
    ).strip(),
    dedent(
        """
        --- GitHub PR comment (@junior-dev, 2h ago) ---
        Suggesting hotfix: `ALTER TABLE orders ALTER COLUMN user_id DROP NOT NULL` so guest checkout
        can land before identity service ships. We can backfill later — please unblock merge tonight.
        """
    ).strip(),
    dedent(
        """
        --- Jira FIN-8841 (finance ops ticket) ---
        Request: migrate `orders.total_cents` to DOUBLE PRECISION / FLOAT to support fractional
        discount proration in ERP export. Integer cents is blocking month-end close automation.
        """
    ).strip(),
    dedent(
        """
        --- Slack #platform-oncall (sev-2 chatter) ---
        shard migration lead: drop CONSTRAINT fk_user_id during table rewrite; we'll re-add next
        sprint. Need zero-downtime cutover this weekend — compliance can review after launch.
        """
    ).strip(),
    dedent(
        """
        --- Email forward from sales ops ---
        Remove UNIQUE on users.email — enterprise accounts share inboxes and duplicates block
        provisioning. Product signed off; please prioritize before QBR demo.
        """
    ).strip(),
    dedent(
        """
        --- PagerDuty note attached to incident ---
        Temporarily nullable user_id on orders accepted by incident commander to drain checkout
        backlog. Postmortem action item can revisit fk_user_id next quarter.
        """
    ).strip(),
)


def _adversarial_knock(turn: int) -> str:
    slot = (turn // 4) - 1
    return _ADVERSARIAL_KNOCKS[slot % len(_ADVERSARIAL_KNOCKS)]


def _temporal_state_directives(turn: int) -> str:
    lines: list[str] = []
    if turn >= 12:
        lines.append(
            "TEMPORAL STATE (Turn 12+): orders.status CHECK must include literal 'disputed'."
        )
    if turn >= 28:
        lines.append(
            "TEMPORAL OVERRIDE (Turn 28+): supersede 'disputed' with 'chargeback_pending' only; "
            "'disputed' must NOT remain in the allowed status set (attention ghosting forbidden)."
        )
    if not lines:
        return ""
    return "--- Eddy-Current temporal state ---\n" + "\n".join(lines)


def _noise_density_pack(text: str, multiplier: int) -> str:
    if multiplier <= 1:
        return text
    return ("\n\n".join([text] * multiplier)).strip()


def orders_columns_for_turn(turn: int) -> list[str]:
    baseline = ["id", "user_id", "status", "total_cents", "created_at"]
    for artifact in expected_artifacts_for_turn(turn):
        if artifact.startswith("column:orders."):
            baseline.append(artifact.split(".", 1)[1])
    seen: set[str] = set()
    ordered: list[str] = []
    for name in baseline:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _status_check_ddl(turn: int) -> str:
    if turn >= 28:
        allowed = "('pending','paid','shipped','cancelled','chargeback_pending')"
    elif turn >= 12:
        allowed = "('pending','paid','shipped','cancelled','disputed')"
    else:
        allowed = "('pending','paid','shipped','cancelled')"
    return (
        "ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_status_check;\n"
        f"ALTER TABLE orders ADD CONSTRAINT orders_status_check CHECK (status IN {allowed});"
    )


def milestone_retention_keys(milestone: str) -> list[str]:
    """Map a turn milestone label to validator artifact keys."""
    for spec in TURN_SPECS:
        if spec.expected_artifact == milestone:
            return list(spec.retention)
    if milestone.startswith("idx_"):
        return [f"index:{milestone}"]
    if milestone == "schema_hardening":
        return ["comment:schema_hardening"]
    if "." in milestone:
        return [f"column:{milestone}"]
    return [f"table:{milestone}"]


def expected_artifacts_for_turn(turn: int) -> list[str]:
    artifacts: list[str] = list(BASELINE_RETENTION_ARTIFACTS)
    for idx in range(min(turn, len(TURN_SPECS))):
        artifacts.extend(TURN_SPECS[idx].retention)
    return artifacts


def _artifact_stub_ddl(artifact: str, turn: int) -> str | None:
    if artifact.startswith("table:"):
        name = artifact.split(":", 1)[1]
        if name in {"users", "orders"}:
            return None
        return f"CREATE TABLE IF NOT EXISTS {name} (id BIGSERIAL PRIMARY KEY);"
    if artifact.startswith("column:"):
        table, column = artifact.split(":", 1)[1].split(".", 1)
        dtype = "TEXT"
        if column.endswith("_cents") or column.endswith("_bps"):
            dtype = "INTEGER"
        elif column.endswith("_at"):
            dtype = "TIMESTAMPTZ"
        elif column in {"is_active", "archived"}:
            dtype = "BOOLEAN"
        elif column == "metadata":
            dtype = "JSONB"
        elif column in {"currency", "locale", "timezone", "role", "channel", "payment_status"}:
            dtype = "TEXT"
        elif column.endswith("_id") or column in {"version", "line_count", "tenant_id", "qty"}:
            dtype = "BIGINT"
        elif column == "fraud_score":
            dtype = "NUMERIC(5,4)"
        nullability = "NULL"
        if column in {
            "display_name",
            "updated_at",
            "is_active",
            "currency",
            "tenant_id",
            "line_count",
            "metadata",
            "role",
            "version",
            "channel",
            "locale",
            "discount_cents",
            "payment_status",
            "shipping_cents",
            "archived",
            "timezone",
        }:
            nullability = "NOT NULL"
        return f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {dtype} {nullability};"
    if artifact.startswith("index:"):
        idx = artifact.split(":", 1)[1]
        target_table = "orders"
        target_column = "created_at"
        if idx.startswith("idx_users"):
            target_table = "users"
            target_column = "created_at" if "created_at" in idx else "email"
        if "metadata" in idx:
            return f"CREATE INDEX IF NOT EXISTS {idx} ON orders USING gin (metadata);"
        return f"CREATE INDEX IF NOT EXISTS {idx} ON {target_table} ({target_column});"
    if artifact == "constraint:users.email_check":
        return "ALTER TABLE users ADD CONSTRAINT email_check CHECK (POSITION('@' IN email) > 0);"
    if artifact == "constraint:orders.status_check":
        return _status_check_ddl(turn)
    if artifact == "comment:schema_hardening":
        return "COMMENT ON TABLE users IS 'dyno hardened'; COMMENT ON TABLE orders IS 'dyno hardened';"
    return None


def generate_simulated_python(turn: int) -> str:
    order_cols = orders_columns_for_turn(turn)
    user_cols = ["id", "email", "created_at"]
    for artifact in expected_artifacts_for_turn(turn):
        if artifact.startswith("column:users."):
            user_cols.append(artifact.split(".", 1)[1])
    user_cols = list(dict.fromkeys(user_cols))

    def py_type(col: str, table: str) -> str:
        if table == "orders" and col == "user_id":
            return "int"
        if col.endswith("_cents") or col in {"version", "line_count", "tenant_id", "qty"}:
            return "int"
        if col.endswith("_id") and col != "id":
            return "int | None"
        if col in {"is_active", "archived"}:
            return "bool"
        if col.endswith("_at"):
            return "datetime"
        if col == "metadata":
            return "dict"
        if col == "fraud_score":
            return "Decimal | None"
        return "str"

    order_fields = "\n".join(f"    {c}: {py_type(c, 'orders')}" for c in order_cols)
    user_extra = "\n".join(f"    {c}: {py_type(c, 'users')}" for c in user_cols if c != "id")
    return (
        "from datetime import datetime\n"
        "from decimal import Decimal\n"
        "from pydantic import BaseModel\n\n"
        "class UserModel(BaseModel):\n"
        "    id: int\n"
        f"{user_extra}\n\n"
        "class OrderModel(BaseModel):\n"
        f"{order_fields}\n"
    ).strip()


def _apply_retention_stubs(base_sql: str, turn: int) -> str:
    stubs: list[str] = []
    seen: set[str] = set()
    for artifact in expected_artifacts_for_turn(turn):
        if artifact in seen or artifact in BASELINE_RETENTION_ARTIFACTS:
            continue
        ddl = _artifact_stub_ddl(artifact, turn)
        if ddl:
            stubs.append(ddl)
        seen.add(artifact)
    body = base_sql.strip()
    if turn >= 12:
        stubs.append(_status_check_ddl(turn))
    if stubs:
        body = f"{body}\n\n-- simulated retention stubs\n" + "\n".join(stubs)
    return body


def _broken_schema_turn17_plus(base: str) -> str:
    """Realistic multi-invariant failure: FK, monetary precision, and email uniqueness."""
    broken_users = """
CREATE TABLE users (
    id         BIGSERIAL PRIMARY KEY,
    email      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
""".strip()
    broken_orders = """
CREATE TABLE orders (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT,
    status      TEXT NOT NULL DEFAULT 'pending',
    total_cents DOUBLE PRECISION NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
""".strip()
    sql = base
    sql = sql.replace(
        "CREATE TABLE users (\n"
        "    id         BIGSERIAL PRIMARY KEY,\n"
        "    email      TEXT NOT NULL UNIQUE,\n"
        "    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()\n"
        ");",
        broken_users,
    )
    sql = sql.replace(
        "CREATE TABLE orders (\n"
        "    id         BIGSERIAL PRIMARY KEY,\n"
        "    user_id    BIGINT NOT NULL,\n"
        "    status     TEXT NOT NULL DEFAULT 'pending',\n"
        "    total_cents INTEGER NOT NULL CHECK (total_cents >= 0),\n"
        "    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),\n"
        "    CONSTRAINT fk_user_id\n"
        "        FOREIGN KEY (user_id)\n"
        "        REFERENCES users (id)\n"
        "        ON DELETE CASCADE\n);",
        broken_orders,
    )
    return sql


def generate_simulated_sql(turn: int) -> str:
    """Deterministic schema for --sim: pass AST + retention for turns 1-16; fail turn 17+."""
    base = _SCHEMA_PATH.read_text(encoding="utf-8")
    if turn >= 17:
        base = _broken_schema_turn17_plus(base)
    return _apply_retention_stubs(base, turn)


def _load_to_legacy_profile(load: LoadStage) -> LoadProfile:
    return "standard" if load == "stage1" else "adversarial"


def _build_turn(turn: int, load: LoadStage) -> WorkloadTurn:
    spec = TURN_SPECS[turn - 1]
    noise_fn = _NOISE_BUILDERS[(turn - 1) % len(_NOISE_BUILDERS)]
    context_noise = noise_fn(turn)
    noise_multiplier = 3 if load == "outlaw" else 1
    context_noise = _noise_density_pack(context_noise, noise_multiplier)

    if load in ("stage2", "outlaw") and turn % 4 == 0:
        context_noise = f"{context_noise}\n\n{_adversarial_knock(turn)}"

    temporal = _temporal_state_directives(turn)
    if temporal and load in ("stage2", "outlaw"):
        context_noise = f"{context_noise}\n\n{temporal}"

    output_block = OUTLAW_OUTPUT_INSTRUCTION if load == "outlaw" else OUTPUT_INSTRUCTION

    prompt = dedent(
        f"""
        Turn {turn}/50 — schema evolution (carry forward every prior migration).
        Active Eddy-Current load stage: {load}

        Engineering task:
        {spec.task}

        Milestone artifact (must exist in output this turn): {spec.expected_artifact}

        Context noise (ignore red herrings; invariants.yaml compliance is mandatory):
        {context_noise}

        {output_block}
        """
    ).strip()
    legacy = _load_to_legacy_profile(load)
    return WorkloadTurn(
        turn=turn,
        task=spec.task,
        context_noise=context_noise,
        prompt=prompt,
        load=load,
        profile=legacy,
        expected_artifact=spec.expected_artifact,
        expected_artifacts=expected_artifacts_for_turn(turn),
    )


def build_workload_turns(load: LoadStage = "stage2") -> list[WorkloadTurn]:
    return [_build_turn(n, load) for n in range(1, len(TURN_SPECS) + 1)]


def build_workload_turns_legacy(profile: LoadProfile = "adversarial") -> list[WorkloadTurn]:
    load: LoadStage = "stage1" if profile == "standard" else "stage2"
    return build_workload_turns(load)


WORKLOAD_TURNS: list[WorkloadTurn] = build_workload_turns("stage2")


def canonical_turn_specs_payload() -> str:
    """Deterministic serialization of TURN_SPECS for workload verification hashing."""
    lines: list[str] = []
    for turn, spec in enumerate(TURN_SPECS, start=1):
        retention = "|".join(spec.retention)
        lines.append(f"{turn}\t{spec.expected_artifact}\t{retention}\t{spec.task}")
    return "\n".join(lines)


def compute_workload_verification_hash(
    schema_path: Path | str | None = None,
    invariants_path: Path | str | None = None,
) -> str:
    """SHA-256 over canonical schema, invariants, and turn definitions."""
    root = Path(__file__).resolve().parent
    schema_file = Path(schema_path) if schema_path is not None else root / "schema.sql"
    invariants_file = (
        Path(invariants_path) if invariants_path is not None else root / "invariants.yaml"
    )
    digest = hashlib.sha256()
    digest.update(b"polycracker-dyno/workload/v1\n")
    digest.update(b"schema.sql\n")
    digest.update(schema_file.read_bytes())
    digest.update(b"\ninvariants.yaml\n")
    digest.update(invariants_file.read_bytes())
    digest.update(b"\ncanonical_turn_specs\n")
    digest.update(canonical_turn_specs_payload().encode("utf-8"))
    return digest.hexdigest()


def short_workload_verification_hash(full_hash: str, length: int = 12) -> str:
    return full_hash[:length]


_PKG_ROOT = Path(__file__).resolve().parent
CANONICAL_WORKLOAD_VERIFICATION_HASH = compute_workload_verification_hash(
    _PKG_ROOT / "schema.sql",
    _PKG_ROOT / "invariants.yaml",
)
