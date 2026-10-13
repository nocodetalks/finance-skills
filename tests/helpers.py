"""Shared test helpers. Every test builds the Northwind fixture in its own temp folder."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from fixture_northwind import build, d  # noqa: F401  (puts scripts/ on sys.path)

from mer import config  # noqa: E402
from mer.checks import run_all  # noqa: E402
from mer.cli import main  # noqa: E402
from mer.data import load  # noqa: E402
from mer.gate import run_gate  # noqa: E402
from mer.util import Period  # noqa: E402

P = Period("2026-09")


def cli(*args) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = main(list(args))
    return rc, out.getvalue() + err.getvalue()


class Base(unittest.TestCase):
    scenario = None
    with_budget = True

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.month = build(self.root, with_budget=self.with_budget, scenario=type(self).scenario)
        self.inp = self.month / "inputs"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def data(self):
        return load(self.month, P, config.load(self.root))

    def manifest(self):
        return json.loads((self.inp / "manifest.json").read_text())

    def save_manifest(self, m):
        (self.inp / "manifest.json").write_text(json.dumps(m))

    def cfg(self):
        return json.loads((self.root / "review-config.json").read_text())

    def save_cfg(self, c, reconfirm=True):
        (self.root / "review-config.json").write_text(json.dumps(c))
        if reconfirm:
            config.confirm(self.root, "Test")

    def gates(self):
        return [f["gate"] for f in run_gate(self.data())]

    def gate_text(self):
        return " | ".join(f["problem"] for f in run_gate(self.data()))

    def review(self):
        d = self.data()
        fails = run_gate(d)
        self.assertEqual(fails, [], fails)
        results, exc, ctx = run_all(d, self.root)
        return {r.id: r for r in results}, exc, ctx

    def titles(self, exc=None, which="open"):
        if exc is None:
            _, exc, _ = self.review()
        return [(e["check"], e["title"]) for e in exc[which]]

    def assertFlag(self, titles, check, *words):
        hits = [t for c, t in titles if c == check and all(w.lower() in t.lower() for w in words)]
        self.assertTrue(hits, f"{check} did not flag {words}. Flags: {titles}")

    def assertNoFlag(self, titles, *words):
        hits = [t for _, t in titles if all(w.lower() in t.lower() for w in words)]
        self.assertFalse(hits, f"Unexpected flag {words}: {hits}")
