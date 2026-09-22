"""Boundary wiring: config → store, and the two collection points.

Phase A integration of the vitals plane (ADR-0026; ArchCom core
``7ec9dda3``). The store is created once per process from Settings;
the MCP handler and the ``pre_llm_call`` hook call
``MemoryManager.record_assemble_vitals(result)`` after the assemble
result exists. Everything here is non-fatal by contract: a broken or
disabled metrics plane never blocks the server.
"""

from __future__ import annotations

import logging

from vesmaro.config import Settings
from vesmaro.metrics.schema import SIDECAR_FILENAME
from vesmaro.metrics.sink import MetricsStore

logger = logging.getLogger("vesmaro.metrics.boundary")


def create_vitals_store(settings: Settings) -> MetricsStore | None:
    """Build the sidecar store from Settings, or ``None`` when disabled.

    Default-on in local-first (ArchCom ``7ec9dda3`` + owner directive
    2026-09-20): a local attacker holding the sidecar already holds the
    main store with full content, so collection adds no exposure.

    C3 config-lint: per-request rows carry a ``session`` column; under
    ``auth_enabled=true`` (multi-principal deployment) collection
    REFUSES to start without the explicit
    ``vitals.ack_session_collection=true`` acknowledgement. The server
    continues without metrics — refusal, not crash.
    """
    vitals = settings.vitals
    if not vitals.enabled:
        logger.info("vitals: disabled by config (vitals.enabled=false)")
        return None
    if settings.api.auth_enabled and not vitals.ack_session_collection:
        logger.warning(
            "vitals: per-request collection REFUSED (C3 config-lint) —"
            " api.auth_enabled=true without vitals.ack_session_collection=true;"
            " the server continues without metrics"
        )
        return None
    try:
        settings.mnemos.data_dir.mkdir(parents=True, exist_ok=True)
        return MetricsStore(settings.mnemos.data_dir / SIDECAR_FILENAME)
    except Exception as exc:
        logger.warning("vitals: store unavailable (non-fatal): %s", exc)
        return None
