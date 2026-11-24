# PRD: Cross-Market Matching Pipeline (AI Engineering + Data Pipeline)

Nov 24, 2026 · @Nrup

## 1. Overview

This project builds a read-only system that finds equivalent markets on Kalshi and Polymarket, confirms they resolve identically, and surfaces live price gaps where buying YES on one and NO on the other costs less than $1. It is a learning project in applied AI engineering and data engineering. It is not a trading product and never places orders.

**Reference.** The design follows Yash Kothari's writeup, [Finding free money between Kalshi and Polymarket](https://www.yashkothari.ca/writing/polymarket-kalshi-arbitrage) (June 24, 2026). His pipeline filters about 2 billion possible market pairs to about 800 confirmed pairs and about 50 genuine arbitrages, using Neon (Postgres), Pinecone and Modal. This PRD credits that design explicitly and does not claim the idea as original.

**What is original here.**

- A hand-labeled eval set built from my own failure cases, with precision and recall reported per pipeline version.
- A resolution-rules verifier that reads full resolution text, not only titles.
- A model comparison (accuracy, cost, latency) across at least three LLMs on the same labeled pairs.
- Verdict caching keyed on a hash of both markets' resolution text, so unchanged pairs are never re-classified.

**Scope of this document.** Phase 1 covers the data pipeline and AI engineering. DevOps (infrastructure as code, CI/CD gating, observability) is deferred to Phase 2 and only marked at the interfaces.

## 2. Goals, non-goals and success metrics

Phase 1 succeeds when the classifier produces zero false positives on a frozen eval set and the pipeline runs unattended for two weeks. The targets below are my proposals, to be revised once the first eval run gives a baseline.

**Goals**

1. Ingest open markets from both platforms on a schedule into one normalized store.
2. Narrow billions of raw pairs to a small set of confirmed-equivalent pairs using a staged funnel.
3. Measure classifier quality with a labeled eval set, and block any prompt or model change that regresses it.
4. Compute live arbitrage margins for confirmed pairs, including fees and order-book depth.
5. Produce a write-up with real failure cases and numbers.

**Non-goals**

- Placing trades or holding funds.
- Beating other arbitrageurs on speed.
- Infrastructure as code, CI/CD, and monitoring stacks (Phase 2).
- Supporting platforms beyond Kalshi and Polymarket.

**Success metrics**

| Metric | Target | How measured |
| --- | --- | --- |
| False positives on eval set | 0 | Eval run on frozen labeled pairs |
| Recall on "must confirm" pairs | 90% or higher | Same eval run |
| Eval set size | 30 pairs at first baseline, 60 or more by Gate 3 | Count of labeled pairs |
| Cost per full classification run | Measured and published | Token logs per run |
| Model comparison | 3 or more models | Same eval set, accuracy, cost, latency |
| Cache hit rate on re-runs | 80% or higher | Verdict cache hits over pairs processed |
| Pricing refresh | Under 2 minutes end to end | Timestamp of oldest quote in live view |
| Unattended run | 14 days without manual fixes | Job logs |

## 3. Users and use cases

The primary user is me, learning how to run an LLM pipeline where wrong answers have a real cost. The secondary user is a reviewer, such as a hiring manager or engineer, who reads the repo and write-up to judge engineering decisions.

| User | Need | Product answer |
| --- | --- | --- |
| Builder | See which market pairs are real arbs, and why a pair was rejected | Dashboard with verdict, rationale and rule clause per pair |
| Builder | Change a prompt without breaking known-good cases | Eval run with per-case pass or fail before any change ships |
| Reviewer | Judge the quality of the AI engineering | Repo with eval set, failure log, model comparison table |
| Reviewer | Check that nothing here can lose money | Read-only design, no trading credentials in the system |

## 4. System overview: the funnel

The system is a funnel that spends little on the many pairs and more on the few. Cheap filters run first, and the LLM only sees what survives.

&#91;embedded content: stage counts from the reference writeup, approximate · log-scale widths\]

The embedding step does the largest cut, about 4,500 pairs in per pair kept. That is why the LLM classifier runs on about 10,000 pairs, not 45 million. Two scheduled jobs sit on top of the funnel: a slow one for classification and a fast one for live pricing.

## 5. Data pipeline requirements

The pipeline must pull about 90,000 open markets (roughly 44,400 on Kalshi and 45,000 on Polymarket, per the reference writeup), normalize them into one schema, and keep that store fresh without duplicate work. Endpoint details and rate limits must be confirmed against each platform's current API docs before build.

| ID | Requirement | Acceptance criterion |
| --- | --- | --- |
| DP-1 | Ingest all open markets from both platforms on a schedule | Full refresh completes and row counts match each platform's own listing within 1% |
| DP-2 | Normalize to one market schema: platform, native id, title, description, full resolution text, close time, outcome labels, volume, status | No null in title, resolution text or close time for rows marked active |
| DP-3 | Idempotent upserts keyed on (platform, native id) | Re-running a job twice yields identical row counts and no duplicates |
| DP-4 | Detect changed markets by hashing title plus resolution text | Only changed or new markets trigger re-embedding and re-classification |
| DP-5 | Handle API failures with retries, backoff and a per-run error log | A forced failure mid-run leaves the store consistent and the run marked failed |
| DP-6 | Keep raw API payloads for each ingest run | Any normalized row can be traced to its raw payload |
| DP-7 | Record run metadata: start, end, rows read, rows written, errors | One row per run, queryable |
| DP-8 | Apply the volume gate (drop markets under $10K cumulative volume) | Gate threshold is a config value, and gated markets are flagged, not deleted |
| DP-9 | Track market lifecycle: open, closed, resolved | Resolved markets leave the live set and are kept for eval history |
| DP-10 | Separate the slow classification cadence from the fast pricing cadence | Two independently scheduled jobs sharing the store |

**Data quality rules.** Resolution text is the field that decides equivalence, so a market without it is held out of matching, not guessed. Close times are stored in UTC with the source timezone noted. Prices are stored as integer cents.

## 6. Matching and AI engineering requirements

Matching has two jobs: find pairs that sound alike (embeddings), then decide whether they resolve identically in every realistic outcome (LLM). The reference design shows why both are needed. Titles differ by phrasing but resolve at the same moment, and near-identical titles can be different bets.

**Stage 2: embedding retrieval**

- Embed each market from title plus a short normalized description, using `text-embedding-3-large` as in the reference.
- For each Kalshi market, retrieve the top-K nearest Polymarket markets by cosine similarity, then the reverse, and keep the union.
- Apply a similarity floor. The threshold is a tuned config value, chosen from the eval set by plotting recall against candidate count, not picked by feel.
- Decision to record: Pinecone (as in the reference) versus Postgres with `pgvector`. At about 90,000 vectors, `pgvector` removes a service. Choose one, and log the reason.

**Stage 3: LLM equivalence classifier**

- Input: both markets' titles, full resolution text, close times and outcome labels. Not titles alone.
- Output must be structured JSON, validated on receipt, with a rejected-parse retry:

```json
{
  "verdict": "equivalent | not_equivalent | uncertain",
  "direction_safe": true,
  "differences": ["deadline", "threshold", "definition", "fallback", "none"],
  "evidence": {"kalshi_clause": "...", "polymarket_clause": "..."},
  "rationale": "one short paragraph"
}
```

- Only `equivalent` with a non-empty evidence pair reaches the dashboard. `uncertain` goes to a human review queue and is treated as rejected for display.
- The classifier must answer one question: would both markets pay the same side in every realistic outcome, including edge cases. It must not rate how similar the markets seem.

**Known failure modes the prompt and eval set must cover**

| Failure mode | Example from the reference | Correct handling |
| --- | --- | --- |
| Different deadlines | Two "will X leave office" markets six months apart | Reject, since a window exists where they diverge |
| Threshold boundary | "Above 3.7%" versus "at or above 3.7%" | Reject, since they split at exactly 3.7% |
| Sibling products | iPhone 18 versus iPhone 18 Pro | Reject, since one can happen without the other |
| Advances versus wins | CA-11 jungle primary: Kalshi "advanced", Polymarket "most votes" | Reject, since second place advances too |
| Same date, different phrasing | "Before Jan 1, 2027" versus "in 2026" | Accept only after checking both resolve at the same calendar moment |

**Requirements**

| ID | Requirement | Acceptance criterion |
| --- | --- | --- |
| AI-1 | Prompts are versioned files, not inline strings | Every verdict row stores the prompt version and model id |
| AI-2 | Model is a config value, so models can be swapped | Same eval set runs against any configured model with no code change |
| AI-3 | Verdicts are cached by hash of both resolution texts plus prompt version | Re-run on unchanged pairs makes zero LLM calls |
| AI-4 | Every LLM call logs tokens, latency and cost | Cost per run is queryable |
| AI-5 | Output schema is validated; invalid output retries once, then marks the pair `uncertain` | No unvalidated verdict can reach the dashboard |
| AI-6 | Low-confidence and `uncertain` pairs enter a review queue | Queue lists pair, rationale and both clauses side by side |
| AI-7 | Verdict direction is checked: YES on one side must map to YES on the other | `direction_safe` false blocks display |

## 7. Eval suite

The eval suite is the core deliverable of Phase 1. A false positive can lose real money for anyone who trusts the dashboard, so it carries zero tolerance. A false negative only shrinks the opportunity set, so it is tracked but tolerated. The reference build skipped evals at first, then found manual review was eating hours; this build starts with them.

**Design**

- A labeled set of market pairs, each tagged `must_reject` or `must_confirm`, with a one-line reason and the clause that decides it.
- Start with 30 pairs written by hand before the first prompt, then grow to 60 or more. The reference suite started at 15 and reached about 60.
- Every false positive found in production becomes a `must_reject` case. Every false negative found in review becomes a `must_confirm` case.
- Cover each failure mode in section 6 with at least three pairs, including the CA-11 style case.
- A frozen holdout of 20 pairs is never used to tune the prompt, and is scored only at release.

**Metrics reported per run**

| Metric | Definition | Gate |
| --- | --- | --- |
| False positives | `must_reject` pairs the model confirmed | Must be 0 |
| Recall | `must_confirm` pairs the model confirmed, as a share of all such pairs | Reported, 90% or higher target |
| Uncertain rate | Share of pairs returned as `uncertain` | Reported |
| Cost per 1,000 pairs | Sum of token cost over the run, scaled | Reported |
| Median latency | Per classification call | Reported |
| Run-to-run flips | Pairs whose verdict changed between two identical runs | Reported, target 0 |

**Workflow**

1. Change the prompt, the model or the retrieval threshold.
2. Run the full eval locally with one command, in under a minute for the labeled set.
3. Compare per-case results against the last accepted run, and list every case that changed.
4. Accept only if false positives stay at 0 and no `must_confirm` case that passed now fails.
5. Record the result, prompt version and model id in the run log.

In Phase 2 this same command becomes a CI gate. In Phase 1 it only needs to be a script with a deterministic exit code.

**Model comparison.** Run the identical labeled set through at least three models, for example a small and a large model from two providers. Publish one table of false positives, recall, uncertain rate, cost per 1,000 pairs and median latency. The point is to show a measured choice, not a default.

## 8. Pricing and arbitrage engine

For each confirmed pair, the engine checks both hedge directions against live order books and reports the margin per dollar staked. A pair is an arbitrage in a direction only if buying both legs costs less than the $1 payout after fees.

For direction 1 (YES on Kalshi, NO on Polymarket), with fees as per-platform config values:

```latex
\text{profit per contract} = 1 - \left( p^{K}_{\text{yes,ask}} + p^{P}_{\text{no,ask}} + f_K + f_P \right)
```

Direction 2 swaps the platforms. Prices use the ask on each leg, because that is what a buyer pays. The reference example: Kalshi YES at 47¢ plus Polymarket NO at 40¢ costs 87¢ against a $1 payout, a 13¢ gross margin per dollar before fees.

| ID | Requirement | Acceptance criterion |
| --- | --- | --- |
| PR-1 | Fetch live bid and ask for every confirmed pair on a short cadence | Quote age under 2 minutes in the live view |
| PR-2 | Evaluate both directions per pair | Each pair has up to two direction rows |
| PR-3 | Subtract per-platform fees from config | Fee values are not hard-coded, and each run records the fee version used |
| PR-4 | Compute maximum size per leg before price moves past top of book, from order-book depth | Size shown with the price level it assumes |
| PR-5 | Report net margin and an expected profit in dollars at that size | Both shown, margin first |
| PR-6 | Materialize open arbs into a `live_arbs_current` view, sorted by net margin | View refreshes after each pricing run |
| PR-7 | Flag stale quotes and closing markets | Quotes older than the freshness limit are excluded, not shown |
| PR-8 | Log every observed arb with first-seen and last-seen times | Arb lifetime is queryable, which shows how fast edges close |

**Fees and fill risk.** Gross margins overstate what is capturable. Fees, thin books, and legs filling at different times all eat the edge. The engine reports what the quotes imply and never claims an executable profit. The dashboard must say so on screen.

## 9. Optional agent layer

The agent layer is optional and ships last, only if the Phase 1 pipeline and eval suite are stable. An agent that cannot beat the plain pipeline on the eval set is cut. No agent in this project may place an order or hold credentials for either platform.

| Agent | What it does | Gate before it ships |
| --- | --- | --- |
| Resolution-rules verifier | Fetches full rules for both markets, reads them clause by clause, returns a verdict with the deciding clause from each side | Beats the single-call classifier on the eval set at the same false-positive count, and cost per pair is reported |
| Failure-to-eval assistant | When a false positive is found, drafts a new labeled eval case and a candidate prompt rule | A human approves every new eval case and every prompt change, and the eval run passes |
| Review-queue summarizer | Summarizes why an `uncertain` pair is uncertain, with both clauses quoted | Cannot change a verdict, only explain it |

**Limits.** Agents may read market data and write verdict proposals. They never write to the live view directly, and a human accepts every prompt change. Each agent run logs its tool calls, tokens and cost.

## 10. Data model

The store is Postgres. Nine tables carry the whole system, and every verdict is traceable to a prompt version, a model and the exact text it saw.

| Table | Key fields | Purpose |
| --- | --- | --- |
| `markets` | platform, native\_id, title, description, resolution\_text, close\_time\_utc, outcome\_labels, volume\_usd, status, content\_hash | Normalized markets from both platforms |
| `raw_payloads` | run\_id, platform, native\_id, payload\_json, fetched\_at | Untouched API responses for tracing |
| `ingest_runs` | run\_id, job, started\_at, ended\_at, rows\_read, rows\_written, error\_count, status | One row per job run |
| `embeddings` | market\_id, model, vector, content\_hash | Vectors, or a pointer to the external store |
| `candidate_pairs` | kalshi\_id, polymarket\_id, similarity, retrieval\_version | Pairs that passed the similarity floor |
| `verdicts` | pair\_id, pair\_hash, prompt\_version, model\_id, verdict, direction\_safe, differences, kalshi\_clause, polymarket\_clause, rationale, tokens\_in, tokens\_out, cost\_usd, latency\_ms | Classifier output and its provenance |
| `eval_cases` | pair\_ref, label, reason, deciding\_clause, failure\_mode, split, added\_on | Labeled eval set with holdout flag |
| `quotes` and `arb_observations` | pair\_id, direction, ask\_yes, ask\_no, depth, fee\_version, net\_margin, quoted\_at, first\_seen, last\_seen | Live pricing and arb lifetimes |

The `live_arbs_current` view joins confirmed `verdicts` to the latest `quotes` and returns open arbs sorted by net margin.

**Keys and constraints**

- `markets` is unique on (platform, native\_id).
- `verdicts` is unique on (pair\_hash, prompt\_version, model\_id), which is the cache key.
- `eval_cases` rows are append-only, and edits create a new row so past eval runs stay reproducible.

## 11. Milestones and exit criteria

Phase 1 is four milestones, each closed by a gate. Gate 3 is the one that matters: no pricing work starts until the classifier shows zero false positives on the eval set.

&#91;embedded content: Phase 1 milestones and exit gates · equal bands, not to scale\]

The build is about 3 weeks. The 14-day unattended run in Gate 4 overlaps with writing the project write-up. Phase 2 (DevOps) starts only after Gate 4, and the agent layer in section 9 ships last, if at all.

## 12. Risks, open questions and constraints

The largest risk is that someone trusts a false positive with real money. The design answers it with a read-only system, zero tolerance on false positives, and an on-screen note that quotes are not executable profit.

| Risk | Likelihood | Mitigation |
| --- | --- | --- |
| Classifier confirms a non-equivalent pair | Medium | Eval gate, evidence clauses required, human reads rules before any trade |
| Eval set too small or biased to easy cases | High early | Add every production miss; keep a frozen holdout; cover each failure mode |
| Platform API changes or rate limits break ingestion | Medium | Raw payload store, run logs, retries with backoff |
| LLM cost grows with market count | Medium | Volume gate, hash-keyed cache, only changed markets re-classified |
| Model output drifts between runs | Medium | Report run-to-run flips; pin model versions |
| Gross margin looks real but fees and thin books remove it | High | Fees in the formula, depth-based sizing, no claim of executable profit |
| Scope grows into DevOps before Phase 1 is solid | Medium | Phase 2 starts only after the Phase 1 exit criteria in section 11 |

**Legal and ethical constraints**

- The system does not trade or hold funds, so it needs no trading account to function.
- Whether I can legally trade on either platform from Canada is a separate question that I must check with each platform's terms and current rules before ever placing a real order. This PRD does not answer it.
- Use only public market data, and respect each platform's API terms and rate limits.
- Credit Yash Kothari's writeup wherever the project is described, including the resume line and the README.

**Open questions**

- Pinecone or `pgvector` for vectors, and which decision criteria count most?
- Which three or more models go in the comparison?
- What are each platform's current fee schedules, and how often do they change?
- Does either platform expose order-book depth through a public endpoint at the cadence I need?
- How should resolved markets be kept for eval history without bloating the store?

## 13. Deferred to Phase 2: DevOps

Phase 2 starts after Phase 1 exits, and gets its own PRD. The Phase 1 design leaves these interfaces ready for it:

| Phase 2 area | Interface left by Phase 1 |
| --- | --- |
| CI gate on prompt and model changes | Eval command with a deterministic exit code and a stored per-case result |
| Infrastructure as code | All config in files and environment variables, with no values hard-coded |
| Scheduling and deployment | Two independent jobs, one slow classification cadence and one fast pricing cadence |
| Observability and cost alerts | Per-run metrics in `ingest_runs` and `verdicts` (tokens, latency, cost, errors) |
| Secrets and environments | Provider keys read only from the environment |
| Agent layer operations | Agent runs logged with tool calls and cost |
