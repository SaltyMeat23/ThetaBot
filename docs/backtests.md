# What the backtests say

Every default in `config.example.yaml` that touches risk was checked against **real option prints**
(Alpaca historical option bars, Feb 2024 to Sep 2026) on a set of small caps and community large
caps, sequentially one put at a time the way the bot trades, with an 80% take-profit. Two fill models
bracket reality: **fair** = the day's volume-weighted print minus 5%, **worst** = the day's low
minus 10%. Anything that only worked under fair fills, or only in one half of the sample, was not
adopted. Numbers are per trade, as a percent of collateral.

## Levers that held up

| Lever | Result | What it means for you |
|---|---|---|
| **Put delta band 0.10–0.20** (was 0.18–0.28) | +1.21% fair / +0.15% worst, 12% assigned, vs +0.67% / −0.27% and 19% at 0.18–0.28 | The one lever robust to both fill models and both halves. Higher delta bands collected more premium and made less money. |
| **IV / realized-vol gate at 1.3** (`min_iv_rv_ratio`) | Pre-registered, 7 tests, Bonferroni: +1.90% fair / +1.76% worst, profit factor 4.0, assignments 13% → 10%, about a third fewer entries | Sell insurance only when it is priced above the recent damage rate. The trades it removes are the loss tail (+0.36% fair, negative under worst fills, 17–19% assigned). Improved every group of names tested (23 names). |
| **Take-profit at 80%** | Beat holding to expiry in every configuration | Keep it. |
| **Skip names more than 20% below their 200-day** | Best single cut in the data (+2.16%/trade for 0–20% below) | Names more than 60% off their highs lost money; 40–60% off was fine. |
| **Calls below cost basis after assignment** (`cc_below_basis_after_days`, opt-in) | Wheel went from +16.7% to +43.6% a year on capital employed; 35 days in shares instead of 100; 1 of 8 names stuck instead of 6 | The basis floor traps capital on volatile names. Opt-in because it realizes share losses. |
| **Quality large caps with a 20% yield floor** | About 1%/month on deployed collateral, nearly fill-insensitive; utilization from ~10% to ~50% | Why `watchlist_tiers` carries a per-ticker `min_annualized_yield: 0.20`. |

## Levers that did not

- **Shock-day / slide-day entries** (selling into a ≥8% down day): −0.22%/trade, 39% assigned. A
  synthetic-premium study had said the opposite; real prints reversed it.
- **RSI floor 40, DTE 3–8, RSI ceiling, expected-move cushion, confirmed-downtrend skip, IV rank vs
  the name's own history**: none robust. The cushion only trimmed drawdown. IV rank failed the same
  pre-registered protocol the IV/RV gate passed.
- **Higher delta bands**: 0.25–0.35 was +0.44% fair and −0.66% worst with 29% assigned.
- **Chart patterns** (regression-channel bounces, indicator crosses): band touches were stopped out
  61–78% of the time; breaks whipsawed (86% lower low and 76% back above the band within 10 days).
  Any positive result lived only in the 2025–26 small-cap bull half.

## How to read your own results

The gap between the fair and worst columns is the cost of execution. On cheap, wide-spread names it
eats 40% of the edge; on liquid names it eats almost none. The bot's journal records the
`iv_rv_ratio` and the fill on every entry, so after thirty or so live trades you can see which
column your account lives in. Everything above is measured on the past and is not a promise about
the future; it is the reason the defaults are what they are.
