# mesh#44 field-presence gate — run INSIDE the built image, pre-push.
# Required (fail build if absent): ListMemoriesRequest.resume_cursor,
# ListMemoriesResponse.cursor, federation PullRequest.cursor.
# Informational: MetadataRecord.origin_peer (S2, not on main yet).
import sys
sys.path.insert(0, "/usr/local/lib/python3.12/federation/gen/python")
import mnemos_core_api_pb2 as core
import federation_pb2 as fed

def fields(msg_cls):
    return [f.name for f in msg_cls.DESCRIPTOR.fields]

checks = {
    "ListMemoriesRequest.resume_cursor": "resume_cursor" in fields(core.ListMemoriesRequest),
    "ListMemoriesResponse.cursor": "cursor" in fields(core.ListMemoriesResponse),
    "federation.PullRequest.cursor": "cursor" in fields(fed.PullRequest),
    "federation.MetadataRecord.origin_peer (S2, optional)": "origin_peer" in fields(fed.MetadataRecord),
    "federation.MetadataRecord.source_peer (main analog)": "source_peer" in fields(fed.MetadataRecord),
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'absent'}  {name}")

# Exercise the crash path itself: mesh_server must import against the
# shipped stubs and the servicer module must resolve the descriptors.
import mnemos.mesh_server  # noqa: F401  (runtime import path; AttributeError here = stale stubs)
print("PASS  import mnemos.mesh_server (site-packages/vesparo/mesh_server.py)")

required = ["ListMemoriesRequest.resume_cursor",
            "ListMemoriesResponse.cursor",
            "federation.PullRequest.cursor"]
missing = [n for n in required if not checks[n]]
if missing:
    print("GATE-FAIL:", ", ".join(missing)); sys.exit(1)
print("GATE-OK")
