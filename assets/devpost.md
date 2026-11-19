# Storm Chaser: "Cloudy with a chance of margin calls"

DurHack 2026: SGX FX (BidFX) Challenge 1: "Protect your assets from a trading market storm"
Theme: Composition

## Tagline

A weather radar for markets: one 0-100 storm score, tested against real crashes, that warns before the low.

## Inspiration

The brief asked for four things: stay safe, get early warning, predict what's next, spot opportunities. We kept thinking about weather radar, not a stock ticker: a single number that tells you how bad it's about to get, a colour you can read from across the room, and a track record you can check against real storms. So we built the radar, then tested it against three storms that actually happened: SGX's own USDJPY data, the FTX collapse, and the UK's 2022 mini-budget. The four pillars became the four numbered sections of one dashboard.

## What it does

Storm Chaser turns any price feed into a storm score from 0 to 100, with four levels: green, watch, warning, tornado, each with hysteresis so the score doesn't flicker. The score blends five sensors: volatility z-score (40%), price jumps (25%), drawdown speed (25%), bid-ask spread (10%), and forward-curve dislocation (15%). We take 100 x (0.5 x the weighted mean of the sensors + 0.5 x the worst single sensor), so one sensor screaming can't be averaged away.

On SGX's own USDJPY data the engine hit tornado on 29 Jul 2026 at 18:01, with 5.0% still to fall before the 3 Aug low. On the FTX collapse it fired at $19,748, with 21% still to fall. On the UK mini-budget it fired 2.8 hours before the low. All three are replayed in the demo, not simulated after the fact.

The baseline keeps re-learning (720-bar half-life), so a market that settles into a new, calmer normal stops alerting on it: the engine tells squalls (short-lived) from storms (sustained) by how long the alert holds.

Beyond the alert: a carry panel pulls the implied rate differential out of SGX's own forward curve (1W to 1Y) and flags when a carry trade is unwinding. An exposure panel prices VaR, expected shortfall and stressed loss for a portfolio, calm market against right now. A cyclone simulator runs 2,000 GARCH-with-jumps paths to show a 5/50/95 fan of where price could go next. A playbook turns the current level into an illustrative hedge idea and a long-volatility idea, both labelled as illustrative, not advice.

## How we built it

Backend: Python, FastAPI, pandas, numpy. Every feed, SGX replay, live Binance, yfinance, lands on the same `Tick` interface so the scoring engine never knows or cares where the data came from.

Frontend: React + TypeScript on Vite, with lightweight-charts for the price and score panes, and a Web Audio siren that gets louder and faster as the score climbs.

SGX's challenge file arrived as a zip of zips of daily CSVs; we wrote a loader that unpacks it directly and a build script that turns it into bundled parquet files for fast replay.

We tested as we went: 149 pytest tests on the scoring, exposure and simulation maths, a 47-check end-to-end script that replays all three crises and checks the engine's behaviour at every step, and 8 Playwright browser tests covering the UI's edge states.

## Challenges we ran into

Getting a single score to behave required real care. A naive average lets one dangerous sensor get diluted by four calm ones, so we built the half-mean-half-worst blend instead. Hysteresis on downgrades (8 points of margin, held for 5 bars) stops the score bouncing between levels on noise, but needed tuning so it doesn't also mute genuine de-escalation.

SGX's own data has two scheduled data releases a month that spike volatility for a few minutes and then settle: the engine briefly reads these as tornado before the squall/storm distinction kicks in and relabels them. We decided that's honest behaviour, not a bug: a real spike that doesn't last is a squall by design, and we test for it explicitly rather than hiding it.

Market data edge cases turned out to be most of the work: feed drops and reconnects, a stale feed that must never show green, bad, duplicate and out-of-order ticks, fat-finger ticks that need the next tick to confirm or reject them, the warm-up period before the engine has enough history to score anything, and weekend/holiday reopen spreads that shouldn't trip a false alarm. Each one has its own test.

## Accomplishments we're proud of

The engine isn't just plausible, it's checked against history: tornado fired ahead of the low in all three real crises we tested, by 5.0%, 21% and 2.8 hours of lead time respectively. We built one scoring engine that runs unmodified against SGX FX data, live crypto and any yfinance market. And we backed the whole thing with 149 backend tests, a 47-check end-to-end demo across three real crises, and 8 browser tests, so "it works" isn't just a demo-day claim.

## What we learned

A good early-warning score is less about a clever formula and more about disciplined edge-case handling: a system that is occasionally wrong about the level is fine, but one that is confidently wrong, showing green on a dead feed, is dangerous. We also learned that honesty about a model's limits (it's tuned on three datasets, not statistically fitted; it briefly misreads scheduled data releases before relabelling them) is worth building into the product, not hiding in a README footnote.

## What's next

Fit the sensor weights properly across a larger set of historical crises instead of the three we hand-picked. Add more asset classes and a wider SGX forward curve so the carry panel covers more of a real FX book. Let the playbook size a hedge against an actual portfolio rather than offering illustrative ideas. Package alerting beyond Telegram for a real desk workflow.

## Built with

Python, FastAPI, pandas, numpy, React, TypeScript, Vite, lightweight-charts, Web Audio API, pytest, Playwright, SGX FX data, Binance API, yfinance

- the Storm Chaser team
