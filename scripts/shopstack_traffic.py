"""Generate concurrent, realistic ShopStack traffic through its API gateway."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from decimal import Decimal
from uuid import UUID, uuid4

import httpx

DEMO_USER_ID = UUID("11111111-1111-4111-8111-111111111111")
PRODUCTS = ("SKU-COFFEE", "SKU-TEA", "SKU-MUG")


async def send_order(client: httpx.AsyncClient, sequence: int) -> int:
    sku = PRODUCTS[sequence % len(PRODUCTS)]
    quantity = sequence % 3 + 1
    unit_prices = {
        "SKU-COFFEE": Decimal("9.25"),
        "SKU-TEA": Decimal("6.50"),
        "SKU-MUG": Decimal("12.00"),
    }
    response = await client.post(
        "/orders",
        headers={"Idempotency-Key": f"traffic-{uuid4()}"},
        json={
            "user_id": str(DEMO_USER_ID),
            "sku": sku,
            "quantity": quantity,
            "amount": str(unit_prices[sku] * quantity),
        },
    )
    return response.status_code


async def run(base_url: str, requests: int, concurrency: int) -> None:
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=base_url, timeout=15, limits=limits) as client:
        semaphore = asyncio.Semaphore(concurrency)

        async def limited_send(sequence: int) -> int:
            async with semaphore:
                try:
                    return await send_order(client, sequence)
                except httpx.HTTPError:
                    return 0

        statuses = await asyncio.gather(*(limited_send(index) for index in range(requests)))
    for status_code, count in sorted(Counter(statuses).items()):
        label = "transport_error" if status_code == 0 else str(status_code)
        print(f"{label}: {count}")
    print(f"total: {len(statuses)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=5)
    args = parser.parse_args()
    if args.requests < 1 or args.concurrency < 1:
        parser.error("--requests and --concurrency must be positive")
    asyncio.run(run(args.base_url.rstrip("/"), args.requests, args.concurrency))


if __name__ == "__main__":
    main()
