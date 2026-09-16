# Deploy

Deployment assets for the Vesmaro (Mnemos) memory server, grouped by target:

| Directory | Target | Entry point |
|-----------|--------|-------------|
| [`helm/vesmaro/`](helm/vesmaro/) | **Kubernetes / K3s** (any 1.25+ cluster) | `helm install vesmaro deploy/helm/vesmaro --set auth.totpMasterKey=$(openssl rand -hex 32)` — Deployment + Service + **Ingress** + 2×PVC + Secret |
| [`docker/`](docker/) | **Docker / Docker Compose** (pre-built image) | `cp .env.example .env && docker compose up -d` (podman-compose compatible) |
| [`podman/quadlet/`](podman/quadlet/) | **Podman** as a systemd user unit | copy unit + `systemctl --user start mnemos` |
| [`podman/kube/`](podman/kube/) | **Podman** `kube play` (single-host pod) | `podman kube play deploy/podman/kube/mnemos-pod.yaml` |
| [`../../compose.yaml`](../compose.yaml) | Build-from-source compose (root) | `podman-compose up --build` |

Full documentation:

- EN: [docs/en/admin/kubernetes-deployment.md](../docs/en/admin/kubernetes-deployment.md) (Helm/K8s/K3s) ·
  [docs/en/admin/runbooks/container-deployment.md](../docs/en/admin/runbooks/container-deployment.md) (Docker/Podman/compose)
- RU: [docs/ru/admin/kubernetes-deployment.md](../docs/ru/admin/kubernetes-deployment.md) ·
  [docs/ru/admin/runbooks/container-deployment.md](../docs/ru/admin/runbooks/container-deployment.md)

## Requirements common to every path

- **TOTP master key** — required in any non-loopback deployment. Generate:
  `openssl rand -hex 32`. An empty key is rejected at startup.
- **Image**: published to `ghcr.io/korrnals/mnemos` (currently **private**;
  set visibility in Package settings, or `docker login ghcr.io` to pull).
  After the 5.0.0 registry migration (ADR-0031 / GWS card #331, phase g)
  images publish to `ghcr.io/vesmaro/vesmaro` — the Helm chart, compose file
  and docs carry the one-line switch.
- **Data**: two volumes — `/data` (SQLite + vector index) and `/vault`
  (Obsidian markdown mirror). Health surface: unauthenticated `GET /health`.

---

## RU — кратко

Каталог деплоя Vesmaro (Mnemos): **K8s/K3s** — Helm-чарт с ингрессом
(`helm/vesmaro/`), **Docker** — docker-compose с готовым образом
(`docker/`), **Podman** — quadlet-юнит и kube-play манифест (`podman/`).
Полные руководства — в docs (EN/RU, ссылки выше). Обязательное для любого
не-loopback деплоя: TOTP-ключ (`openssl rand -hex 32`); образ сейчас
приватный на ghcr.io (см. примечание выше).
