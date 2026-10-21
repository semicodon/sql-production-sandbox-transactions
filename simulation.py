"""
SQL Production Sandbox: Real-World Database Engineering Simulations
Demonstrates: N+1 Queries, Indexing & Query Plans, ACID Transactions, and Window Functions.
"""

import random
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta


class DatabaseSimulator:
    def __init__(self, db_path=":memory:"):
        # Using an in-memory SQLite database for instant, reproducible runs
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        # Enable Foreign Key constraints (off by default in SQLite)
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.query_count = 0

        # Hook into SQLite's trace callback to count every query executed
        self.conn.set_trace_callback(self._trace_queries)

    def _trace_queries(self, statement: str):
        if statement.strip().upper().startswith(("SELECT", "INSERT", "UPDATE", "DELETE")):
            self.query_count += 1

    @contextmanager
    def benchmark(self, label: str):
        """Context manager to measure execution time and total SQL queries."""
        self.query_count = 0
        start_time = time.perf_counter()
        yield
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        print(f"  -> [{label}] Queries Executed: {self.query_count} | Time: {elapsed_ms:.2f} ms")

    def setup_schema_and_seed(self, num_customers=500, num_orders=25000):
        print("1. Setting up schema and seeding realistic messy data...")
        cursor = self.conn.cursor()

        cursor.executescript("""
                             CREATE TABLE customers (
                                                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                                                        name TEXT NOT NULL,
                                                        email TEXT UNIQUE NOT NULL,
                                                        tier TEXT DEFAULT 'free',
                                                        deleted_at TIMESTAMP NULL -- Soft delete column!
                             );

                             CREATE TABLE products (
                                                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                                                       sku TEXT UNIQUE NOT NULL,
                                                       name TEXT NOT NULL,
                                                       stock_count INTEGER NOT NULL CHECK (stock_count >= 0),
                                                       price_cents INTEGER NOT NULL
                             );

                             CREATE TABLE orders (
                                                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                                                     customer_id INTEGER NOT NULL,
                                                     status TEXT NOT NULL, -- 'pending', 'paid', 'refunded'
                                                     total_cents INTEGER NOT NULL,
                                                     discount_code TEXT NULL, -- Often NULL in real life
                                                     created_at TIMESTAMP NOT NULL,
                                                     FOREIGN KEY (customer_id) REFERENCES customers(id)
                             );
                             """)

        # Seed Customers (some soft-deleted)
        tiers = ["free", "pro", "enterprise"]
        customers_data = [
            (
                f"Customer_{i}",
                f"user_{i}@example.com",
                random.choice(tiers),
                datetime.now().isoformat() if random.random() < 0.1 else None,  # 10% soft-deleted
            )
            for i in range(1, num_customers + 1)
        ]
        cursor.executemany(
            "INSERT INTO customers (name, email, tier, deleted_at) VALUES (?, ?, ?, ?)",
            customers_data,
        )

        # Seed Products
        cursor.execute(
            "INSERT INTO products (sku, name, stock_count, price_cents) VALUES ('SKU-100', 'Mechanical Keyboard', 5, 15000)"
        )

        # Seed Orders
        statuses = ["pending", "paid", "paid", "refunded"]
        base_time = datetime.now() - timedelta(days=90)
        orders_data = [
            (
                random.randint(1, num_customers),
                random.choice(statuses),
                random.randint(1000, 50000),
                "SAVE10" if random.random() < 0.2 else None,
                (base_time + timedelta(minutes=random.randint(1, 120000))).isoformat(),
            )
            for _ in range(num_orders)
        ]
        cursor.executemany(
            "INSERT INTO orders (customer_id, status, total_cents, discount_code, created_at) VALUES (?, ?, ?, ?, ?)",
            orders_data,
        )
        self.conn.commit()
        print(f"   Seeded {num_customers} customers and {num_orders} orders.\n")

    # =========================================================================
    # SCENARIO 1: The N+1 Query Trap vs. Eager JOIN Loading
    # =========================================================================
    def simulate_n_plus_one(self):
        print("=" * 65)
        print("SCENARIO 1: The N+1 ORM Trap vs. SQL JOIN")
        print("=" * 65)
        cursor = self.conn.cursor()

        # BAD: Fetching 200 recent orders, then querying the customer inside a loop
        with self.benchmark("BAD: N+1 Queries in a Loop"):
            orders = cursor.execute(
                "SELECT id, customer_id, total_cents FROM orders LIMIT 200"
            ).fetchall()
            results_bad = []
            for order in orders:
                customer = cursor.execute(
                    "SELECT name, email FROM customers WHERE id = ?",
                    (order["customer_id"],),
                ).fetchone()
                results_bad.append((order["id"], customer["name"], order["total_cents"]))

        # GOOD: Fetching everything in a single round-trip using an INNER JOIN
        with self.benchmark("GOOD: Single Eager INNER JOIN"):
            results_good = cursor.execute("""
                                          SELECT o.id, c.name, o.total_cents
                                          FROM orders o
                                                   INNER JOIN customers c ON o.customer_id = c.id
                                              LIMIT 200
                                          """).fetchall()

        print()

    # =========================================================================
    # SCENARIO 2: Full Table Scan vs. B-Tree Indexing
    # =========================================================================
    def simulate_indexing(self):
        print("=" * 65)
        print("SCENARIO 2: Unindexed Full Table Scan vs. Composite B-Tree Index")
        print("=" * 65)
        cursor = self.conn.cursor()

        query = """
                SELECT id, customer_id, total_cents
                FROM orders
                WHERE status = 'paid' AND total_cents > 45000 \
                """

        # Explain Query Plan BEFORE index
        plan_before = cursor.execute(f"EXPLAIN QUERY PLAN {query}").fetchone()
        print(f"  Plan Before Index : {plan_before['detail']}")
        with self.benchmark("Unindexed Query (Full Table Scan)"):
            for _ in range(50):  # Run 50 times to simulate concurrent API load
                cursor.execute(query).fetchall()

        # Create a composite B-Tree index
        cursor.execute("CREATE INDEX idx_orders_status_total ON orders(status, total_cents);")

        # Explain Query Plan AFTER index
        plan_after = cursor.execute(f"EXPLAIN QUERY PLAN {query}").fetchone()
        print(f"  Plan After Index  : {plan_after['detail']}")
        with self.benchmark("Indexed Query (B-Tree Lookup)"):
            for _ in range(50):
                cursor.execute(query).fetchall()

        print()

    # =========================================================================
    # SCENARIO 3: ACID Transactions & Atomic Rollbacks
    # =========================================================================
    def simulate_acid_transactions(self):
        print("=" * 65)
        print("SCENARIO 3: ACID Transactions (Preventing Partial Checkout Bugs)")
        print("=" * 65)
        cursor = self.conn.cursor()

        initial_stock = cursor.execute(
            "SELECT stock_count FROM products WHERE sku = 'SKU-100'"
        ).fetchone()["stock_count"]
        print(f"  Initial Keyboard Stock: {initial_stock}")

        print("  Attempting checkout where Payment Gateway crashes mid-flight...")
        try:
            # Begin an atomic transaction block
            with self.conn:
                # Step 1: Deduct inventory
                cursor.execute(
                    "UPDATE products SET stock_count = stock_count - 1 WHERE sku = 'SKU-100'"
                )
                # Step 2: Create the order record
                cursor.execute(
                    """INSERT INTO orders (customer_id, status, total_cents, created_at)
                       VALUES (1, 'pending', 15000, ?)""",
                    (datetime.now().isoformat(),),
                )
                # Step 3: Simulate external Stripe/Payment API failure!
                raise RuntimeError("504 Gateway Timeout from Payment Provider!")
        except RuntimeError as exc:
            print(f"  [Caught Error]: {exc}")
            print("  Database automatically triggered a ROLLBACK.")

        final_stock = cursor.execute(
            "SELECT stock_count FROM products WHERE sku = 'SKU-100'"
        ).fetchone()["stock_count"]
        print(f"  Stock After Crash     : {final_stock} (Inventory safely preserved!)\n")

    # =========================================================================
    # SCENARIO 4: Executive Analytics (CTEs, Soft Deletes & Window Functions)
    # =========================================================================
    def simulate_analytics_query(self):
        print("=" * 65)
        print("SCENARIO 4: Production Analytics (CTEs + Window Functions)")
        print("=" * 65)
        print("  Goal: Find the Top 2 highest-spending ACTIVE customers per tier.\n")
        cursor = self.conn.cursor()

        # Real-world nuances handled here:
        # 1. Filtering out soft-deleted users (deleted_at IS NULL)
        # 2. Only counting 'paid' orders (ignoring 'pending' or 'refunded')
        # 3. Using a CTE for readability and DENSE_RANK() window function
        analytics_sql = """
                        WITH customer_lifetime_value AS (
                            SELECT
                                c.id AS customer_id,
                                c.name,
                                c.tier,
                                COUNT(o.id) AS paid_orders_count,
                                SUM(o.total_cents) / 100.0 AS ltv_dollars
                            FROM customers c
                                     INNER JOIN orders o ON c.id = o.customer_id
                            WHERE c.deleted_at IS NULL       -- Exclude soft-deleted accounts
                              AND o.status = 'paid'          -- Only count realized revenue
                            GROUP BY c.id, c.name, c.tier
                        ),
                             ranked_customers AS (
                                 SELECT
                                     *,
                                     DENSE_RANK() OVER (
                        PARTITION BY tier 
                        ORDER BY ltv_dollars DESC
                    ) AS tier_rank
                                 FROM customer_lifetime_value
                             )
                        SELECT tier, tier_rank, name, paid_orders_count, ltv_dollars
                        FROM ranked_customers
                        WHERE tier_rank <= 2
                        ORDER BY tier, tier_rank; \
                        """

        rows = cursor.execute(analytics_sql).fetchall()
        print(f"  {'TIER':<12} | {'RANK':<4} | {'CUSTOMER':<14} | {'ORDERS':<6} | {'LTV ($)':<10}")
        print("  " + "-" * 58)
        for r in rows:
            print(
                f"  {r['tier']:<12} | #{r['tier_rank']:<3} | {r['name']:<14} | {r['paid_orders_count']:<6} | ${r['ltv_dollars']:,.2f}"
            )
        print()


if __name__ == "__main__":
    sim = DatabaseSimulator()
    sim.setup_schema_and_seed()
    sim.simulate_n_plus_one()
    sim.simulate_indexing()
    sim.simulate_acid_transactions()
    sim.simulate_analytics_query()