import sqlite3
import pytest
from simulation import DatabaseSimulator


@pytest.fixture
def db():
    """Spins up a fresh in-memory database seeded with test data before each test."""
    simulator = DatabaseSimulator(db_path=":memory:")
    simulator.setup_schema_and_seed(num_customers=100, num_orders=2000)
    return simulator


def test_n_plus_one_vs_eager_join_query_count(db):
    """Proves that N+1 executes 1 + N queries while INNER JOIN executes exactly 1 query."""
    metrics = db.simulate_n_plus_one(limit=50)

    assert metrics["bad_query_count"] == 51  # 1 initial query + 50 loop queries
    assert metrics["good_query_count"] == 1  # 1 combined JOIN query
    assert metrics["row_count"] == 50


def test_btree_index_switches_query_plan_from_scan_to_search(db):
    """Proves that creating a composite index changes SQLite's planner from Full Table Scan to B-Tree Search."""
    plans = db.simulate_indexing()

    assert "SCAN" in plans["plan_before"].upper()
    assert "SEARCH" in plans["plan_after"].upper()
    assert "IDX_ORDERS_STATUS_TOTAL" in plans["plan_after"].upper()


def test_acid_transaction_rollback_preserves_inventory_and_orders(db):
    """Proves that an unhandled exception mid-checkout rolls back both inventory deduction and order creation."""
    result = db.simulate_acid_transactions()

    assert result["final_stock"] == result["initial_stock"]
    assert result["final_orders"] == result["initial_orders"]


def test_check_constraint_prevents_negative_inventory(db):
    """Proves that database-level CHECK constraints block invalid negative inventory states."""
    cursor = db.conn.cursor()
    with pytest.raises(sqlite3.IntegrityError):
        cursor.execute("UPDATE products SET stock_count = -1 WHERE sku = 'SKU-100'")


def test_analytics_cte_excludes_soft_deleted_customers_and_ranks_correctly(db):
    """Proves that the analytics query excludes soft-deleted users and returns only Top 2 per tier."""
    results = db.simulate_analytics_query()
    cursor = db.conn.cursor()

    assert len(results) > 0
    for row in results:
        # 1. Rank must only be 1 or 2
        assert row["tier_rank"] in (1, 2)

        # 2. Verify none of the returned customers are soft-deleted
        customer = cursor.execute(
            "SELECT deleted_at FROM customers WHERE id = ?", (row["customer_id"],)
        ).fetchone()
        assert customer["deleted_at"] is None