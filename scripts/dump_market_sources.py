"""Rank discovered geopolitical markets and dump the source plan each one gets.

Answers two questions at once, from the local discovery store -- no network:

  1. which markets are actually worth sourcing for (ranked, not hand-picked);
  2. what sources each already receives, and which required ones are MISSING.

`missing_required` is the column that matters. A market whose rule names a
settlement source the registry cannot resolve is a market the system can watch
but never authorize from, so those rows are the real source backlog. Ranking is
by volume then liquidity: attention is where stale quotes and crowd reaction
both live, and it is observable without any forward data.

Usage:
  PYTHONPATH=. python3 scripts/dump_market_sources.py           # top 50, table
  PYTHONPATH=. python3 scripts/dump_market_sources.py 50 json   # machine-readable
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from polybot.discovery.config import load_discovery_config
from polybot.discovery.sources import build_source_plan
from polybot.discovery.store import DiscoveryStore

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 50
FORMAT = (sys.argv[2] if len(sys.argv) > 2 else "table").lower()

config = load_discovery_config(Path("configs/geopolitics/discovery.yaml"))
contexts = [c for c in DiscoveryStore(config.data_dir).all_contexts() if not c.closed]
contexts.sort(key=lambda c: (c.volume, c.liquidity), reverse=True)

rows: list[dict[str, object]] = []
for context in contexts:
    if len(rows) >= LIMIT:
        break
    analysis = context.rule_analysis
    if analysis is None:
        # Unanalyzed markets cannot have a plan built; report them as backlog
        # rather than dropping them, since an ungraded market is also a gap.
        rows.append(
            {
                "market_id": context.market_id,
                "question": context.question or context.event_title,
                "deadline": context.deadline_iso,
                "state": context.state,
                "volume": round(context.volume, 2),
                "actors": [],
                "mediators": [],
                "keywords": [],
                "resolution_source": context.resolution_source,
                "discretionary": None,
                "feeds": 0,
                "poll_urls": 0,
                "auto_trade_domains": [],
                "required": [],
                "missing_required": ["<no rule analysis: not graded yet>"],
            }
        )
        continue
    try:
        plan = build_source_plan(context)
    except Exception as exc:  # a plan refusal is itself a finding
        rows.append(
            {
                "market_id": context.market_id,
                "question": context.question or context.event_title,
                "deadline": context.deadline_iso,
                "state": context.state,
                "volume": round(context.volume, 2),
                "actors": analysis.parties,
                "mediators": analysis.mediators,
                "keywords": analysis.keywords[:6],
                "resolution_source": context.resolution_source,
                "discretionary": analysis.discretionary,
                "feeds": 0,
                "poll_urls": 0,
                "auto_trade_domains": [],
                "required": [],
                "missing_required": [f"<plan refused: {exc}>"],
            }
        )
        continue
    rows.append(
        {
            "market_id": context.market_id,
            "question": context.question or context.event_title,
            "deadline": context.deadline_iso,
            "state": context.state,
            "volume": round(context.volume, 2),
            "actors": analysis.parties,
            "mediators": analysis.mediators,
            "keywords": analysis.keywords[:6],
            "resolution_source": context.resolution_source,
            "discretionary": analysis.discretionary,
            "feeds": len(plan.feed_urls),
            "poll_urls": len(plan.poll_urls),
            "auto_trade_domains": plan.auto_trade_domains,
            "required": plan.required_source_refs,
            "missing_required": plan.missing_required_source_refs,
        }
    )

if FORMAT == "json":
    print(json.dumps(rows, indent=2, sort_keys=False))
    raise SystemExit(0)

print(f"{len(rows)} markets (ranked by volume), from {len(contexts)} open contexts\n")
for index, row in enumerate(rows, start=1):
    flag = " DISCRETIONARY" if row["discretionary"] else ""
    print(f"{index:>3}. {str(row['question'])[:96]}")
    print(
        f"     id={row['market_id']}  vol={row['volume']}  "
        f"deadline={row['deadline'][:10]}  state={row['state']}{flag}"
    )
    actors = ", ".join(list(row["actors"]) + list(row["mediators"])) or "-"
    print(f"     actors: {actors}")
    print(
        f"     sources: {row['feeds']} feeds, {row['poll_urls']} direct polls, "
        f"{len(row['auto_trade_domains'])} auto-trade domains"
    )
    if row["resolution_source"]:
        print(f"     rule names: {row['resolution_source']}")
    if row["missing_required"]:
        print(f"     >>> MISSING: {', '.join(str(m) for m in row['missing_required'])}")
    print()

missing = [r for r in rows if r["missing_required"]]
print(f"{len(missing)} of {len(rows)} markets have unresolved required sources.")
print("Those are the source backlog: rules name an authority the registry cannot resolve.")
