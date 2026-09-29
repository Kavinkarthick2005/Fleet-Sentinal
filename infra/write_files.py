import pathlib

ROOT = pathlib.Path(r"c:/Users/kavin/Fleet-Sentinal")

def w(rel, content):
    p = ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content.lstrip("\n"), encoding="utf-8")
    print(f"  wrote {rel}")

# ── docker-compose.yml ────────────────────────────────────────────
DC = r"""# Fleet Sentinel Infrastructure  (run: make up from repo root)
# Requires Docker >=24, Compose >=2.24.  WSL2/Git-Bash on Windows.
# make up auto-copies infra/.env.example -> infra/.env on first run.

x-logging: &logging
  driver: json-file
  options: {max-size: "10m", max-file: "3"}

services:

  # Postgres + pgvector  (NO TimescaleDB — ADR-002)
  postgres:
    image: pgvector/pgvector:pg16
    container_name: fs-postgres
    restart: unless-stopped
    environment:
      POSTGRES_DB:       ${POSTGRES_DB:-fleet_sentinel}
      POSTGRES_USER:     ${POSTGRES_USER:-fleet}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-fleet_CHANGEME}
    ports: ["${POSTGRES_PORT:-5432}:5432"]
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./postgres/init.sql:/docker-entrypoint-initdb.d/01_init.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB"]
      interval: 5s
      timeout: 5s
      retries: 15
      start_period: 20s
    logging: *logging

  # ClickHouse — raw telemetry, ReplacingMergeTree tables
  clickhouse:
    image: clickhouse/clickhouse-server:24.3-alpine
    container_name: fs-clickhouse
    restart: unless-stopped
    environment:
      CLICKHOUSE_DB:                        ${CLICKHOUSE_DB:-fleet_telemetry}
      CLICKHOUSE_USER:                      ${CLICKHOUSE_USER:-fleet}
      CLICKHOUSE_PASSWORD:                  ${CLICKHOUSE_PASSWORD:-ch_CHANGEME}
      CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT: 1
    ports:
      - "${CH_HTTP_PORT:-8123}:8123"
      - "${CH_TCP_PORT:-9000}:9000"
    volumes:
      - clickhouse_data:/var/lib/clickhouse
      - ./clickhouse/init.sql:/docker-entrypoint-initdb.d/01_init.sql:ro
      - ./clickhouse/limits.xml:/etc/clickhouse-server/config.d/limits.xml:ro
    mem_limit: ${CH_MEM:-3g}
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:8123/ping"]
      interval: 5s
      timeout: 5s
      retries: 20
      start_period: 30s
    logging: *logging

  # Redpanda — Kafka-compatible, 24-partition topics, 2 h+ retention
  redpanda:
    image: redpandadata/redpanda:v23.3.21
    container_name: fs-redpanda
    restart: unless-stopped
    command:
      - redpanda
      - start
      - --kafka-addr internal://0.0.0.0:9092,external://0.0.0.0:19092
      - --advertise-kafka-addr internal://redpanda:9092,external://localhost:19092
      - --pandaproxy-addr internal://0.0.0.0:8082,external://0.0.0.0:18082
      - --advertise-pandaproxy-addr internal://redpanda:8082,external://localhost:18082
      - --schema-registry-addr internal://0.0.0.0:8081,external://0.0.0.0:18081
      - --rpc-addr redpanda:33145
      - --advertise-rpc-addr redpanda:33145
      - --mode dev-container
      - --smp ${REDPANDA_SMP:-2}
      - --memory ${REDPANDA_MEM:-2G}
      - --reserve-memory 0M
      - --overprovisioned
      - --default-log-level=warn
    ports:
      - "19092:19092"
      - "18082:18082"
      - "18081:18081"
      - "9644:9644"
    volumes: [redpanda_data:/var/lib/redpanda/data]
    mem_limit: ${REDPANDA_MEM:-2G}
    healthcheck:
      test: ["CMD-SHELL", "rpk cluster health"]
      interval: 10s
      timeout: 10s
      retries: 10
      start_period: 30s
    logging: *logging

  # Redpanda Console — topic / consumer-group web UI
  redpanda-console:
    image: redpandadata/console:v2.4.6
    container_name: fs-redpanda-console
    restart: unless-stopped
    depends_on:
      redpanda: {condition: service_healthy}
    ports: ["${CONSOLE_PORT:-8080}:8080"]
    environment:
      KAFKA_BROKERS: redpanda:9092
    logging: *logging

  # Redis — rate-limits, dedup, vehicle-state cache, alert pub/sub
  redis:
    image: redis:7-alpine
    container_name: fs-redis
    restart: unless-stopped
    environment:
      REDIS_PASSWORD: ${REDIS_PASSWORD:-redis_CHANGEME}
    command: >
      redis-server
      --requirepass ${REDIS_PASSWORD:-redis_CHANGEME}
      --maxmemory ${REDIS_MAX_MEM:-512mb}
      --maxmemory-policy allkeys-lru
      --appendonly yes
    ports: ["${REDIS_PORT:-6379}:6379"]
    volumes: [redis_data:/data]
    healthcheck:
      test: ["CMD-SHELL", "redis-cli -a $$REDIS_PASSWORD ping | grep PONG"]
      interval: 5s
      timeout: 3s
      retries: 10
    logging: *logging

  # Prometheus
  prometheus:
    image: prom/prometheus:v2.51.0
    container_name: fs-prometheus
    restart: unless-stopped
    ports: ["${PROMETHEUS_PORT:-9090}:9090"]
    volumes:
      - ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus_data:/prometheus
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.path=/prometheus
      - --storage.tsdb.retention.time=30d
      - --web.enable-lifecycle
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:9090/-/healthy"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s
    logging: *logging

  # Grafana
  grafana:
    image: grafana/grafana:10.4.2
    container_name: fs-grafana
    restart: unless-stopped
    depends_on:
      prometheus: {condition: service_healthy}
    ports: ["${GRAFANA_PORT:-3000}:3000"]
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:-grafana_CHANGEME}
      GF_AUTH_ANONYMOUS_ENABLED:  "false"
      GF_USERS_ALLOW_SIGN_UP:     "false"
    volumes:
      - grafana_data:/var/lib/grafana
      - ./grafana/provisioning:/etc/grafana/provisioning:ro
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:3000/api/health"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 15s
    logging: *logging

  # Topic initialiser — runs once then exits
  redpanda-topics:
    image: redpandadata/redpanda:v23.3.21
    container_name: fs-redpanda-topics
    depends_on:
      redpanda: {condition: service_healthy}
    volumes: [./redpanda/create-topics.sh:/create-topics.sh:ro]
    entrypoint: ["/bin/bash", "/create-topics.sh"]
    restart: "no"

volumes:
  postgres_data:
  clickhouse_data:
  redpanda_data:
  redis_data:
  prometheus_data:
  grafana_data:

networks:
  default:
    name: fleet-sentinel-net
"""
w("infra/docker-compose.yml", DC)

print("docker-compose.yml done")
