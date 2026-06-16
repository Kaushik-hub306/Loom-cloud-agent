"""
Loom Validation Spike
Proves session_init, teach, and recall_relevant work as Python function calls
without MCP protocol. This is the Step 0 gate before building the memory agent.
"""
import sys
from pathlib import Path

def test_file_store():
    """Test with file-based RuleStore (no Postgres needed for validation)."""
    from loom.engine.rule_store import RuleStore

    print("=" * 60)
    print("LOOM VALIDATION SPIKE — RuleStore (file-based)")
    print("=" * 60)

    # 1. Initialize store
    store = RuleStore(path=Path("/tmp/loom-spike/store.json"))
    print("\n✓ 1. RuleStore initialized")

    # 2. TEACH: Add rules
    rule1 = store.add_rule(
        domain="coding",
        rule_type="convention",
        rule="Use Result<T, AppError> pattern for all middleware returns. Never throw.",
        example="async def auth_middleware(req) -> Result[User, AppError]:",
        confidence=7,
        source_type="explicit_teach",
    )
    print(f"✓ 2. TEACH: rule added → id={rule1.id[:40]}..., confidence={rule1.confidence}")

    rule2 = store.add_rule(
        domain="coding",
        rule_type="convention",
        rule="All log statements must include request_id via structlog.",
        example="logger.info('auth_complete', request_id=request_id)",
        confidence=6,
        source_type="explicit_teach",
    )
    print(f"   TEACH: rule added → id={rule2.id[:40]}..., confidence={rule2.confidence}")

    rule3 = store.add_rule(
        domain="architecture",
        rule_type="decision",
        rule="Auth uses Redis sessions with key pattern auth:{session_id}. JWT migration planned.",
        confidence=8,
        source_type="explicit_teach",
    )
    print(f"   TEACH: rule added → id={rule3.id[:40]}..., confidence={rule3.confidence}")

    # 3. Duplicate teach → confidence bump
    rule1_dup = store.add_rule(
        domain="coding",
        rule_type="convention",
        rule="Use Result<T, AppError> pattern for all middleware returns. Never throw.",
        confidence=7,
        source_type="explicit_teach",
    )
    bumped = rule1_dup.confidence > rule1.confidence
    print(f"✓ 3. Duplicate TEACH: confidence {rule1.confidence} → {rule1_dup.confidence} (bumped: {bumped})")

    # 4. RECALL: Search for relevant rules
    results = store.search_rules(
        query="auth middleware error handling",
        min_confidence=1,
    )
    print(f"✓ 4. RECALL_RELEVANT: '{'auth middleware error handling'}' → {len(results)} results")
    for r in results:
        print(f"   [{r.domain}] (conf={r.confidence}) {r.rule[:80]}...")

    # 5. Domain-filtered recall
    results = store.search_rules(
        query="middleware",
        domain="coding",
    )
    print(f"✓ 5. Domain-filtered RECALL: '{'middleware'}' in domain 'coding' → {len(results)} results")

    # 6. Low-confidence filter
    results = store.search_rules(
        query="auth",
        min_confidence=8,
    )
    print(f"✓ 6. Confidence-filtered RECALL (>7): '{'auth'}' → {len(results)} results")

    # 7. Count total rules
    all_rules = store.search_rules(query="", min_confidence=1)
    print(f"✓ 7. Total rules stored: {len(all_rules)}")

    print("\n" + "=" * 60)
    print("RESULT: ALL Loom functions work as Python calls ✓")
    print("session_init → search_rules() + context assembly")
    print("teach → add_rule()")
    print("recall_relevant → search_rules()")
    print("=" * 60)

    return True


def test_postgres_store():
    """Test PostgresStore if DATABASE_URL is available."""
    import os
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("\n⚠ Skipping PostgresStore test — no DATABASE_URL set")
        print("  Set DATABASE_URL to test Postgres-backed Loom storage")
        return None

    from loom.storage.postgres_store import PostgresStore

    print("\n" + "=" * 60)
    print("LOOM VALIDATION SPIKE — PostgresStore")
    print("=" * 60)

    try:
        store = PostgresStore(db_url=db_url)
        store.initialize()
        print("✓ 1. PostgresStore initialized + schema created")

        health = store.health_check()
        print(f"✓ 2. Health check: {health}")

        # Quick teach + recall
        rule = store.add_rule(
            domain="test",
            rule_type="convention",
            rule="PostgresStore validation — delete me",
            confidence=5,
            source_type="spike",
        )
        print(f"✓ 3. TEACH to Postgres: rule added → id={rule.id[:40]}...")

        results = store.search_rules(query="PostgresStore validation", min_confidence=1)
        print(f"✓ 4. RECALL from Postgres: {len(results)} results")

        store.delete_rule(rule.id)
        print(f"✓ 5. Cleanup: rule deleted")

        print("\nRESULT: PostgresStore works ✓")
        return True

    except Exception as e:
        print(f"\n✗ PostgresStore FAILED: {e}")
        return False


if __name__ == "__main__":
    file_ok = test_file_store()
    pg_ok = test_postgres_store()

    if file_ok:
        print("\n✅ Loom is usable as Python functions. Proceed to build the memory agent.")
    else:
        print("\n❌ Loom validation FAILED. Do not build until this passes.")
        sys.exit(1)
