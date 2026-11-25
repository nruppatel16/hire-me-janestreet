# xmatch: cross-market matching for Kalshi and Polymarket

A **read-only** pipeline that finds equivalent markets on Kalshi and Polymarket, checks that they resolve identically, and (later) shows live price gaps. It never places orders and never holds trading credentials.

**Credit.** The architecture follows Yash Kothari's writeup, [Finding free money between Kalshi and Polymarket](https://www.yashkothari.ca/writing/polymarket-kalshi-arbitrage) (June 24, 2026): a funnel of volume gate → embedding retrieval → LLM equivalence classifier → live pricing, run as a slow classification job and a fast pricing job. The idea and the funnel are his. This repo is a learning reimplementation with its own eval suite.

## Status
- Milestone 1 (ingest): done, Gate 1 passed.
- Milestone 2 (embedding retrieval, Gemini): built; Gate 2 is waiting on hand-labeled eval pairs.

See the PRD in the repo root and `docs/`.

## Quick start (local, no Docker)
```bash
cp .env.example .env
./scripts/db.sh init          # project-local Postgres on :5433, data in .pgdata/
uv sync
uv run xmatch migrate
uv run xmatch ingest          # full ingest, both platforms
uv run python scripts/check_gate1.py
uv run xmatch dashboard       # http://127.0.0.1:8050, read-only

# Milestone 2 (needs XMATCH_GEMINI_API_KEY in .env)
uv run xmatch match           # slow job: ingest -> embed new/changed -> retrieve candidate pairs
uv run xmatch label           # hand-label candidate pairs into the eval set
uv run xmatch recall          # recall vs candidate count, to choose the similarity floor
uv run python scripts/check_gate2.py
uv run pytest
```

## Docs
- `docs/api-notes.md`: what was verified in each platform's API docs, with URLs and dates
- `docs/DECISIONS.md`: decision log
