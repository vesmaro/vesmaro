"""Unit tests for the MnemosCore gRPC server (#105 M4.0).

Exercises :class:`vesmaro.mesh_server.MeshServer` end-to-end over a real
gRPC Unix socket on a ``tmp_path`` — no mocks on the gRPC layer. The
tests cover:

* Server starts on a Unix socket and stops cleanly.
* :rpc:`Heartbeat` returns a non-empty version + ``healthy=True``.
* :rpc:`ListMemories` returns seeded records (moderation-processed).
* :rpc:`ListMemories` with a disallowed ``project_scope`` → ACL refuses
  (empty response, ``PERMISSION_DENIED``).
* :rpc:`ListMemories` cursor contract (ADR-0020): non-empty page mints a
  stable opaque cursor; empty page mints ""; resume by a fresh cursor
  returns EXACTLY the new records; resume_cursor takes priority over
  ``since``; garbage/foreign cursors → ``INVALID_ARGUMENT``; the legacy
  ``since`` path still filters; cursor-walk pagination partitions the
  corpus (no dupes, no gaps); records carry ``revision`` (storage rowid,
  ADR-0021 Q10.6) monotonic across the page.
* :rpc:`WriteMemory` writes to SQLite (verified via a direct DB query).
* :rpc:`WriteMemory` with a disallowed scope → ``PERMISSION_DENIED``.
* :rpc:`WriteMemory` idempotency (#359): replays return
  ``ALREADY_EXHAUSTED`` with the existing storage id (fed_id key,
  title+source_agent fallback, no match for provenance-less records).
* :rpc:`GetSubscriptionState` returns an empty cursor (M4: mesh-side
  persistence in M5) and enforces the ACL.
* ACL hardening fail-closed matrix (vesmaro#371/#369 family): unscoped
  requests intersect with the peer's allowed set; empty allow-list /
  wildcard-with-empty-shared / unknown peer → ``PERMISSION_DENIED`` on
  read AND write; untagged WriteMemory records are refused; wildcard
  read/write is bounded by ``shared_projects``.
* Server lifecycle (start/stop cleanly, socket cleanup, context manager).

The tests use the real :class:`MemoryManager` against a tmp SQLite store
so the moderation pipeline + Layer 1 secrets scanner run for real.
"""

from __future__ import annotations

import stat
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import grpc
import pytest

from vesmaro import _mesh_gen
from vesmaro.compact import CompactRecord
from vesmaro.config import FederationConfig, PeerConfig, Settings
from vesmaro.manager import MemoryManager
from vesmaro.mesh_server import MeshServer
from vesmaro.models import MemoryCreate, MemorySource

# ── Constants ────────────────────────────────────────────────────────────────

#: Test project slug — the peer is allowed to access this project only.
_PROJECT = "test-project"

#: A second project the peer is NOT allowed to access — for ACL refusal.
_PROJECT_DENIED = "project-secret"

#: A second SHAREABLE project — for the ACL-hardening matrix (seeded, but
#: visible to the peer only when its allowed set / shared union says so).
_PROJECT_OTHER = "project-other"

#: A2A id of the single configured peer (the mesh in these tests).
_PEER_ID = "mnemos-A"

#: Agent slug used in seeded memories + imported records.
_AGENT = "gcw-test-agent"

#: Bearer token env var name — value is irrelevant for the mesh server
#: (auth is via the Unix socket + filesystem perms, not bearer tokens),
#: but :class:`PeerConfig` requires the field.
_TOKEN_ENV = "VESMARO_FED_PEER_TEST_TOKEN"


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _settings_with_peer(
    tmp_path: Path,
    *,
    allow_wildcard: bool = False,
    allowed: list[str] | None = None,
    shared: list[str] | None = None,
) -> Settings:
    """Build a :class:`Settings` with one configured peer + isolated store.

    ``allowed``/``shared`` override the peer's ``allowed_projects`` and
    the global ``shared_projects`` (ACL-hardening matrix tests); the
    defaults reproduce the historical single-project fixture.
    """
    if allowed is None:
        allowed = ["*"] if allow_wildcard else [_PROJECT]
    if shared is None:
        shared = [_PROJECT]
    # Pydantic coerces dict kwargs into the nested config models at
    # runtime; the cast keeps mypy --strict happy without changing behaviour.
    settings = Settings(
        **{  # type: ignore[arg-type]  # pydantic dict→model coercion
            "mnemos": {
                "vault_path": str(tmp_path / "vault"),
                "data_dir": str(tmp_path / "data"),
                "db_name": "test_mesh_server.db",
            },
            "embedding": {"provider": "onnx"},
            "scanner": {"enabled": False},
            "federation": FederationConfig(
                shared_projects=shared,
                peers={
                    _PEER_ID: PeerConfig(
                        bearer_token_env=_TOKEN_ENV,
                        allowed_projects=allowed,
                        allowed_types=["decision", "learning"],
                        rate_limit_per_minute=600,
                    ),
                },
            ),
        }
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings with a single peer allowed to access ``_PROJECT`` only."""
    return _settings_with_peer(tmp_path)


@pytest.fixture
def manager(settings: Settings) -> Generator[MemoryManager, None, None]:
    """Real :class:`MemoryManager` against a tmp SQLite store.

    The embedder is mocked so the tests do not require the ONNX runtime.
    """
    mgr = MemoryManager(settings)
    # Stub the embedder so MemoryManager construction + search work
    # without the real ONNX model download.
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    yield mgr
    mgr.close()


@pytest.fixture
def seeded_manager(manager: MemoryManager) -> MemoryManager:
    """Manager with two seeded memories (one decision, one learning)."""
    manager.add(
        MemoryCreate(
            content="We chose bearer+TOTP 2FA for remote sessions.",
            title="ADR-0014 auth decision",
            tags=[f"project:{_PROJECT}", f"agent:{_AGENT}", "mnemos:decision"],
            source=MemorySource.MANUAL,
        ),
        project=_PROJECT,
        agent=_AGENT,
    )
    manager.add(
        MemoryCreate(
            content="Rate-limiting slowapi needs a reset between test runs.",
            title="Rate-limit fixture gotcha",
            tags=[f"project:{_PROJECT}", f"agent:{_AGENT}", "mnemos:learning"],
            source=MemorySource.MANUAL,
        ),
        project=_PROJECT,
        agent=_AGENT,
    )
    return manager


@pytest.fixture
def server(
    tmp_path: Path,
    settings: Settings,
    seeded_manager: MemoryManager,
) -> Generator[MeshServer, None, None]:
    """Start a :class:`MeshServer` on a tmp Unix socket and stop it after."""
    socket_path = str(tmp_path / "core.sock")
    srv = MeshServer(socket_path, seeded_manager, settings, max_workers=2)
    srv.start()
    yield srv
    srv.stop(grace=0.5)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _channel(server: MeshServer) -> grpc.Channel:
    """Open an insecure gRPC channel to the running server's Unix socket."""
    return grpc.insecure_channel(f"unix://{server.socket_path}")


def _stub(server: MeshServer) -> Any:
    """Build a ``MnemosCoreStub`` against the running server."""
    return _mesh_gen.core_pb2_grpc.MnemosCoreStub(_channel(server))


def _wait_for_server(server: MeshServer, timeout: float = 2.0) -> None:
    """Block until the server's gRPC channel is ready (or timeout)."""
    channel = _channel(server)
    grpc.channel_ready_future(channel).result(timeout=timeout)


def _make_compact_record(
    *,
    record_id: str = "fed:gcw-test-agent:abc-123",
    project: str = _PROJECT,
    agent: str = _AGENT,
    title: str = "Imported decision",
    summary: str = "A decision imported from a peer via the mesh.",
    tags: list[str] | None = None,
) -> CompactRecord:
    """Build a :class:`CompactRecord` for :rpc:`WriteMemory` tests."""
    if tags is None:
        tags = [f"project:{project}", f"agent:{agent}", "mnemos:decision"]
    return CompactRecord(
        id=record_id,
        type="decision",
        title=title,
        summary=summary,
        key_points=["point one"],
        tags=tags,
        source_agent=agent,
        timestamp="2026-07-22T10:00:00Z",
    )


def _to_proto_record(record: CompactRecord) -> Any:
    """Marshal a :class:`CompactRecord` to the protobuf message for the request."""
    return _mesh_gen.fed_pb2.CompactRecord(
        id=record.id,
        type=record.type,
        title=record.title,
        summary=record.summary,
        key_points=list(record.key_points),
        tags=list(record.tags),
        source_agent=record.source_agent,
        timestamp=record.timestamp,
    )


# ── Lifecycle ────────────────────────────────────────────────────────────────


class TestServerLifecycle:
    def test_server_starts_and_stops_cleanly(
        self,
        tmp_path: Path,
        settings: Settings,
        manager: MemoryManager,
    ) -> None:
        """MeshServer.start() creates the socket; stop() removes it."""
        socket_path = str(tmp_path / "lifecycle.sock")
        srv = MeshServer(socket_path, manager, settings, max_workers=2)
        assert not srv.is_running
        srv.start()
        assert srv.is_running
        assert Path(socket_path).exists()
        srv.stop(grace=0.1)
        assert not srv.is_running
        assert not Path(socket_path).exists()

    def test_context_manager_starts_and_stops(
        self,
        tmp_path: Path,
        settings: Settings,
        manager: MemoryManager,
    ) -> None:
        """MeshServer works as a context manager (start on enter, stop on exit)."""
        socket_path = str(tmp_path / "ctx.sock")
        with MeshServer(socket_path, manager, settings, max_workers=2) as srv:
            assert srv.is_running
            assert Path(socket_path).exists()
        assert not srv.is_running

    def test_double_start_raises(
        self,
        tmp_path: Path,
        settings: Settings,
        manager: MemoryManager,
    ) -> None:
        """Calling start() twice raises RuntimeError (no double-bind)."""
        socket_path = str(tmp_path / "double.sock")
        srv = MeshServer(socket_path, manager, settings, max_workers=2)
        srv.start()
        try:
            with pytest.raises(RuntimeError, match="already started"):
                srv.start()
        finally:
            srv.stop(grace=0.1)

    def test_stop_without_start_is_noop(
        self,
        tmp_path: Path,
        settings: Settings,
        manager: MemoryManager,
    ) -> None:
        """stop() before start() is a safe no-op."""
        socket_path = str(tmp_path / "noop.sock")
        srv = MeshServer(socket_path, manager, settings, max_workers=2)
        srv.stop()  # must not raise


class TestSocketPermissions:
    """Socket/dir modes for the two deployment profiles (W2 wiring).

    Default: owner-only (0600 socket / 0700 dir) — unchanged behaviour.
    ``mesh.socket_group_access: true``: group rw (0660/0770) so a mesh
    binary running as a different uid in the same gid (fsGroup, compose
    ``user:``) can dial the shared-volume socket.
    """

    def test_default_modes_are_owner_only(
        self,
        tmp_path: Path,
        settings: Settings,
        manager: MemoryManager,
    ) -> None:
        socket_path = str(tmp_path / "sock" / "perms_default.sock")
        srv = MeshServer(socket_path, manager, settings, max_workers=2)
        srv.start()
        try:
            assert stat.S_IMODE(Path(socket_path).stat().st_mode) == 0o600
            assert stat.S_IMODE(Path(socket_path).parent.stat().st_mode) == 0o700
        finally:
            srv.stop(grace=0.1)

    def test_group_access_modes(
        self,
        tmp_path: Path,
        settings: Settings,
        manager: MemoryManager,
    ) -> None:
        settings.mesh.socket_group_access = True
        socket_path = str(tmp_path / "sock" / "perms_group.sock")
        srv = MeshServer(socket_path, manager, settings, max_workers=2)
        srv.start()
        try:
            assert stat.S_IMODE(Path(socket_path).stat().st_mode) == 0o660
            assert stat.S_IMODE(Path(socket_path).parent.stat().st_mode) == 0o770
        finally:
            srv.stop(grace=0.1)


# ── Heartbeat ─────────────────────────────────────────────────────────────────


class TestHeartbeat:
    def test_heartbeat_returns_nonempty_version(self, server: MeshServer) -> None:
        """Heartbeat returns healthy=True + a non-empty version string."""
        _wait_for_server(server)
        stub = _stub(server)
        response = stub.Heartbeat(
            _mesh_gen.core_pb2.HeartbeatRequest(peer_id=_PEER_ID, component="mesh"),
            timeout=2.0,
        )
        assert response.healthy is True
        assert response.version
        assert response.version.startswith("mnemos ")

    def test_heartbeat_uptime_is_nonnegative(self, server: MeshServer) -> None:
        """Uptime seconds is >= 0 for a freshly started server."""
        _wait_for_server(server)
        stub = _stub(server)
        response = stub.Heartbeat(
            _mesh_gen.core_pb2.HeartbeatRequest(peer_id=_PEER_ID, component="mesh"),
            timeout=2.0,
        )
        assert response.uptime_seconds >= 0


# ── ListMemories ─────────────────────────────────────────────────────────────


class TestListMemories:
    def test_list_returns_seeded_memories(self, server: MeshServer) -> None:
        """ListMemories returns the seeded compact records (moderation-processed)."""
        _wait_for_server(server)
        stub = _stub(server)
        response = stub.ListMemories(
            _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]),
            timeout=2.0,
        )
        assert response.total >= 2
        assert len(response.records) >= 2
        # Each record is a CompactRecord with an id, type, title.
        for rec in response.records:
            assert rec.id
            assert rec.type in {"decision", "learning", "session"}
            assert rec.title

    def test_list_acl_denied_scope_refuses(self, server: MeshServer) -> None:
        """ListMemories with a disallowed project → PERMISSION_DENIED."""
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ListMemories(
                _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT_DENIED]),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_list_filter_by_type(self, server: MeshServer) -> None:
        """ListMemories with types=["decision"] returns only decision records."""
        _wait_for_server(server)
        stub = _stub(server)
        response = stub.ListMemories(
            _mesh_gen.core_pb2.ListMemoriesRequest(
                projects=[_PROJECT],
                types=["decision"],
            ),
            timeout=2.0,
        )
        assert len(response.records) >= 1
        for rec in response.records:
            assert rec.type == "decision"

    def test_list_no_federate_excluded_by_default(self, server: MeshServer) -> None:
        """Records tagged mnemos:no-federate are excluded by default."""
        _wait_for_server(server)
        # Seed a no-federate record directly in the store.
        servicer = server.servicer
        assert servicer is not None
        servicer._manager.add(
            MemoryCreate(
                content="secret access key AKIAEXAMPLE123",
                title="Should be excluded",
                tags=[
                    f"project:{_PROJECT}",
                    f"agent:{_AGENT}",
                    "mnemos:decision",
                    "mnemos:no-federate",
                ],
                source=MemorySource.MANUAL,
            ),
            project=_PROJECT,
            agent=_AGENT,
        )
        stub = _stub(server)
        response = stub.ListMemories(
            _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]),
            timeout=2.0,
        )
        for rec in response.records:
            assert "mnemos:no-federate" not in list(rec.tags)


# ── ListMemories cursor contract (ADR-0020) ──────────────────────────────────


class TestListMemoriesCursors:
    """ADR-0020: core mints opaque cursors, resumes exactly, rejects garbage."""

    def _list(self, server: MeshServer, **kwargs: Any) -> Any:
        """Call ListMemories on the running server with optional fields."""
        _wait_for_server(server)
        stub = _stub(server)
        return stub.ListMemories(
            _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT], **kwargs),
            timeout=2.0,
        )

    @staticmethod
    def _seed(servicer: Any, title: str, *, tag: str = "mnemos:decision") -> None:
        """Add one federable memory through the servicer's manager."""
        servicer._manager.add(
            MemoryCreate(
                content=f"Cursor-test content for {title}.",
                title=title,
                tags=[f"project:{_PROJECT}", f"agent:{_AGENT}", tag],
                source=MemorySource.MANUAL,
            ),
            project=_PROJECT,
            agent=_AGENT,
        )

    def test_mints_nonempty_stable_cursor(self, server: MeshServer) -> None:
        """Non-empty response → non-empty cursor; a repeated call mints the
        same cursor (deterministic mint from the same storage position)."""
        first = self._list(server)
        assert len(first.records) >= 2
        assert first.cursor, "non-empty page must mint a non-empty cursor"
        second = self._list(server)
        assert second.cursor == first.cursor

    def test_empty_page_mints_empty_cursor(self, server: MeshServer) -> None:
        """A filter matching nothing → 0 records and an EMPTY cursor
        (byte-identical to a pre-ADR-0020 core: old-mesh fresh-subscribe)."""
        response = self._list(server, types=["nonexistent-type"])
        assert len(response.records) == 0
        assert response.cursor == ""
        assert response.has_more is False

    def test_revision_stamped_monotonic(self, server: MeshServer) -> None:
        """Every exported record carries revision > 0 (the storage rowid,
        ADR-0021 Q10.6) and revisions strictly increase across the page
        (rowid-ASC forward walk)."""
        response = self._list(server)
        assert len(response.records) >= 2
        revisions = [rec.revision for rec in response.records]
        assert all(r > 0 for r in revisions)
        assert revisions == sorted(revisions)
        assert len(set(revisions)) == len(revisions)

    def test_resume_returns_exactly_new_records(self, server: MeshServer) -> None:
        """Resume by a fresh cursor → EXACTLY the records added after the
        checkpoint (no dupes of the delivered set, no gaps)."""
        baseline = self._list(server)
        assert len(baseline.records) >= 2
        served_ids = {rec.id for rec in baseline.records}
        servicer = server.servicer
        assert servicer is not None
        for i in range(3):
            self._seed(servicer, f"Post-checkpoint decision {i}")
        resumed = self._list(server, resume_cursor=baseline.cursor)
        new_ids = {rec.id for rec in resumed.records}
        assert len(resumed.records) == 3
        assert new_ids.isdisjoint(served_ids)
        assert resumed.cursor and resumed.cursor != baseline.cursor
        # Fully caught up: resuming by the newest cursor → nothing new.
        caught_up = self._list(server, resume_cursor=resumed.cursor)
        assert len(caught_up.records) == 0
        assert caught_up.cursor == ""

    def test_resume_priority_over_since(self, server: MeshServer) -> None:
        """A non-empty resume_cursor IGNORES `since` (ADR-0020: checkpoint
        takes priority) — even a since in the far future must not zero the
        resumed page."""
        baseline = self._list(server)
        servicer = server.servicer
        assert servicer is not None
        self._seed(servicer, "Priority-over-since decision")
        resumed = self._list(
            server,
            resume_cursor=baseline.cursor,
            since="2999-01-01T00:00:00",
        )
        assert len(resumed.records) == 1

    def test_garbage_resume_cursor_rejected(self, server: MeshServer) -> None:
        """Malformed/foreign cursors → INVALID_ARGUMENT, never a silent
        empty page (ADR-0020 rule 3).

        Shapes covered: non-base64 garbage; base64 of non-JSON; a valid
        token with an unknown format version; a non-integer rowid; a
        rowid ABOVE the SQLite 2^63-1 ceiling (uncaught it would raise
        OverflowError and kill the RPC with UNKNOWN — review E1); and an
        oversized token (> 128 chars) rejected before any decode work.
        """
        import base64
        import json as _json

        def _token(payload: bytes) -> str:
            return base64.urlsafe_b64encode(payload).rstrip(b"=").decode()

        wrong_version = _token(_json.dumps({"v": 2, "rowid": 1}).encode())
        not_an_int = _token(_json.dumps({"v": 1, "rowid": "one"}).encode())
        not_json = _token(b"not json at all")
        rowid_overflow = _token(_json.dumps({"v": 1, "rowid": 2**63}).encode())
        oversized = "A" * 129
        for garbage in (
            "garbage",
            not_json,
            wrong_version,
            not_an_int,
            rowid_overflow,
            oversized,
        ):
            _wait_for_server(server)
            stub = _stub(server)
            with pytest.raises(grpc.RpcError) as exc_info:
                stub.ListMemories(
                    _mesh_gen.core_pb2.ListMemoriesRequest(
                        projects=[_PROJECT],
                        resume_cursor=garbage,
                    ),
                    timeout=2.0,
                )
            assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT, garbage

    def test_since_path_still_filters(self, server: MeshServer) -> None:
        """Legacy `since` behaviour is unchanged (pre-ADR-0020 meshes):
        a far-past bound returns the full set, a far-future bound returns
        nothing (with an empty cursor)."""
        all_records = self._list(server, since="2000-01-01T00:00:00")
        assert len(all_records.records) >= 2
        none_records = self._list(server, since="2999-01-01T00:00:00")
        assert len(none_records.records) == 0
        assert none_records.cursor == ""

    def test_cursor_pagination_round_trip(self, server: MeshServer) -> None:
        """Full sync by walking resume cursors: pages partition the corpus
        exactly (no dupes, no gaps) and the walk terminates."""
        servicer = server.servicer
        assert servicer is not None
        for i in range(5):
            self._seed(servicer, f"Pagination decision {i}")
        seen: list[str] = []
        cursor = ""
        pages = 0
        while True:
            response = self._list(server, limit=2, resume_cursor=cursor)
            seen.extend(rec.id for rec in response.records)
            pages += 1
            if not response.has_more:
                break
            assert response.cursor, "has_more page must mint a continuation cursor"
            cursor = response.cursor
            assert pages <= 10, "pagination did not terminate"
        total_expected = len(self._list(server).records)
        assert pages >= 3  # 5+2 seeded records at limit=2 must span >= 3 pages
        assert len(seen) == total_expected
        assert len(set(seen)) == len(seen), "pages must not overlap"


# ── WriteMemory ──────────────────────────────────────────────────────────────


class TestWriteMemory:
    def test_write_persists_to_sqlite(self, server: MeshServer) -> None:
        """WriteMemory writes to SQLite — verified via a direct DB query."""
        _wait_for_server(server)
        stub = _stub(server)
        record = _make_compact_record(summary="A clean decision from a peer.")
        response = stub.WriteMemory(
            _mesh_gen.core_pb2.WriteMemoryRequest(
                record=_to_proto_record(record),
                import_mode=_mesh_gen.core_pb2.ImportMode.MERGE,
            ),
            timeout=2.0,
        )
        assert response.written_id
        assert response.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE
        # Verify the memory landed in SQLite.
        servicer = server.servicer
        assert servicer is not None
        memory = servicer._manager.sqlite.get(response.written_id)
        assert memory is not None
        assert memory.project == _PROJECT
        assert memory.agent == _AGENT
        # The federation id is stored in metadata for traceability.
        assert memory.metadata.get("fed_id") == record.id

    def test_write_acl_denied_returns_refused(self, server: MeshServer) -> None:
        """WriteMemory with a disallowed project → PERMISSION_DENIED."""
        _wait_for_server(server)
        stub = _stub(server)
        record = _make_compact_record(project=_PROJECT_DENIED)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.WriteMemory(
                _mesh_gen.core_pb2.WriteMemoryRequest(
                    record=_to_proto_record(record),
                    import_mode=_mesh_gen.core_pb2.ImportMode.MERGE,
                ),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_write_unspecified_mode_rejected(self, server: MeshServer) -> None:
        """WriteMemory with UNSPECIFIED import_mode → INVALID_ARGUMENT."""
        _wait_for_server(server)
        stub = _stub(server)
        record = _make_compact_record()
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.WriteMemory(
                _mesh_gen.core_pb2.WriteMemoryRequest(
                    record=_to_proto_record(record),
                    import_mode=_mesh_gen.core_pb2.ImportMode.IMPORT_MODE_UNSPECIFIED,
                ),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    def test_write_restore_without_confirm_rejected(self, server: MeshServer) -> None:
        """RESTORE without confirm=True → FAILED_PRECONDITION (hard gate)."""
        _wait_for_server(server)
        stub = _stub(server)
        record = _make_compact_record()
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.WriteMemory(
                _mesh_gen.core_pb2.WriteMemoryRequest(
                    record=_to_proto_record(record),
                    import_mode=_mesh_gen.core_pb2.ImportMode.RESTORE,
                    confirm=False,
                ),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.FAILED_PRECONDITION


# ── WriteMemory idempotency (vesmaro #359) ───────────────────────────────────


class TestWriteMemoryIdempotency:
    """Replayed one-shot pulls (mnemos-mesh #34) must not duplicate rows.

    The mesh classifies each response by trigger_code: EXHAUSTIVE →
    Written, ALREADY_EXHAUSTED → Duplicates. These tests pin the core
    side of that contract (#359).
    """

    def _write(self, server: MeshServer, record: CompactRecord) -> Any:
        """One WriteMemory(MERGE, confirm=false) call — the mesh pull shape."""
        _wait_for_server(server)
        stub = _stub(server)
        return stub.WriteMemory(
            _mesh_gen.core_pb2.WriteMemoryRequest(
                record=_to_proto_record(record),
                import_mode=_mesh_gen.core_pb2.ImportMode.MERGE,
                confirm=False,
            ),
            timeout=2.0,
        )

    def _rows_with_fed_id(self, server: MeshServer, fed_id: str) -> list[Any]:
        """All stored memories whose metadata.fed_id equals fed_id."""
        servicer = server.servicer
        assert servicer is not None
        return [
            m
            for m in servicer._manager.sqlite.list_all(limit=1000)
            if m.metadata.get("fed_id") == fed_id
        ]

    def test_replay_same_fed_id_returns_already_exhausted_same_id(self, server: MeshServer) -> None:
        """Same record twice: EXHAUSTIVE then ALREADY_EXHAUSTED, same
        storage id, exactly one stored row."""
        record = _make_compact_record(summary="Idempotent replay candidate.")
        first = self._write(server, record)
        assert first.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE
        second = self._write(server, record)
        assert second.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.ALREADY_EXHAUSTED
        assert second.written_id == first.written_id
        assert second.mode_applied == _mesh_gen.core_pb2.ImportMode.MERGE
        rows = self._rows_with_fed_id(server, record.id)
        assert len(rows) == 1

    def test_triple_replay_mesh_scenario(self, server: MeshServer) -> None:
        """The mnemos-mesh #34 one-shot pull replay, end to end: N replays
        → 1 Written + (N-1) Duplicates in mesh Stats terms, one row."""
        record = _make_compact_record(summary="Mesh replay scenario.")
        ids = set()
        outcomes = []
        for _ in range(3):
            resp = self._write(server, record)
            ids.add(resp.written_id)
            outcomes.append(resp.trigger_code)
        assert outcomes == [
            _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE,
            _mesh_gen.fed_pb2.TriggerCodes.ALREADY_EXHAUSTED,
            _mesh_gen.fed_pb2.TriggerCodes.ALREADY_EXHAUSTED,
        ]
        assert len(ids) == 1
        assert len(self._rows_with_fed_id(server, record.id)) == 1

    def test_duplicate_refreshes_last_fed_at_without_rewrite(self, server: MeshServer) -> None:
        """A replay refreshes metadata.last_fed_at; content/title stay."""
        record = _make_compact_record(summary="Freshness touch candidate.")
        first = self._write(server, record)
        servicer = server.servicer
        assert servicer is not None
        before = servicer._manager.sqlite.get(first.written_id)
        assert before is not None
        assert "last_fed_at" in before.metadata  # seeded at first import (#359)
        ts_before = before.metadata["last_fed_at"]
        self._write(server, record)
        after = servicer._manager.sqlite.get(first.written_id)
        assert after is not None
        assert after.metadata["last_fed_at"] > ts_before
        # No rewrite: the stored projection is untouched.
        assert after.content == before.content
        assert after.title == before.title

    def test_fallback_title_source_agent_without_fed_id(self, server: MeshServer) -> None:
        """Records arriving without a fed id dedup on title+source_agent."""
        record = _make_compact_record(
            record_id="", summary="No-fed-id record body.", title="No-fed-id title"
        )
        first = self._write(server, record)
        assert first.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE
        second = self._write(server, record)
        assert second.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.ALREADY_EXHAUSTED
        assert second.written_id == first.written_id

    def test_no_provenance_records_are_never_deduped(self, server: MeshServer) -> None:
        """No fed_id AND no source_agent → plain API semantics: every call
        creates a fresh row (обычные API-записи не трогаем)."""
        record = _make_compact_record(
            record_id="",
            agent="",
            summary="Provenance-less body.",
            title="No provenance",
            tags=[f"project:{_PROJECT}", "mnemos:decision"],
        )
        assert record.source_agent == ""
        first = self._write(server, record)
        second = self._write(server, record)
        assert first.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE
        assert second.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE
        assert second.written_id != first.written_id

    def test_different_fed_id_creates_new_record(self, server: MeshServer) -> None:
        """Same title+source_agent but a different fed_id → a new row (the
        fed_id branch is the priority key and never falls through)."""
        record_a = _make_compact_record(
            record_id="fed:gcw-test-agent:aaa", summary="Body A.", title="Shared headline"
        )
        record_b = _make_compact_record(
            record_id="fed:gcw-test-agent:bbb", summary="Body B.", title="Shared headline"
        )
        first = self._write(server, record_a)
        second = self._write(server, record_b)
        assert first.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE
        assert second.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE
        assert second.written_id != first.written_id

    def test_different_title_creates_new_record(self, server: MeshServer) -> None:
        """Fallback path: same source_agent, different title → new row."""
        record_a = _make_compact_record(record_id="", summary="Body A.", title="Headline one")
        record_b = _make_compact_record(record_id="", summary="Body B.", title="Headline two")
        first = self._write(server, record_a)
        second = self._write(server, record_b)
        assert first.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE
        assert second.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE
        assert second.written_id != first.written_id

    def test_regular_api_record_not_matched_by_fallback(self, server: MeshServer) -> None:
        """A federated record sharing title+agent with a LOCAL API memory
        (no federation metadata) still imports — the fallback only matches
        previously imported federated rows."""
        # The seeded fixture has a local "ADR-0014 auth decision" memory
        # authored by _AGENT without any fed_* metadata.
        record = _make_compact_record(
            record_id="",
            summary="Federated namesake of a local note.",
            title="ADR-0014 auth decision",
        )
        resp = self._write(server, record)
        assert resp.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE
        servicer = server.servicer
        assert servicer is not None
        namesakes = [
            m
            for m in servicer._manager.sqlite.list_all(limit=1000)
            if m.title == "ADR-0014 auth decision"
        ]
        assert len(namesakes) == 2  # the local seed + the federated import


# ── GetSubscriptionState ──────────────────────────────────────────────────────


class TestGetSubscriptionState:
    def test_subscription_state_returns_empty_cursor(self, server: MeshServer) -> None:
        """GetSubscriptionState returns an empty cursor (M4: mesh-side persistence in M5)."""
        _wait_for_server(server)
        stub = _stub(server)
        response = stub.GetSubscriptionState(
            _mesh_gen.core_pb2.GetSubscriptionStateRequest(
                peer_id=_PEER_ID,
                project_scope=_PROJECT,
            ),
            timeout=2.0,
        )
        assert response.cursor == ""
        assert response.last_rev == 0
        assert response.last_sync_timestamp == ""

    def test_subscription_state_acl_denied(self, server: MeshServer) -> None:
        """GetSubscriptionState with a disallowed scope → PERMISSION_DENIED."""
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.GetSubscriptionState(
                _mesh_gen.core_pb2.GetSubscriptionStateRequest(
                    peer_id=_PEER_ID,
                    project_scope=_PROJECT_DENIED,
                ),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED


# ── ACL hardening (vesmaro#371/#369 family) ───────────────────────────────────


#: gRPC metadata asserting an UNKNOWN peer id (identity spoof attempt).
_GHOST_METADATA = (("x-mnemos-peer-id", "mnemos-ghost"),)


@pytest.fixture
def acl_server_factory(tmp_path: Path) -> Generator[Any, None, None]:
    """Factory for ACL-matrix servers: one peer, N seeded projects.

    Yields a callable taking ``allowed`` / ``shared`` / ``seed_projects``
    (ACL-hardening matrix axes) that starts an isolated ``MeshServer``
    with one seeded federable memory per seed project. Multiple servers
    per test are supported (each gets its own subdir + socket). All
    servers and managers are torn down after the test.
    """
    started: list[tuple[MeshServer, MemoryManager]] = []

    def _make(
        *,
        allowed: list[str],
        shared: list[str],
        seed_projects: tuple[str, ...] = (_PROJECT, _PROJECT_OTHER),
    ) -> MeshServer:
        n = len(started)
        base = tmp_path / f"acl{n}"
        settings = _settings_with_peer(base, allowed=allowed, shared=shared)
        manager = MemoryManager(settings)
        mock_embedder = MagicMock()
        mock_embedder.embed.return_value = [0.1] * 384
        manager._embedder = mock_embedder
        for project in seed_projects:
            manager.add(
                MemoryCreate(
                    content=f"ACL-hardening seed record for {project}.",
                    title=f"Seed {project}",
                    tags=[f"project:{project}", f"agent:{_AGENT}", "mnemos:decision"],
                    source=MemorySource.MANUAL,
                ),
                project=project,
                agent=_AGENT,
            )
        srv = MeshServer(str(base / "core.sock"), manager, settings, max_workers=2)
        srv.start()
        started.append((srv, manager))
        return srv

    yield _make
    for srv, manager in started:
        srv.stop(grace=0.5)
        manager.close()


class TestAclHardeningFailClosed:
    """Regression matrix for the fail-open family (vesmaro#371/#369).

    Fix- principles ratified by these tests: (a) no implicit "all" — an
    empty effective allowed set (empty allow-list, or "*" with an empty
    shared_projects) denies reads AND writes; (b) an unscoped request is
    the intersection with the peer's allowed set, never the whole corpus;
    (c) every data path is ACL-gated before serve/write, unknown peers
    included.
    """

    def test_unscoped_request_returns_only_allowed_projects(self, acl_server_factory: Any) -> None:
        """(i) Unscoped ListMemories from a limited peer → ONLY its projects.

        The live #371/#369 pair: the peer's allowed set is [_PROJECT] while
        the store also holds _PROJECT_OTHER — an unscoped pull must return
        exactly the allowed project, never the foreign records.
        """
        server = acl_server_factory(allowed=[_PROJECT], shared=[_PROJECT, _PROJECT_OTHER])
        _wait_for_server(server)
        stub = _stub(server)
        response = stub.ListMemories(
            _mesh_gen.core_pb2.ListMemoriesRequest(),
            timeout=2.0,
        )
        assert len(response.records) == 1, "only the allowed project's record"
        for rec in response.records:
            assert f"project:{_PROJECT}" in list(rec.tags)
            assert f"project:{_PROJECT_OTHER}" not in list(rec.tags)

    def test_unscoped_request_wildcard_peer_returns_shared_union(
        self, acl_server_factory: Any
    ) -> None:
        """Wildcard peer + non-empty shared → unscoped pull = shared union."""
        server = acl_server_factory(allowed=["*"], shared=[_PROJECT, _PROJECT_OTHER])
        _wait_for_server(server)
        stub = _stub(server)
        response = stub.ListMemories(
            _mesh_gen.core_pb2.ListMemoriesRequest(),
            timeout=2.0,
        )
        assert len(response.records) == 2, "both shared projects are served"

    def test_empty_allowed_set_denies_read_and_write(self, acl_server_factory: Any) -> None:
        """(ii) Empty allowed_projects → PERMISSION_DENIED on read AND write."""
        server = acl_server_factory(allowed=[], shared=[_PROJECT], seed_projects=(_PROJECT,))
        _wait_for_server(server)
        stub = _stub(server)
        # Read: unscoped …
        with pytest.raises(grpc.RpcError) as unscoped:
            stub.ListMemories(_mesh_gen.core_pb2.ListMemoriesRequest(), timeout=2.0)
        assert unscoped.value.code() == grpc.StatusCode.PERMISSION_DENIED
        # … and scoped (the pre-existing membership deny stays).
        with pytest.raises(grpc.RpcError) as scoped:
            stub.ListMemories(
                _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]), timeout=2.0
            )
        assert scoped.value.code() == grpc.StatusCode.PERMISSION_DENIED
        # Write: a tagged record the peer would otherwise accept.
        with pytest.raises(grpc.RpcError) as write:
            stub.WriteMemory(
                _mesh_gen.core_pb2.WriteMemoryRequest(
                    record=_to_proto_record(_make_compact_record()),
                    import_mode=_mesh_gen.core_pb2.ImportMode.MERGE,
                ),
                timeout=2.0,
            )
        assert write.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_wildcard_with_empty_shared_denies_everything(self, acl_server_factory: Any) -> None:
        """(iii) '*' allow-list + empty shared_projects → DENIED, not 'all'.

        Pre-fix this was the #369 fail-open: the wildcard resolved to an
        empty shared union, the empty union became ``projects=None``, and
        the unscoped query returned EVERY project in the store.
        """
        server = acl_server_factory(allowed=["*"], shared=[], seed_projects=(_PROJECT,))
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as unscoped:
            stub.ListMemories(_mesh_gen.core_pb2.ListMemoriesRequest(), timeout=2.0)
        assert unscoped.value.code() == grpc.StatusCode.PERMISSION_DENIED
        with pytest.raises(grpc.RpcError) as scoped:
            stub.ListMemories(
                _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]), timeout=2.0
            )
        assert scoped.value.code() == grpc.StatusCode.PERMISSION_DENIED
        with pytest.raises(grpc.RpcError) as write:
            stub.WriteMemory(
                _mesh_gen.core_pb2.WriteMemoryRequest(
                    record=_to_proto_record(_make_compact_record()),
                    import_mode=_mesh_gen.core_pb2.ImportMode.MERGE,
                ),
                timeout=2.0,
            )
        assert write.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_unknown_peer_denied_read_and_write(self, acl_server_factory: Any) -> None:
        """(iv) Unknown peer (spoofed metadata) → DENIED on every data path.

        Pre-fix the unscoped read from an unknown peer skipped the ACL
        gate AND fell into the empty-allowed-set → unfiltered query — a
        full-corpus leak. The scoped read/write were already denied via
        the scope check; they stay denied.
        """
        server = acl_server_factory(
            allowed=[_PROJECT], shared=[_PROJECT], seed_projects=(_PROJECT,)
        )
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as unscoped:
            stub.ListMemories(
                _mesh_gen.core_pb2.ListMemoriesRequest(),
                timeout=2.0,
                metadata=_GHOST_METADATA,
            )
        assert unscoped.value.code() == grpc.StatusCode.PERMISSION_DENIED
        with pytest.raises(grpc.RpcError) as scoped:
            stub.ListMemories(
                _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]),
                timeout=2.0,
                metadata=_GHOST_METADATA,
            )
        assert scoped.value.code() == grpc.StatusCode.PERMISSION_DENIED
        with pytest.raises(grpc.RpcError) as write:
            stub.WriteMemory(
                _mesh_gen.core_pb2.WriteMemoryRequest(
                    record=_to_proto_record(_make_compact_record()),
                    import_mode=_mesh_gen.core_pb2.ImportMode.MERGE,
                ),
                timeout=2.0,
                metadata=_GHOST_METADATA,
            )
        assert write.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_untagged_record_write_denied_even_for_allowed_peer(
        self, acl_server_factory: Any
    ) -> None:
        """A record WITHOUT a project: tag cannot be ACL'd → DENIED.

        Pre-fix ``if project and …`` skipped the gate entirely and the
        record landed in the project-less namespace regardless of the
        peer's allow-list. Also asserts nothing was persisted.
        """
        server = acl_server_factory(
            allowed=[_PROJECT], shared=[_PROJECT], seed_projects=(_PROJECT,)
        )
        _wait_for_server(server)
        stub = _stub(server)
        record = _make_compact_record(tags=[f"agent:{_AGENT}", "mnemos:decision"])
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.WriteMemory(
                _mesh_gen.core_pb2.WriteMemoryRequest(
                    record=_to_proto_record(record),
                    import_mode=_mesh_gen.core_pb2.ImportMode.MERGE,
                ),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED
        servicer = server.servicer
        assert servicer is not None
        assert (
            servicer._manager.sqlite.find_federated_duplicate(
                fed_id=record.id,
                title=record.title,
                source_agent=record.source_agent,
            )
            is None
        ), "the refused record must not be persisted"

    def test_wildcard_write_to_non_shared_project_denied(self, acl_server_factory: Any) -> None:
        """Wildcard write is bounded by shared_projects (read/write symmetry).

        Pre-fix ``_acl_allows`` short-circuited '*' → True for ANY
        project, letting a wildcard peer write into projects the operator
        never shared. The read side never served those projects; now the
        write side matches.
        """
        server = acl_server_factory(allowed=["*"], shared=[_PROJECT], seed_projects=(_PROJECT,))
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.WriteMemory(
                _mesh_gen.core_pb2.WriteMemoryRequest(
                    record=_to_proto_record(_make_compact_record(project=_PROJECT_OTHER)),
                    import_mode=_mesh_gen.core_pb2.ImportMode.MERGE,
                ),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_wildcard_scoped_read_of_non_shared_project_denied(
        self, acl_server_factory: Any
    ) -> None:
        """Wildcard scoped read of a non-shared project → DENIED (safety net)."""
        server = acl_server_factory(allowed=["*"], shared=[_PROJECT], seed_projects=(_PROJECT,))
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ListMemories(
                _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT_OTHER]),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED
