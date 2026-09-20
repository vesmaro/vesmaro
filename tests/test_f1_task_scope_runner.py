"""F1 task-scope runner — machinery gates over the REAL corpus (epic #308).

RUNNER-MACHINERY gates, not an experiment run: collect-only executes
every arm, validates the artifacts and persists NOTHING. The first
recorded F1 run stays an owner-gated deliberate decision after this
infrastructure merges (F1 §4.3; the hard constraint: NEVER --record in
this wave — the record-path tests below exercise ``record_run`` and the
CLI on THROWAWAY directories with a PATCHED collect, never the real
runs dir, never the real E-file).

Pins (F1 §4.3 / §4.5 / §2.8):

* dry run over the real corpus produces a WELL-FORMED manifest +
  per-query outcome tuples without persisting a run;
* the default invocation REFUSES to record (message + zero writes +
  the E-file untouched); ``--record`` writes write-once artifacts and
  appends the §9 run-ledger entry; a re-record fails loud;
* BLAKE2b fingerprint determinism (same seed → same fingerprint; any
  mutation → a different fingerprint);
* equal-budget enforcement (every assembly at 2048 — breach fails loud);
* V1-V6 executed and logged in the manifest; any breach = void run;
* determinism of a collect-only dry run: two collects over fresh stores
  → byte-identical outcome tuples and the identical content-addressed
  run id;
* the §6.5 anchor quartet lives in ``tests/test_f1_power.py``.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from benchmarks.experiments.f1_task_scope import corpus as f1_corpus
from benchmarks.experiments.f1_task_scope import ledger as f1_ledger
from benchmarks.experiments.f1_task_scope import runner

_QID_RE = {
    "t_gold": "ft-\\d{3}-(ph|pr)",
    "x_gold": "fx-\\d{3}-(ph|pr)",
    "l_neg": "fl-\\d{3}-(ph|pr)",
}


@pytest.fixture(scope="module")
def collected() -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """One full collect over the real corpus (all four arms)."""
    manifest, outcomes = runner.collect_run()
    yield manifest, outcomes


# ── corpus: shape, fingerprint, gold coverage (§3) ────────────────────────────


def test_corpus_shape_locked_at_registration() -> None:
    corpus = f1_corpus.build_corpus()
    counts = f1_corpus.segment_counts(corpus)
    assert counts == {
        "task_prose": 144,
        "task_code": 96,
        "task_doc": 120,
        "shared": 120,
        "checkpoint": 278,
        "misc": 194,
        "canary": 8,
    }
    assert len(corpus.rows) == 960
    task_total = counts["task_prose"] + counts["task_code"] + counts["task_doc"]
    assert task_total == 360  # task mass 37.5% of 960 (§3.4)


def test_corpus_fingerprint_deterministic_and_mutation_sensitive() -> None:
    first = f1_corpus.build_corpus()
    second = f1_corpus.build_corpus()
    assert f1_corpus.corpus_fingerprint(first) == f1_corpus.corpus_fingerprint(second)
    # any content mutation → a different fingerprint (§4.2)
    mutated_row = dataclasses.replace(first.rows[0], content=first.rows[0].content + " drift")
    mutated = dataclasses.replace(first, rows=(mutated_row, *first.rows[1:]))
    assert f1_corpus.corpus_fingerprint(mutated) != f1_corpus.corpus_fingerprint(first)
    # a gold reassignment (query-side mutation) → a different fingerprint
    q0 = first.queries[0]
    mutated_q = dataclasses.replace(
        first, queries=(dataclasses.replace(q0, gold_slug="other"), *first.queries[1:])
    )
    assert f1_corpus.corpus_fingerprint(mutated_q) != f1_corpus.corpus_fingerprint(first)


def test_task_display_committed_and_complete() -> None:
    assert set(f1_corpus.TASK_DISPLAY) == set(f1_corpus.TASKS)
    for slug, display in f1_corpus.TASK_DISPLAY.items():
        assert f1_corpus.TASK_TAG_RE.match(f"task:{slug}")
        assert display and display != slug


def test_analyzed_denominators_and_session_shape() -> None:
    corpus = f1_corpus.build_corpus()
    analyzed = corpus.analyzed_queries()
    per_stratum: dict[str, int] = {}
    for q in analyzed:
        per_stratum[q.stratum] = per_stratum.get(q.stratum, 0) + 1
    assert per_stratum == f1_corpus.ANALYZED_COUNTS  # 192 / 48 / 24
    for task in f1_corpus.TASKS:
        t_q = [q for q in analyzed if q.stratum == "t_gold" and q.current_task == task]
        x_q = [q for q in analyzed if q.stratum == "x_gold" and q.current_task == task]
        assert len(t_q) == 24 and len(x_q) == 6  # the G3b session shape


def test_lens_axis_contract_generator_pinned() -> None:
    """Code-axis queries activate the CODE lens; every other axis never
    does (the L-neg trap family stays inert) — the generator pins it."""
    from vesmaro.lens import Lens, lens_active

    for q in f1_corpus.build_corpus().queries:
        active = lens_active(Lens.CODE, query=q.text)
        assert active == (q.axis == "code"), q.qid


def test_audit_subsample_frozen_rule() -> None:
    subsample = f1_corpus.audit_subsample()
    analyzed_total = sum(f1_corpus.ANALYZED_COUNTS.values())
    assert len(subsample) == 53  # ceil(20% of 264)
    assert len(subsample) == -(-analyzed_total * 20 // 100)
    again = f1_corpus.audit_subsample()
    assert [p["pair_id"] for p in subsample] == [p["pair_id"] for p in again]
    for pair in subsample:
        # arm-stripped by construction (§4.4): no arm ids, no provenance
        assert set(pair) == {
            "pair_id",
            "qid",
            "query_text",
            "gold_slug",
            "gold_title",
            "gold_content",
        }


# ── ledger: bootstrap state, replacements, hash coupling ──────────────────────


def test_committed_ledger_is_the_bootstrap_state() -> None:
    committed = f1_ledger.load_ledger()
    assert committed == f1_ledger.initial_ledger()
    assert committed["rejects"] == [] and committed["replacements"] == []
    active = f1_ledger.active_analyzed_queries(committed)
    assert len(active) == 264


def test_ledger_rejection_swaps_from_surplus_pool() -> None:
    corpus = f1_corpus.build_corpus()
    ledger = f1_ledger.initial_ledger()
    original_hash = f1_ledger.ledger_state_hash(ledger)
    t_analyzed = next(q for q in corpus.analyzed_queries() if q.stratum == "t_gold")
    t_surplus = next(q for q in corpus.queries if q.surplus and q.stratum == "t_gold")
    x_surplus = next(q for q in corpus.queries if q.surplus and q.stratum == "x_gold")
    # cross-stratum replacement is refused
    with pytest.raises(ValueError):
        f1_ledger.record_rejection(ledger, t_analyzed.qid, x_surplus.qid)
    updated = f1_ledger.record_rejection(ledger, t_analyzed.qid, t_surplus.qid)
    assert f1_ledger.ledger_state_hash(updated) != original_hash
    assert t_analyzed.qid not in {q.qid for q in f1_ledger.active_analyzed_queries(updated)}
    assert t_surplus.qid in {q.qid for q in f1_ledger.active_analyzed_queries(updated)}
    # the denominators restore exactly (192/48/24)
    per_stratum: dict[str, int] = {}
    for q in f1_ledger.active_analyzed_queries(updated):
        per_stratum[q.stratum] = per_stratum.get(q.stratum, 0) + 1
    assert per_stratum == f1_corpus.ANALYZED_COUNTS


# ── dry run over the real corpus ──────────────────────────────────────────────


def test_dry_run_manifest_well_formed(collected: tuple[dict, dict]) -> None:
    manifest, _ = collected
    runner.verify_manifest(manifest)  # raises on any drift
    assert manifest["experiment"] == "f1-task-scope"
    assert manifest["arm_order"] == ["A0", "C", "B", "A"]
    assert set(manifest["arms"]) == {"A0", "C", "B", "A"}
    assert manifest["equal_budget"] == 2048 and manifest["top_k"] == 5
    block = manifest["common_block"]
    assert block["lanes_enabled"] is False  # V2: the E3 verdict stands
    assert block["type_boost"] is True  # §1.3 common block
    assert block["budget"] == 2048 and block["scanner"] is False
    assert manifest["clock"]["run_now"] == runner.RUN_NOW.isoformat()
    assert manifest["corpus"]["seed"] == f1_corpus.SEED
    assert manifest["corpus"]["fingerprint_blake2b"] == f1_corpus.corpus_fingerprint()
    assert manifest["ledger"]["analyzed"] == f1_corpus.ANALYZED_COUNTS
    code = manifest["code"]
    assert code["mnemos_version"] and code["git_commit"] not in ("", None)


def test_dry_run_outcomes_tuples_complete(collected: tuple[dict, dict]) -> None:
    _, outcomes = collected
    runner.verify_outcomes(outcomes)  # schema + statistics ban
    import re as re_mod

    queries = outcomes["queries"]
    assert len(queries) == 264
    qids = [row["qid"] for row in queries]
    assert len(set(qids)) == 264
    for row in queries:
        assert re_mod.match(_QID_RE[row["stratum"]], row["qid"])
        assert set(row["arms"]) == {"A0", "C", "B", "A"}
        for arm in ("A0", "C", "B", "A"):
            arm_tuple = row["arms"][arm]
            assert isinstance(arm_tuple["hit"], bool)
            assert arm_tuple["tokens"] >= 0
            assert len(arm_tuple["block_slugs"]) <= 5
    # G4a probes recorded on arm A task-class tuples only
    for row in queries:
        a_tuple = row["arms"]["A"]
        for arm in ("A0", "C", "B"):
            assert row["arms"][arm]["lens_active"] is None
            assert row["arms"][arm]["pre_lens_gold_top5"] is None
        if row["query_class"] == "task":
            assert a_tuple["pre_lens_gold_top5"] is not None
            assert a_tuple["post_lens_gold_top5"] is not None
        else:
            assert a_tuple["lens_active"] is not None


def test_discordance_tallies_consistent_with_tuples(collected: tuple[dict, dict]) -> None:
    """The persisted McNemar inputs are counts over the raw tuples."""
    _, outcomes = collected
    by_qid = {row["qid"]: row for row in outcomes["queries"]}
    for stratum, comparisons in outcomes["discordance"].items():
        for name, tally in comparisons.items():
            first, second = name.split("_vs_")
            first_only = sum(
                1
                for row in outcomes["queries"]
                if row["stratum"] == stratum
                and row["arms"][first]["hit"]
                and not row["arms"][second]["hit"]
            )
            second_only = sum(
                1
                for row in outcomes["queries"]
                if row["stratum"] == stratum
                and row["arms"][second]["hit"]
                and not row["arms"][first]["hit"]
            )
            assert tally == {f"{first}_only": first_only, f"{second}_only": second_only}
            assert by_qid is not None  # pairing keys unique (checked above)


def test_invariants_v1_to_v6_executed_and_logged(collected: tuple[dict, dict]) -> None:
    manifest, _ = collected
    inv = manifest["invariants"]
    # V1: the fixed probe set — every lens-inactive cross query byte-identical
    assert inv["V1"]["probes"] == 48  # 24 shared/foreign prose + 24 L-neg
    assert inv["V1"]["identical"] == inv["V1"]["probes"]
    assert inv["V1"]["failures"] == []
    # V3 shadow: every lens-active cross query narrows order-preservingly
    assert inv["V3_shadow"]["probes"] == 24  # X-gold code axis
    assert inv["V3_shadow"]["subset_ok"] == 24
    assert inv["V3_shadow"]["code_only_ok"] == 24
    assert inv["V3_shadow"]["failures"] == []
    # V2/V4/V5/V6
    assert inv["V2"] == {"lanes_enabled": False, "arms": ["A0", "C", "B", "A"]}
    assert inv["V4"]["violations"] == 0 and inv["V4"]["canaries_inert"] == 8
    assert inv["V5"]["violations"] == 0 and inv["V5"]["rows_verified"] == 960
    assert inv["V6"]["manifest_verified"] is True
    assert inv["store_copy"]["digests_equal_across_arms"] is True
    assert len(inv["store_copy"]["content_digest_sha256"]) == 64


def test_collect_is_deterministic_run_id_and_outcomes() -> None:
    """Two collects over fresh stores → identical run id + byte-identical
    outcome tuples (§4.2 determinism: seeded ids, frozen clock, clone
    discipline)."""
    first_manifest, first_outcomes = runner.collect_run()
    second_manifest, second_outcomes = runner.collect_run()
    assert first_manifest["run_id"] == second_manifest["run_id"]
    assert json.dumps(first_outcomes, sort_keys=True) == json.dumps(second_outcomes, sort_keys=True)
    # and the outcomes carry no wall-clock anywhere (tuples only)
    assert set(first_outcomes) <= {
        "spec",
        "pairing",
        "strata",
        "arm_order",
        "budget",
        "top_k",
        "queries",
        "discordance",
    }


# ── equal-budget enforcement (§4.1 — breach fails loud) ───────────────────────


def test_equal_budget_breach_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    corpus = f1_corpus.build_corpus()
    query = next(q for q in corpus.analyzed_queries() if q.stratum == "t_gold")
    slug_to_id = {row.slug: f"id-{i:04d}" for i, row in enumerate(corpus.rows)}

    class _StubManager:
        def close(self) -> None:
            return None

    monkeypatch.setattr(runner, "_open_arm_manager", lambda settings: _StubManager())
    monkeypatch.setattr(
        runner,
        "assemble_context",
        lambda *args, **kwargs: {"tokens": {"budget": 4096, "estimated": 1}},
    )
    with pytest.raises(AssertionError, match="equal-budget breach"):
        runner.execute_arm(
            "A0", corpus, (query,), slug_to_id, runner._store_settings(Path("/tmp/f1-unused"))
        )


# ── refuse-to-record default / --record write-once / §9 ledger ────────────────


def test_default_invocation_refuses_to_record(
    collected: tuple[dict, dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest, outcomes = collected
    doc_copy = tmp_path / "f1-task-scope.md"
    doc_copy.write_text(runner.DOC_PATH.read_text())
    monkeypatch.setattr(runner, "collect_run", lambda: (manifest, outcomes))
    rc = runner.main(["--runs-dir", str(tmp_path / "runs"), "--doc-path", str(doc_copy)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "REFUSING to record" in err
    assert not (tmp_path / "runs").exists()  # zero writes
    assert doc_copy.read_text() == runner.DOC_PATH.read_text()  # E-file untouched


def test_record_write_once_and_ledger_append(
    collected: tuple[dict, dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, outcomes = collected
    doc_copy = tmp_path / "f1-task-scope.md"
    doc_copy.write_text(runner.DOC_PATH.read_text())
    runs = tmp_path / "runs"
    monkeypatch.setattr(runner, "collect_run", lambda: (manifest, outcomes))

    rc = runner.main(["--record", "--runs-dir", str(runs), "--doc-path", str(doc_copy)])
    assert rc == 0
    run_dir = runs / str(manifest["run_id"])
    assert (run_dir / "manifest.json").exists()
    assert (run_dir / "outcomes.json").exists()
    doc_text = doc_copy.read_text()
    assert manifest["run_id"] in doc_text  # §9 entry appended
    # exactly ONE ledger entry (the id appears twice inside it: the id
    # itself + the artifacts path — the E0 §9 entry shape)
    assert doc_text.count("— RUN — arms A0/C/B/A") == 1

    # write-once: a second --record of the SAME id fails loud and does
    # not double-append the ledger
    rc = runner.main(["--record", "--runs-dir", str(runs), "--doc-path", str(doc_copy)])
    assert rc == 1
    assert doc_copy.read_text() == doc_text


def test_record_run_refuses_existing_directory(
    collected: tuple[dict, dict], tmp_path: Path
) -> None:
    manifest, outcomes = collected
    run_dir = runner.record_run(manifest, outcomes, tmp_path)
    assert run_dir.exists()
    with pytest.raises(FileExistsError, match="write-once"):
        runner.record_run(manifest, outcomes, tmp_path)


def test_run_ledger_entry_shape(collected: tuple[dict, dict], tmp_path: Path) -> None:
    manifest, _ = collected
    entry = runner.run_ledger_entry(manifest)
    for expected in (
        manifest["run_id"],
        manifest["corpus"]["fingerprint_blake2b"],
        "budget 2048",
        "lanes off",
    ):
        assert expected in entry
    doc_copy = tmp_path / "doc.md"
    doc_copy.write_text(runner.DOC_PATH.read_text())
    runner.append_run_ledger(manifest, doc_copy)
    appended = doc_copy.read_text()
    assert manifest["run_id"] in appended
    with pytest.raises(FileExistsError, match="already present"):
        runner.append_run_ledger(manifest, doc_copy)


# ── the frozen doc is untouched by everything above ───────────────────────────


def test_frozen_doc_sections_untouched_by_the_package() -> None:
    """The package never edits §1-§7: the only doc-touching code is
    append_run_ledger (EOF append into §9), which --record alone fires."""
    text = runner.DOC_PATH.read_text()
    assert "## 8. Amendment log" in text and "## 9. Run ledger" in text
    assert text.rstrip().endswith("*(empty — no run recorded; the window is open)*")
