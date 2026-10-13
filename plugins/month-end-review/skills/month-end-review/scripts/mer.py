#!/usr/bin/env python3
"""month-end-review: read-only month-end review for QuickBooks + Stripe.

Python 3.10+, standard library only. Run `python3 mer.py --help`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mer.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
