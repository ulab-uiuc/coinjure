# Hypothesis Set (Discovery)

## H1: Overreaction Fade in Binary Buckets
- hypothesis_id: `H1-overreaction-fade`
- market_id: `517311`
- event_id: `16282`
- direction: `mean_revert`
- trigger: short-horizon price spike above local mean followed by momentum stall
- invalidation: price holds above rolling mean with continued positive momentum
- holding_horizon: 5min to 60min
- risk_note: avoid persistent trend regimes
- why_now: sampled series shows alternating up/down bursts in bounded range

## H2: Breakout Continuation in Regime Shift
- hypothesis_id: `H2-breakout-momentum`
- market_id: `678876`
- event_id: `16183`
- direction: `momentum`
- trigger: positive jump with z-score above 0 and follow-through
- invalidation: immediate reversal below recent mean
- holding_horizon: 5min to 30min
- risk_note: whipsaw risk high in low-liquidity intervals
- why_now: strong directional moves observed in historical sample

## H3: Low-Price Compression Mean Reversion
- hypothesis_id: `H3-low-price-rebound`
- market_id: `516926`
- event_id: `16167`
- direction: `long_yes`
- trigger: deeply oversold z-score with first positive reversal candle
- invalidation: continued decline through stop-loss
- holding_horizon: 5min to 45min
- risk_note: tail risk in near-zero probability markets
- why_now: repeated oscillation around low probability band in sample
