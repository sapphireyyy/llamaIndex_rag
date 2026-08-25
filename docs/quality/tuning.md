# Initial policy tuning

The general-v1 corpus establishes conservative defaults: 512-token chunks with 64 overlap,
dense top 20, lexical top 20, reciprocal-rank fusion constant 60, rerank top 12, evidence top
6, 24,000-character context budget, relevance threshold 0.01, 45-second model timeout, two
bounded retries, circuit threshold five, and no fallback unless the active policy permits it.

Tune only through a new policy version. Compare against the same dataset snapshot and require
access control 1.0, refusal correctness 1.0, no citation regression, and no material p95 or
cost regression. Exact-term misses suggest lexical tuning; paraphrase misses suggest embedding
or chunk tuning; unsupported answers require higher evidence thresholds or stricter validation.
