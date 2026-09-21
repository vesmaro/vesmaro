"""F1 task-scope experiment package (multicontext epic #308, ADR-0027 Phase 1).

Pre-registration: ``docs/experiments/f1-task-scope.md`` (frozen; sections
1-7 are never edited by code). This package ships the corpus generator
(``corpus``), the adjudication ledger (``ledger``), the §6.5 power
arithmetic (``power`` — analysis support, never run-time), and the
E3-canon runner (``runner``: refuse-to-record default, write-once
``--record``, V1-V6 invariants, no statistics at run time).
"""
