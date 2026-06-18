-- Per-wallet edge over RESOLVED markets, computed entirely in DuckDB.
--
-- The join chain: market_positions (a wallet's buy in one outcome token)
--   -> market_data  (token -> condition + outcome_index)
--   -> conditions   (resolution + payoutNumerators -> which outcome won).
--
-- Why this beats the data-api: market_positions records a buy whether or not
-- the wallet redeemed, so losers are NOT hidden. The survivorship bias that
-- makes /closed-positions read 90% (truly 48%) does not exist here.
--
-- entry price p = valueBought / quantityBought  (USDC 6dp / shares 6dp -> 0..1)
-- won          = payoutNumerators[outcome_index] != 0
-- z            = (wins - Σp) / sqrt(Σ p(1-p))   -- wins above what odds implied
--
-- :cutoff_ts binds an out-of-sample boundary. Pass 0 to score everything.

WITH bet AS (
    SELECT
        mp.user_id,
        c.resolution_ts,
        LEAST(0.999, GREATEST(0.001,
            mp.val_bought::DOUBLE / mp.qty_bought)) AS p,
        CASE WHEN md.winner THEN 1 ELSE 0 END AS won
    FROM market_positions mp
    JOIN market_data md ON md.token_id = mp.token_id
    JOIN conditions  c  ON c.id = md.condition_id
    WHERE mp.qty_bought > 0
      AND c.resolution_ts > 0
)
SELECT
    b.user_id,
    count(*)                              AS n,
    sum(b.won)                            AS wins,
    round(sum(b.p), 1)                    AS exp_wins,
    round(100.0 * sum(b.won) / count(*), 1) AS win_rate,
    round((sum(b.won) - sum(b.p))
          / sqrt(nullif(sum(b.p * (1 - b.p)), 0)), 2) AS z,
    round(avg(b.p), 3)                    AS avg_entry,
    a.scaled_profit                       AS profit,
    a.scaled_volume                       AS volume,
    a.creation_ts
FROM bet b
LEFT JOIN accounts a ON a.id = b.user_id
WHERE b.resolution_ts <= :cutoff_ts OR :cutoff_ts = 0
GROUP BY b.user_id, a.scaled_profit, a.scaled_volume, a.creation_ts
HAVING count(*) >= :min_n
ORDER BY z DESC;
