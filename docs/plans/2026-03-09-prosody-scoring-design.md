# Prosody Scoring Design

**Goal**
Build a more scientifically grounded stress and rhythm scoring system that supports both fixed-script reading and future free speaking, while keeping the final feedback intuitive for teachers, students, and parents.

## Principles
- Scientific accuracy is the base layer.
- Easy-to-understand visual feedback is the presentation layer.
- Do not equate visual red markers with absolute pronunciation errors.
- Separate word-level stress from sentence-level rhythm.
- Support two modes: fixed-script and free speaking.

## Current Problems
- Current stress/rhythm display relies heavily on heuristic inference.
- `word.score`, word duration, and expected stress are blended too early.
- Fixed-script and free-speaking needs are not separated.
- Visual output currently implies stricter certainty than the model actually has.

## Proposed Scoring Layers
### 1. Word Stress Score
Purpose: evaluate lexical stress inside multi-syllable content words.

Evidence:
- syllable duration contrast
- syllable energy contrast
- syllable F0 peak or movement
- vowel fullness / reduction

Output:
- `0-100`
- used only when confidence is high enough

### 2. Sentence Rhythm Score
Purpose: evaluate sentence-level prominence distribution.

Evidence:
- content-word prominence vs function-word prominence
- over-stressed function words
- under-prominent content words
- sentence-wide stress-rhythm contrast

Output:
- `0-100`

### 3. Prosodic Contrast Score
Purpose: measure whether the sentence has a clear strong-vs-light pattern.

Evidence:
- mean prominence(content words) - mean prominence(function words)
- within-sentence prominence spread
- flatness penalty when all words are similarly prominent

Output:
- `0-100`

## Core Acoustic Signal: Prominence Score
Per word, compute a `prominence_score`.

Recommended normalized weighted formula:
- `0.35 * duration_norm`
- `0.30 * energy_norm`
- `0.20 * pitch_norm`
- `0.15 * vowel_fullness_norm`

Notes:
- fixed-script mode can add reference-template comparison
- free-speaking mode uses only acoustic + linguistic priors
- existing engine `word.score` should not dominate this calculation
- existing engine scores may still be used as confidence hints only

## Two Operating Modes
### Fixed-Script Mode
Use:
- known text
- word boundaries/alignment
- optional expected prominence template
- lexical stress dictionary where available

Benefits:
- highest accuracy
- strongest diagnosis and sentence comparison

### Free-Speaking Mode
Use:
- acoustic prominence only
- content/function word priors
- sentence structure and discourse cues where available

Benefits:
- generalizable to open speaking
- same display system can still be used

## Presentation Mapping
This is only the display layer.

### Bubble Size
- large bubble: relatively prominent
- small bubble: relatively light

### Bubble Color
- green: natural handling
- red: needs adjustment
- gray: light position

### Sentence Cards
- best sentence: highest `Sentence Rhythm Score` with sufficient confidence
- needs-adjustment sentence: lowest `Sentence Rhythm Score` with sufficient confidence and actionable guidance

## Confidence Rules
Use conservative red marking.

Mark red only when:
- word/syllable boundary confidence is acceptable
- acoustic evidence is consistent
- not caused mainly by missing/poor recognition noise
- contrast evidence agrees with target expectation

Low-confidence cases should avoid hard red labels.

## Feedback Layer
Teachers and students should see:
- one natural sentence
- one most-worth-adjusting sentence
- one short actionable instruction

Suggested interpretation text:
- large bubbles show relatively stronger words
- small bubbles show relatively lighter words
- red means the stress-rhythm handling there can be adjusted

## Rollout Strategy
1. Add prominence feature extraction.
2. Add sentence rhythm score.
3. Map bubbles and sentence cards to the new scores.
4. Add word stress scoring for multi-syllable words.
5. Tune confidence thresholds with real classroom samples.
