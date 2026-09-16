# Container Deployment

**🌐 Language / Язык:** English · [Русский](../../../ru/admin/runbooks/container-deployment.md)

> Admin-tier runbook for building, pushing, and running Mnemos in a container.
> Covers the container-native paths: `deploy.sh` helper, docker/podman-compose,
> raw `podman run`, `podman kube play`, and systemd via quadlet. For real
> Kubernetes/K3s clusters use the Helm chart —
> [kubernetes-deployment.md](../kubernetes-deployment.md).

---

## Overview

Mnemos ships a `Containerfile` (OCI-compatible, podman/buildah) and a ready-made `compose.yaml`.
Seven deployment paths are available — pick the one that fits your environment:

| Path | Tool | When to use |
|------|------|-------------|
| `./scripts/deploy.sh` | podman + podman-compose | Dev and automation — wraps every other option |
| `podman-compose up` | podman-compose | Recommended for single-host production |
| `docker compose up` | Docker Compose | Same on Docker — `deploy/docker/` (pre-built image) |
| `podman run` | podman | Minimal dependencies; use the pre-built image from ghcr.io |
| `podman kube play` | podman | Kubernetes-style pod on a single host |
| systemd quadlet | podman + systemd | Long-running user service with automatic restart |
| Helm chart | Helm 3 + K8s/K3s | Real clusters — [kubernetes-deployment.md](../kubernetes-deployment.md) |

The container exposes **port 8787** and uses two named volumes: `mnemos-data` (SQLite + vector index)
and `mnemos-vault` (Obsidian markdown mirror).

---

## Prerequisites

- **podman** ≥ 4.0 — rootless usage is fully supported and recommended
- **podman-compose** — required for the compose path (`pip install podman-compose` or distro package)
- **buildah** — alternative to `podman build`; optional
- Python and `git` are **not** required on the host — everything runs inside the container

---

## Build

Build a versioned local image from the source tree:

```bash
podman build -t localhost/mnemos:4.0.0 -f Containerfile .
```

The `Containerfile` uses `python:3.12-slim` as the base, installs the package (the MCP SDK rides in core), copies
`config.container.yaml` as `/app/config.yaml`, and sets `CMD ["mnemos", "serve"]` on port 8787.

Makefile shortcut (builds `localhost/mnemos:latest`):

```bash
make build-image
```

The deploy helper does the same:

```bash
./scripts/deploy.sh build
```

> **Release builds**: the local release pipeline (`scripts/local-release.sh`) builds and
> pushes the image on every release — GitHub Actions are disabled, and this script is the
> canonical path (see [ci-cd.md](ci-cd.md)). Manual builds are needed only for local testing
> or out-of-band deploys.

---

## Push to ghcr.io

> Skip this section if you are consuming the pre-built image from `ghcr.io/vesmaro/vesmaro`.

**Manual push** (requires a PAT with `write:packages` scope):

```bash
podman login ghcr.io
podman tag localhost/mnemos:4.3.0 ghcr.io/vesmaro/vesmaro:4.3.0
podman push ghcr.io/vesmaro/vesmaro:4.3.0
podman push ghcr.io/vesmaro/vesmaro:latest
```

Makefile shortcut:

```bash
make push-image
```

> **Release pushes**: `scripts/local-release.sh` pushes both the versioned tag and `:latest`
> on every release. It currently targets the legacy `ghcr.io/korrnals/mnemos` name (the
> pipeline flip is part of the 5.0.0 phase-g, GWS card #331) — new releases are backfilled
> to the org namespace `ghcr.io/vesmaro/vesmaro` manually in the meantime. No manual push
> is required as part of the normal release cycle.

---

## Run — compose (recommended)

The compose path uses `compose.yaml` and mounts `./config.container.yaml` from the repo root as
`/app/config.yaml` inside the container (read-only).

### Start

```bash
podman-compose up -d
```

On success, the deploy helper prints:

```text
Mnemos API:   http://localhost:8787
Swagger UI:   http://localhost:8787/docs
```

### Logs

```bash
podman-compose logs -f mnemos
```

### Stop

```bash
podman-compose down
```

### Required environment variable

`config.container.yaml` binds to `0.0.0.0`, which **requires** authentication.
Pass the TOTP master key at runtime — never store it in the config file:

```bash
MNEMOS_API__TOTP_MASTER_KEY=<your-key> podman-compose up -d
```

Or add it to a `.env` file that is listed in `.gitignore`:

```bash
echo 'MNEMOS_API__TOTP_MASTER_KEY=<your-key>' >> .env
podman-compose up -d
```

### Ollama sidecar (optional embeddings)

Start Mnemos together with a local Ollama instance using the bundled profile:

```bash
podman-compose --profile ollama up -d
```

Pull the embedding model into the sidecar:

```bash
podman exec mnemos-ollama ollama pull nomic-embed-text
```

To activate Ollama as the embedding provider, update `config.container.yaml`:

```yaml
embedding:
  provider: ollama
  model: nomic-embed-text
  ollama_url: http://ollama:11434
```

---

## Run — single container

Pull the released image and start it directly:

```bash
podman pull ghcr.io/vesmaro/vesmaro:4.3.0
podman run -d -v mnemos-data:/data -v mnemos-vault:/vault -p 8787:8787 \
  --env MNEMOS_API__TOTP_MASTER_KEY=<your-key> ghcr.io/vesmaro/vesmaro:4.3.0
```

The image includes `config.container.yaml` baked in as `/app/config.yaml` — no separate config
mount is required unless you want to override settings.

---

## Run — Kubernetes-style pod (podman kube play)

Mnemos ships a Kubernetes-style pod manifest (`deploy/podman/kube/mnemos-pod.yaml`) compatible
with `podman kube play`. The manifest uses `PersistentVolumeClaims`, so named volumes must exist
first, and it injects the TOTP key from a podman secret plus health probes.

### Start

```bash
printf 'MNEMOS_API__TOTP_MASTER_KEY=<your-key>\nVESMARO_API__TOTP_MASTER_KEY=<your-key>\n' \
  | podman secret create vesmaro-totp -
podman volume create mnemos-data
podman volume create mnemos-vault
podman kube play deploy/podman/kube/mnemos-pod.yaml
```

Shortcut (creates volumes automatically before playing the manifest):

```bash
./scripts/deploy.sh kube-up
```

### Stop

```bash
podman kube down deploy/kube/mnemos-pod.yaml
```

Shortcut:

```bash
./scripts/deploy.sh kube-down
```

---

## Run — systemd (quadlet)

The quadlet path installs a systemd **user** unit and manages the container as a persistent
service. The unit references `localhost/mnemos:latest`, so build the image locally first
(see [Build](#build)).

### Set the TOTP key

The unit reads the key from `~/.vesmaro.env` (`EnvironmentFile`), so no unit
editing is needed. Both env spellings must carry the same value — 4.x images
read `MNEMOS_API__*`, 5.x images read `VESMARO_API__*` (ADR-0031):

```bash
KEY=$(openssl rand -hex 32)
printf 'MNEMOS_API__TOTP_MASTER_KEY=%s\nVESMARO_API__TOTP_MASTER_KEY=%s\n' "$KEY" "$KEY" > ~/.vesmaro.env
```

### Install the unit

```bash
./scripts/deploy.sh quadlet
```

This copies `deploy/podman/quadlet/mnemos.container` to `~/.config/containers/systemd/` and runs
`systemctl --user daemon-reload`.

### Start and enable

```bash
systemctl --user start mnemos
systemctl --user enable mnemos   # autostart on login
```

### Check status

```bash
systemctl --user status mnemos
```

---

## Configuration

Mnemos uses `config.container.yaml` as the container config. It is:

- Embedded in the image at build time as `/app/config.yaml`
- Overridden in the compose path by mounting `./config.container.yaml:/app/config.yaml:ro`

Key settings:

| Setting | Default | Notes |
|---------|---------|-------|
| `mnemos.data_dir` | `/data` | Mapped to the `mnemos-data` named volume |
| `mnemos.vault_path` | `/vault` | Mapped to the `mnemos-vault` named volume |
| `api.host` | `0.0.0.0` | Binds to all interfaces — **requires auth** |
| `api.port` | `8787` | Container-internal port; host mapping set in compose/run |
| `api.auth_enabled` | `true` | Must stay `true` when `host` is `0.0.0.0` |
| `api.totp_enabled` | `true` | Requires TOTP 2FA; key via `MNEMOS_API__TOTP_MASTER_KEY` |
| `api.behind_tls_proxy` | `true` | TLS terminates upstream (Caddy, nginx, etc.) |
| `embedding.provider` | `nano` | mnema-embed-v1: bundled local model, works offline; no GPU required |

### Security requirements

Binding to `0.0.0.0` **requires** both `auth_enabled: true` and `totp_enabled: true`.
The TOTP master key must be supplied via `MNEMOS_API__TOTP_MASTER_KEY` — it must never appear
in the config file or in any committed file.

Place Mnemos behind a TLS-terminating reverse proxy (Caddy, nginx, etc.).
Set `trusted_proxies` to the CIDR of your proxy so that `X-Forwarded-For` headers are trusted.

For the full threat model and auth configuration details, see [../security.md](../security.md).

### Embedding provider

- **Default**: `nano` — the bundled `mnema-embed-v1` local ONNX model (no torch, no GPU, works offline; external providers like `onnx`/`sentence-transformers` remain available)
- **Ollama sidecar**: set `embedding.provider: ollama` and `embedding.ollama_url: http://ollama:11434`
  (see [Ollama sidecar](#ollama-sidecar-optional-embeddings) above)

---

## Health & ops

### Container healthcheck

The compose healthcheck runs `mnemos stats` every 30 seconds with a 5-second timeout.
Check the current health state:

```bash
podman inspect --format '{{.State.Health.Status}}' mnemos
```

### Status overview

```bash
./scripts/deploy.sh status
```

Prints running containers (name, status, ports) and named volumes.

### Shell access

```bash
./scripts/deploy.sh shell
# equivalent to: podman exec -it mnemos /bin/bash
```

### Run CLI inside the container

```bash
./scripts/deploy.sh cli search "hello"
# equivalent to: podman exec mnemos mnemos search "hello"
```

---

## See also

- [kubernetes-deployment.md](../kubernetes-deployment.md) — Helm chart for real K8s/K3s clusters
- [`deploy/README.md`](../../../../deploy/README.md) — all deployment paths at a glance
- [install.md](install.md) — bare-metal / virtualenv install
- [../security.md](../security.md) — threat model, auth model, SSRF guard
- [../../user/getting-started.md](../../user/getting-started.md) — first run guide

---

_Source files: `Containerfile`, `compose.yaml`, `config.container.yaml`, `scripts/deploy.sh`,
`deploy/podman/quadlet/mnemos.container`, `deploy/podman/kube/mnemos-pod.yaml`,
`deploy/docker/`, `deploy/helm/vesmaro/`_
