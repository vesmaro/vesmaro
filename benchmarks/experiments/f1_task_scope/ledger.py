"""F1 adjudication ledger — the analyzed-qid state (the e2-gov pattern).

The run id core couples every run manifest to the exact adjudication
state it ran under (E0 §6.6 / F1 §4.3): the ledger holds the analyzed
qid set, audit rejects and pool replacements. Bootstrap state (this
wave): zero rejects — all 264 birth-declared pairs analyzed; the §3.5
blind applicability audit (``corpus.audit_subsample``, 53 pairs) is
the future human judgment layer; a rejected pair is replaced from the
committed surplus pool and the ledger state hash changes, which makes
any post-adjudication run a NEW run id — the anti-cherry-picking
coupling, exactly as e2-gov did for the G-gov stratum.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from benchmarks.experiments.f1_task_scope import corpus as f1_corpus

LEDGER_PATH = Path(__file__).resolve().parent / "adjudication_ledger.json"

STRATUM = f1_corpus.STRATUM_VERSION
SPEC = "docs/experiments/f1-task-scope.md §3.5, §4.4, §6.6, §9"


def initial_ledger() -> dict[str, Any]:
    """Bootstrap ledger: every analyzed pair active, zero rejects."""
    analyzed = sorted(q.qid for q in f1_corpus.build_corpus().analyzed_queries())
    return {
        "stratum": STRATUM,
        "spec": SPEC,
        "denominators": dict(f1_corpus.ANALYZED_COUNTS),
        "analyzed_qids": analyzed,
        "rejects": [],
        "replacements": [],
    }


def load_ledger(path: Path | None = None) -> dict[str, Any]:
    ledger_path = path if path is not None else LEDGER_PATH
    return json.loads(ledger_path.read_text())


def save_ledger(ledger: dict[str, Any], path: Path | None = None) -> None:
    ledger_path = path if path is not None else LEDGER_PATH
    ledger_path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")


def ledger_state_hash(ledger: dict[str, Any]) -> str:
    """sha256 over the canonical ledger state (the manifest coupling)."""
    return hashlib.sha256(
        json.dumps(ledger, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def active_analyzed_queries(ledger: dict[str, Any]) -> tuple[f1_corpus.GoldQuery, ...]:
    """The analyzed query set under the CURRENT ledger state.

    Initial state == the birth-declared analyzed set; after a rejection
    the rejected qids drop out and their surplus replacements (same
    stratum, same axis where the pool allows) enter. Fails loud when
    the denominators drift off 192/48/24 (replacements must restore the
    registered stratum sizes exactly).
    """
    corpus = f1_corpus.build_corpus()
    by_qid = {q.qid: q for q in corpus.queries}
    rejected = {r if isinstance(r, str) else r["qid"] for r in ledger["rejects"]}
    replaced = [r if isinstance(r, str) else r["qid"] for r in ledger["replacements"]]
    active_qids = [q for q in ledger["analyzed_qids"] if q not in rejected] + replaced
    active = tuple(by_qid[qid] for qid in sorted(set(active_qids)))
    per_stratum: dict[str, int] = {}
    for q in active:
        per_stratum[q.stratum] = per_stratum.get(q.stratum, 0) + 1
    if per_stratum != f1_corpus.ANALYZED_COUNTS:
        raise AssertionError(
            f"active analyzed strata {per_stratum} != registered {f1_corpus.ANALYZED_COUNTS}"
        )
    return active


def record_rejection(ledger: dict[str, Any], qid: str, replacement_qid: str) -> dict[str, Any]:
    """Reject one analyzed pair, promoting its surplus replacement.

    A copy is returned (the caller owns persistence); the replacement
    must be a surplus pair of the SAME stratum (and axis, where the
    pool has one) — the E0 §3.1 replacement discipline.
    """
    corpus = f1_corpus.build_corpus()
    by_qid = {q.qid: q for q in corpus.queries}
    if qid not in by_qid or by_qid[qid].surplus:
        raise ValueError(f"{qid!r} is not an analyzed pair")
    if replacement_qid not in by_qid or not by_qid[replacement_qid].surplus:
        raise ValueError(f"{replacement_qid!r} is not a surplus replacement pair")
    if by_qid[qid].stratum != by_qid[replacement_qid].stratum:
        raise ValueError("replacement must come from the same stratum")
    new_ledger = json.loads(json.dumps(ledger))  # deep copy
    new_ledger["rejects"].append(qid)
    new_ledger["replacements"].append(replacement_qid)
    active_analyzed_queries(new_ledger)  # validates the 192/48/24 shape
    return new_ledger


if __name__ == "__main__":  # pragma: no cover — bootstrap regeneration helper
    state = initial_ledger()
    save_ledger(state)
    print(
        f"f1 ledger: bootstrap written to {LEDGER_PATH} "
        f"({len(state['analyzed_qids'])} analyzed pairs, "
        f"state_sha256={ledger_state_hash(state)[:12]}…)"
    )
