"""The checks, in the order they must run, and the order they are shown."""

from __future__ import annotations

from pathlib import Path

from ..data import Data
from .base import CheckResult, SEV_ORDER  # noqa: F401
from .cash import prepare_matching, t1_cash, t3_stripe
from .classification import k1_accounts, k2_history, k3_unusual_inflows
from .completeness import (c1_duplicates_recurring, c2_cutoff_accruals,
                           c3_prepaid_deferred, c4_recurring_entries)
from .ledgers import t2_subledgers, t5_payroll
from .post import finalize
from .stripe_books import stripe_bridge, t4_stripe_books  # noqa: F401
from .variance import v1_pl, v2_balance_sheet, v3_metrics

RUN_ORDER = [("T3", t3_stripe), ("T1", t1_cash), ("T2", t2_subledgers), ("T4", t4_stripe_books), ("T5", t5_payroll),
             ("C2", c2_cutoff_accruals), ("C1", c1_duplicates_recurring), ("C3", c3_prepaid_deferred),
             ("C4", c4_recurring_entries), ("K1", k1_accounts), ("K2", k2_history), ("K3", k3_unusual_inflows),
             ("V1", v1_pl), ("V2", v2_balance_sheet), ("V3", v3_metrics)]
SHOW_ORDER = ["T1", "T2", "T3", "T4", "T5", "C1", "C2", "C3", "C4", "K1", "K2", "K3", "V1", "V2", "V3"]


def run_all(d: Data, root: Path) -> tuple[list[CheckResult], dict, dict]:
    ctx: dict = {}
    prepare_matching(d, ctx)
    results = {}
    for cid, fn in RUN_ORDER:
        results[cid] = fn(d, ctx)
        ctx[cid] = results[cid]
    exc = finalize(d, results, root)
    return [results[c] for c in SHOW_ORDER], exc, ctx
