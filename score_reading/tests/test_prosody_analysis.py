import pytest

from src.models import Alignment, WordAlignment, WordTag
from src.pipeline.analyze import (
    _build_prosody_intonation_analysis,
    _compute_word_prominence_scores,
    generate_expected_stress,
)


def test_compute_word_prominence_scores_prefers_content_words_with_stronger_acoustics():
    alignment = Alignment(
        words=[
            WordAlignment(word='my', start=0.00, end=0.12, tag=WordTag.OK, score=82),
            WordAlignment(word='family', start=0.12, end=0.48, tag=WordTag.OK, score=90),
            WordAlignment(word='is', start=0.48, end=0.58, tag=WordTag.OK, score=84),
            WordAlignment(word='here', start=0.58, end=0.84, tag=WordTag.OK, score=88),
        ],
        phonemes=[],
    )
    generate_expected_stress(alignment)

    scores = _compute_word_prominence_scores(
        alignment,
        pitch_by_index={0: 0.22, 1: 0.95, 2: 0.18, 3: 0.72},
        energy_by_index={0: 0.28, 1: 0.92, 2: 0.25, 3: 0.70},
    )

    assert scores[1] > scores[0]
    assert scores[1] > scores[2]
    assert scores[3] > scores[2]


def test_build_prosody_intonation_analysis_picks_best_and_problem_sentences():
    alignment = Alignment(
        words=[
            WordAlignment(word='My', start=0.00, end=0.10, tag=WordTag.OK, score=82),
            WordAlignment(word='family', start=0.10, end=0.42, tag=WordTag.OK, score=90),
            WordAlignment(word='lives', start=0.42, end=0.68, tag=WordTag.OK, score=88),
            WordAlignment(word='there', start=0.68, end=0.94, tag=WordTag.OK, score=87),
            WordAlignment(word='It', start=1.00, end=1.10, tag=WordTag.OK, score=80),
            WordAlignment(word='is', start=1.10, end=1.18, tag=WordTag.OK, score=80),
            WordAlignment(word='a', start=1.18, end=1.24, tag=WordTag.OK, score=80),
            WordAlignment(word='nice', start=1.24, end=1.36, tag=WordTag.OK, score=80),
            WordAlignment(word='day', start=1.36, end=1.48, tag=WordTag.OK, score=80),
        ],
        phonemes=[],
    )
    generate_expected_stress(alignment)

    analysis = _build_prosody_intonation_analysis(
        alignment,
        script_text='My family lives there. It is a nice day.',
        pitch_by_index={0: 0.20, 1: 0.94, 2: 0.86, 3: 0.88, 4: 0.32, 5: 0.30, 6: 0.28, 7: 0.34, 8: 0.36},
        energy_by_index={0: 0.24, 1: 0.90, 2: 0.82, 3: 0.84, 4: 0.33, 5: 0.30, 6: 0.29, 7: 0.34, 8: 0.35},
    )

    assert analysis is not None
    assert analysis['best_sentence']['sentence'] == 'My family lives there'
    assert analysis['problem_sentences'][0]['sentence'] == 'It is a nice day'
    assert analysis['best_sentence']['stress_accuracy'] > analysis['problem_sentences'][0]['stress_accuracy']
