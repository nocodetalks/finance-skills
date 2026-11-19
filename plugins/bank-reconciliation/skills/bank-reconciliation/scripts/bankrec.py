#!/usr/bin/env python3
"""bank-reconciliation: explain why QuickBooks and the bank statement disagree, and draft the fixes.

Python 3.10+, standard library only. Read-only: it never writes to QuickBooks. Run `python3 bankrec.py --help`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bankrec.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
