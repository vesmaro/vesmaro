# Docker / Docker Compose deployment

Single-host deployment of the pre-built image via Docker Compose (or
podman-compose — the file is compatible with both).

**Full container docs:
[docs/en/admin/runbooks/container-deployment.md](../../docs/en/admin/runbooks/container-deployment.md) ·
[docs/ru/admin/runbooks/container-deployment.md](../../docs/ru/admin/runbooks/container-deployment.md)**
Umbrella over all deployment paths: [../README.md](../README.md)

## Start

```bash
cd deploy/docker
cp .env.example .env          # then edit: TOTP_MASTER_KEY=$(openssl rand -hex 32)
docker compose up -d          # or: podman-compose up -d
curl -fsS http://localhost:8787/health    # → {"status":"ok"}
```

## Notes

- The image is private on ghcr.io — run `docker login ghcr.io` first if the
  pull fails (visibility is being migrated, see deploy/README.md).
- Data lives in named volumes `vesmaro-data` (SQLite + vector index) and
  `vesmaro-vault` (markdown mirror).
- Optional local-embeddings sidecar: `docker compose --profile ollama up -d`.
- To build the image from source instead of pulling it, use the root
  [`compose.yaml`](../../compose.yaml) (`podman-compose up --build`).
