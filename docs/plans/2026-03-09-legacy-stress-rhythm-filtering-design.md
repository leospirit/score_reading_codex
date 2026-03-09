# Legacy Stress Rhythm Filtering Design

Keep the legacy report page's `Stress & Rhythm Guide` on the current front-end algorithm, but add three guardrails so it stops surfacing obviously misleading sentences.

1. Best-sentence filter
- Exclude any sentence containing `missing` words from `Natural Rhythm Example`.
- This prevents omission zones such as `I'm [going to] bring...` from being selected as the model sentence.

2. Worst-sentence quality gate
- Only allow `Most Worth Adjusting` to choose sentences that look complete enough for feedback.
- Reject tiny tail fragments and half-sentences, especially segments ending at abrupt function words like `What`.

3. Better sentence split
- Keep pause-based splitting, but prefer punctuation boundaries if present in the reading words stream.
- This reduces unnatural chunking that currently produces incomplete fragments.

Scope:
- Only old HTML report template behavior.
- No score weight changes.
- No completeness logic changes.
- No new backend algorithm.
