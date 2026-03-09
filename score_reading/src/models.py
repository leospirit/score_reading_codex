from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class EngineMode(str, Enum):
    AUTO = "auto"
    FAST = "fast"
    PRO = "pro"
    WHISPER = "whisper"
    AZURE = "azure"
    GEMINI = "gemini"
    WAV2VEC2 = "wav2vec2"


class WordTag(str, Enum):
    OK = "ok"
    WEAK = "weak"
    MISSING = "missing"
    POOR = "poor"


class PhonemeTag(str, Enum):
    OK = "ok"
    WEAK = "weak"
    POOR = "poor"


@dataclass
class AudioMetrics:
    duration_sec: float
    silence_ratio: float
    rms_db: float
    clipping_ratio: float = 0.0


@dataclass
class PauseInfo:
    type: str
    duration: float = 0.0
    issue: str = ""
    target_min: float = 0.0
    target_max: float = 0.0
    adjust_sec: float = 0.0
    expected_type: str = ""


@dataclass
class WordAlignment:
    word: str
    start: float
    end: float
    tag: WordTag = WordTag.OK
    score: float = 100.0
    pause: PauseInfo | None = None
    stress: float = 0.0
    prominence_score: float = 0.0
    is_linked: bool = False
    expected_stress: float = 0.5
    diagnosis: str = ""
    phonemes: list["PhonemeAlignment"] = field(default_factory=list)


@dataclass
class PhonemeAlignment:
    phoneme: str
    start: float
    end: float
    tag: PhonemeTag = PhonemeTag.OK
    score: float = 100.0
    in_word: str = ""


@dataclass
class Alignment:
    words: list[WordAlignment] = field(default_factory=list)
    phonemes: list[PhonemeAlignment] = field(default_factory=list)


@dataclass
class Scores:
    overall_100: float = 0.0
    pronunciation_100: float = 0.0
    fluency_100: float = 0.0
    intonation_100: float = 0.0
    completeness_100: float = 0.0


@dataclass
class Confusion:
    expected: str
    got: str
    count: int = 1


@dataclass
class PacePoint:
    x: float
    y: int


@dataclass
class PitchPoint:
    t: float
    f0: float


@dataclass
class HesitationStats:
    score_label: str
    desc: str
    fillers: list[dict] = field(default_factory=list)
    examples: list[dict] = field(default_factory=list)
    tips: list[str] = field(default_factory=list)


@dataclass
class CompletenessStats:
    title: str = "Completeness"
    score_label: str = ""
    coverage: int = 0
    missing_stats: dict = field(default_factory=dict)
    insight: str = ""
    tips: list[str] = field(default_factory=list)


@dataclass
class Analysis:
    weak_words: list[str] = field(default_factory=list)
    weak_phonemes: list[str] = field(default_factory=list)
    missing_words: list[str] = field(default_factory=list)
    missing_indices: list[int] = field(default_factory=list)
    confusions: list[Confusion] = field(default_factory=list)
    mistakes: list[dict] = field(default_factory=list)
    pace_chart_data: list[PacePoint] = field(default_factory=list)
    pitch_contour: list[PitchPoint] = field(default_factory=list)
    hesitations: HesitationStats | None = None
    completeness: CompletenessStats | None = None
    intonation_analysis: dict[str, Any] | None = None


@dataclass
class Feedback:
    cn_summary: str = ""
    cn_actions: list[str] = field(default_factory=list)
    practice: list[str] = field(default_factory=list)


@dataclass
class Meta:
    task_id: str = ""
    student_id: str = ""
    student_name: str = ""
    submission_id: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    engine_used: str = ""
    fallback_chain: list[str] = field(default_factory=list)
    processing_time_ms: int = 0
    is_auto_transcribed: bool = False


@dataclass
class ScoringResult:
    meta: Meta = field(default_factory=Meta)
    audio: AudioMetrics | None = None
    script_text: str = ""
    scores: Scores = field(default_factory=Scores)
    engine_raw: dict[str, Any] = field(default_factory=dict)
    alignment: Alignment = field(default_factory=Alignment)
    analysis: Analysis = field(default_factory=Analysis)
    feedback: Feedback = field(default_factory=Feedback)
    advisor_feedback: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "meta": {
                "task_id": self.meta.task_id,
                "student_id": self.meta.student_id,
                "student_name": self.meta.student_name,
                "submission_id": self.meta.submission_id,
                "timestamp": self.meta.timestamp,
                "engine_used": self.meta.engine_used,
                "fallback_chain": self.meta.fallback_chain,
                "processing_time_ms": self.meta.processing_time_ms,
                "is_auto_transcribed": self.meta.is_auto_transcribed,
            },
            "audio": {
                "duration_sec": self.audio.duration_sec if self.audio else 0,
                "silence_ratio": self.audio.silence_ratio if self.audio else 0,
                "rms_db": self.audio.rms_db if self.audio else 0,
                "clipping_ratio": self.audio.clipping_ratio if self.audio else 0,
            } if self.audio else None,
            "script_text": self.script_text,
            "scores": {
                "overall_100": round(self.scores.overall_100, 1),
                "pronunciation_100": round(self.scores.pronunciation_100, 1),
                "fluency_100": round(self.scores.fluency_100, 1),
                "intonation_100": round(self.scores.intonation_100, 1),
                "completeness_100": round(self.scores.completeness_100, 1),
            },
            "engine_raw": self.engine_raw,
            "alignment": {
                "words": [
                    {
                        "word": w.word,
                        "start": round(w.start, 3),
                        "end": round(w.end, 3),
                        "tag": w.tag.value,
                        "score": round(w.score, 1),
                        "pause": {
                            "type": w.pause.type,
                            "duration": w.pause.duration,
                            "issue": w.pause.issue,
                            "target_min": w.pause.target_min,
                            "target_max": w.pause.target_max,
                            "adjust_sec": w.pause.adjust_sec,
                            "expected_type": w.pause.expected_type,
                        } if w.pause else None,
                        "stress": w.stress,
                        "prominence_score": w.prominence_score,
                        "expected_stress": w.expected_stress,
                        "is_linked": w.is_linked,
                        "diagnosis": w.diagnosis,
                    }
                    for w in self.alignment.words
                ],
                "phonemes": [
                    {
                        "phoneme": p.phoneme,
                        "start": round(p.start, 3),
                        "end": round(p.end, 3),
                        "tag": p.tag.value,
                        "score": round(p.score, 1),
                        "in_word": p.in_word,
                    }
                    for p in self.alignment.phonemes
                ],
            },
            "analysis": {
                "weak_words": self.analysis.weak_words,
                "weak_phonemes": self.analysis.weak_phonemes,
                "missing_words": self.analysis.missing_words,
                "missing_indices": self.analysis.missing_indices,
                "confusions": [
                    {"expected": c.expected, "got": c.got, "count": c.count}
                    for c in self.analysis.confusions
                ],
                "mistakes": self.analysis.mistakes,
                "pace_chart_data": [{"x": p.x, "y": p.y} for p in self.analysis.pace_chart_data],
                "pitch_contour": [{"t": p.t, "f": p.f0} for p in self.analysis.pitch_contour],
                "hesitations": {
                    "score_label": self.analysis.hesitations.score_label,
                    "desc": self.analysis.hesitations.desc,
                    "fillers": self.analysis.hesitations.fillers,
                    "examples": self.analysis.hesitations.examples,
                    "tips": self.analysis.hesitations.tips,
                } if self.analysis.hesitations else None,
                "completeness": {
                    "title": self.analysis.completeness.title,
                    "score_label": self.analysis.completeness.score_label,
                    "coverage": self.analysis.completeness.coverage,
                    "missing_stats": self.analysis.completeness.missing_stats,
                    "insight": self.analysis.completeness.insight,
                    "tips": self.analysis.completeness.tips,
                } if self.analysis.completeness else None,
                "intonation_analysis": self.analysis.intonation_analysis,
            },
            "feedback": {
                "cn_summary": self.feedback.cn_summary,
                "cn_actions": self.feedback.cn_actions,
                "practice": self.feedback.practice,
            },
            "advisor_feedback": self.advisor_feedback,
            "error": self.error,
        }
