# Docker

The root `compose.yaml` is the local development stack. Run `docker compose up --build` from the repository root. The Dockerfile builds the shared Python workspace image; Compose starts one process per service plus PostgreSQL and Redis.

