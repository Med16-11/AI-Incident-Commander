"""Experiment catalogue and safety bounds for ShopStack ChaosLab."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Experiment:
    """A bounded experiment with a concrete target and documented cleanup."""

    name: str
    target: str
    failure_type: str
    duration_seconds: int
    expected_symptoms: tuple[str, ...]
    rollback_procedure: str
    setting: str | None = None
    value: str | None = None
    stop_target: bool = False


EXPERIMENTS: dict[str, Experiment] = {
    "payment-latency": Experiment(
        "payment-latency",
        "shopstack-payment-service",
        "service latency",
        30,
        ("payment p95 latency rises", "order latency and timeout errors may rise"),
        "Recreate the payment service with its original environment.",
        "LATENCY_MS",
        "1200",
    ),
    "payment-outage": Experiment(
        "payment-outage",
        "shopstack-payment-service",
        "service outage",
        30,
        ("payment health checks fail", "order requests return dependency errors"),
        "Start the payment service and wait for its readiness check.",
        stop_target=True,
    ),
    "inventory-outage": Experiment(
        "inventory-outage",
        "shopstack-inventory-service",
        "service outage",
        30,
        ("inventory health checks fail", "orders fail during reservation"),
        "Start the inventory service and wait for its readiness check.",
        stop_target=True,
    ),
    "redis-outage": Experiment(
        "redis-outage",
        "shopstack-redis",
        "cache outage",
        30,
        ("Redis probes fail", "cache-backed requests record dependency errors"),
        "Start Redis and wait for its health check.",
        stop_target=True,
    ),
    "postgres-connection-exhaustion": Experiment(
        "postgres-connection-exhaustion",
        "shopstack-order-service",
        "database pool exhaustion",
        30,
        ("database pool wait time rises", "order requests time out under concurrent load"),
        "Recreate the order service with SHOPSTACK_FAILURE_DB_CONNECTIONS unset.",
        "DB_CONNECTIONS",
        "5",
    ),
    "cpu-saturation": Experiment(
        "cpu-saturation",
        "shopstack-order-service",
        "CPU saturation",
        30,
        ("CPU usage rises while requests are in flight", "request latency rises"),
        "Recreate the order service with SHOPSTACK_FAILURE_CPU_STRESS_MS unset.",
        "CPU_STRESS_MS",
        "750",
    ),
    "memory-pressure": Experiment(
        "memory-pressure",
        "shopstack-payment-service",
        "memory pressure",
        30,
        ("container working set rises", "memory dashboards show increased usage"),
        "Recreate the payment service with SHOPSTACK_FAILURE_MEMORY_MB unset.",
        "MEMORY_MB",
        "128",
    ),
    "pod-crash-loop": Experiment(
        "pod-crash-loop",
        "shopstack-payment-service",
        "container crash loop",
        30,
        ("payment container repeatedly fails startup", "restart count rises"),
        "Recreate the payment service with SHOPSTACK_FAILURE_CRASH_ON_START unset.",
        "CRASH_ON_START",
        "true",
    ),
    "network-latency": Experiment(
        "network-latency",
        "shopstack-payment-service",
        "request-path latency",
        30,
        ("payment request latency rises", "upstream order latency rises"),
        "Recreate the payment service with its original environment.",
        "LATENCY_MS",
        "1500",
    ),
    "network-packet-loss": Experiment(
        "network-packet-loss",
        "shopstack-payment-service",
        "simulated request loss",
        30,
        ("sampled requests receive injected 503 responses", "dependency failure metrics rise"),
        "Recreate the payment service with SHOPSTACK_FAILURE_PACKET_LOSS_RATE unset.",
        "PACKET_LOSS_RATE",
        "0.2",
    ),
    "bad-deployment": Experiment(
        "bad-deployment",
        "shopstack-payment-service",
        "bad release configuration",
        30,
        ("payment 5xx rate rises to 100%", "orders fail at payment authorization"),
        "Recreate the payment service with its original environment.",
        "HTTP_500_RATE",
        "1",
    ),
    "dependency-timeout": Experiment(
        "dependency-timeout",
        "shopstack-order-service",
        "dependency timeout",
        30,
        ("outbound calls time out quickly", "order errors and dependency latency rise"),
        "Recreate the order service with SHOPSTACK_FAILURE_DEPENDENCY_TIMEOUT_MS unset.",
        "DEPENDENCY_TIMEOUT_MS",
        "100",
    ),
}

MAX_DURATION_SECONDS = 300
