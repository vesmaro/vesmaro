# Mesh-enabled release image (v4.3.x-mesh.N)

Builds the release image with the gencode the `vesmaro._mesh_gen` shim
expects. This institutionalizes recipe #354 and the verification gate
added after the `v4.3.4-mesh.1` regression (mnemos-mesh#44: the build
copied stale gitignored stubs — the image's gencode lacked
`ListMemoriesRequest.resume_cursor` and every pull to the cluster died
on `AttributeError`).

## Build (from a clean checkout — the working tree never enters the context)

```bash
git archive <commit> -o /tmp/src.tar
mkdir /tmp/mesh-img && tar -xf /tmp/src.tar -C /tmp/mesh-img
cp build/mesh-image/Dockerfile.mesh /tmp/mesh-img/
distrobox-host-exec podman build -f /tmp/mesh-img/Dockerfile.mesh \
  -t ghcr.io/korrnals/mnemos:v<X>-mesh.<N> /tmp/mesh-img
```

`Dockerfile.mesh` generates the python stubs **from the proto files of
the archived commit** (never copies `federation/gen/` from a checkout)
and runs `gate.py` inside the built image **before** it can be pushed.

## Gate (mandatory, fails the build)

`gate.py` imports the shipped stubs and asserts the presence of the
newest proto fields (`resume_cursor`, `ListMemoriesResponse.cursor`,
`PullRequest.cursor`, `MetadataRecord.origin_peer` when present on the
branch) plus `import vesmaro.mesh_server`. A missing field = stale
gencode = build failure. Extend the field list whenever the proto gains
load-bearing fields.

## Rotation reminder

Core-identity cert leaves (`CN=*-mnemos-core`) rotate manually every 90
days via the mesh pki directory; see
`~/.config/mnemos-mesh/pki/fingerprints.txt` on the operator host.
