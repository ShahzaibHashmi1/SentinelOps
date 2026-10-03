"""SentinelOps load test (Locust). Every request goes through the gateway.

Task weights: list products 40, get product 30, create order 25, get order 5.
Run headless through docker compose (see README):  docker compose --profile load run --rm loadtest
"""
import os
import random

from locust import HttpUser, between, events, task

SKUS = [f"SKU-{i:03d}" for i in range(1, 51)]  # the 50 seeded products
P95_LIMIT_MS = float(os.environ.get("P95_LIMIT_MS", "500"))  # Definition of Done: p95 below 500 ms

# Order ids created during the run, shared by all simulated users, so "get order" reads real orders.
ORDER_IDS: list[str] = []
MAX_REMEMBERED_ORDERS = 1000


class ShopUser(HttpUser):
    wait_time = between(0.5, 1.5)  # think time between actions of one user

    @task(40)
    def list_products(self):
        self.client.get("/api/products", name="GET /api/products")

    @task(30)
    def get_product(self):
        sku = random.choice(SKUS)
        # name= groups all SKUs into ONE row in the statistics
        self.client.get(f"/api/products/{sku}", name="GET /api/products/[sku]")

    @task(25)
    def create_order(self):
        payload = {
            "sku": random.choice(SKUS),
            "quantity": random.randint(1, 3),
            "customer_id": f"load-user-{random.randint(1, 1000)}",
        }
        with self.client.post("/api/orders", json=payload, name="POST /api/orders", catch_response=True) as r:
            if r.status_code != 201:
                r.failure(f"expected 201, got {r.status_code}: {r.text[:200]}")
                return
            order = r.json()
            if order.get("status") != "PAID":
                r.failure(f"order not PAID: {order.get('status')}")
                return
            if len(ORDER_IDS) < MAX_REMEMBERED_ORDERS:
                ORDER_IDS.append(order["id"])

    @task(5)
    def get_order(self):
        if not ORDER_IDS:  # nothing created yet: skip this round
            return
        self.client.get(f"/api/orders/{random.choice(ORDER_IDS)}", name="GET /api/orders/[id]")


@events.quitting.add_listener
def check_pass_criteria(environment, **_kwargs):
    """Exit code 1 (visible to docker compose) if the run has failures or p95 is above the limit."""
    total = environment.stats.total
    if total.num_requests == 0:
        environment.process_exit_code = 1
    elif total.num_failures > 0:
        print(f"FAIL: {total.num_failures} failed requests")
        environment.process_exit_code = 1
    elif total.get_response_time_percentile(0.95) > P95_LIMIT_MS:
        print(f"FAIL: p95 {total.get_response_time_percentile(0.95)} ms is above {P95_LIMIT_MS} ms")
        environment.process_exit_code = 1
    else:
        print(f"PASS: 0 failures, p95 {total.get_response_time_percentile(0.95)} ms (limit {P95_LIMIT_MS} ms)")
