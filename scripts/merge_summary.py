"""Merge daily summary tables without losing rows: ``merge_summary.py OUT IN [IN ...]``.

Rows are keyed by day x platform x mode x polarization; for a day found in several inputs, the
last input wins. Used by the workflow so that a run never drops rows another run committed.
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from s1rfi import daily  # noqa: E402


def main():
    out, *ins = sys.argv[1:]
    tables = [pd.read_csv(f) for f in ins if Path(f).exists() and Path(f).stat().st_size]
    daily.merge_summaries(*tables).to_csv(out, index=False)


if __name__ == "__main__":
    main()
