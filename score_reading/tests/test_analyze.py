"""
分析模块测试
"""
import pytest


class TestExtractWeakWords:
    """测试弱词提取"""
    
    def test_extract_weak_words_from_alignment(self):
        """应该从对齐结果中提取低分词"""
        from src.models import Alignment, WordAlignment, WordTag
        from src.pipeline.analyze import extract_weak_words
        
        alignment = Alignment(
            words=[
                WordAlignment(word="hello", start=0, end=0.5, tag=WordTag.OK, score=90),
                WordAlignment(word="world", start=0.5, end=1.0, tag=WordTag.WEAK, score=50),
                WordAlignment(word="test", start=1.0, end=1.5, tag=WordTag.POOR, score=30),
            ],
            phonemes=[],
        )
        
        weak_words = extract_weak_words(alignment)
        
        assert "test" in weak_words  # 最低分的词
        assert "world" in weak_words
        assert "hello" not in weak_words  # 高分词不应该出现


class TestExtractMissingWords:
    """测试缺失词提取"""
    
    def test_extract_missing_from_alignment(self):
        """应该提取标记为 missing 的词"""
        from src.models import Alignment, WordAlignment, WordTag
        from src.pipeline.analyze import extract_missing_words
        
        alignment = Alignment(
            words=[
                WordAlignment(word="hello", start=0, end=0.5, tag=WordTag.OK, score=90),
                WordAlignment(word="world", start=0, end=0, tag=WordTag.MISSING, score=0),
            ],
            phonemes=[],
        )
        
        missing = extract_missing_words(alignment, "hello world test")
        
        assert "world" in missing or "test" in missing


class TestAssignTags:
    """测试标签分配"""
    
    def test_assign_tags_based_on_score(self):
        """应该根据分数分配正确的标签"""
        from src.models import Alignment, PhonemeAlignment, WordAlignment, WordTag
        from src.pipeline.analyze import assign_tags
        
        alignment = Alignment(
            words=[
                WordAlignment(word="high", start=0, end=0.5, tag=WordTag.OK, score=90),
                WordAlignment(word="mid", start=0.5, end=1.0, tag=WordTag.OK, score=55),
                WordAlignment(word="low", start=1.0, end=1.5, tag=WordTag.OK, score=25),
            ],
            phonemes=[],
        )
        
        assign_tags(alignment)
        
        assert alignment.words[0].tag == WordTag.OK
        assert alignment.words[1].tag == WordTag.WEAK
        assert alignment.words[2].tag == WordTag.POOR


class TestDetectLinking:
    """测试连读检测"""
    
    def test_detect_linking_between_words(self):
        """应该检测到相邻词之间的连读"""
        from src.models import Alignment, WordAlignment
        from src.pipeline.analyze import detect_linking
        
        # 模拟 "pick up"
        # pick: 0.1 - 0.4
        # up:   0.38 - 0.6  (有重叠，典型的连读特征)
        alignment = Alignment(
            words=[
                WordAlignment(word="pick", start=0.1, end=0.4, score=90),
                WordAlignment(word="up", start=0.38, end=0.6, score=90),
                WordAlignment(word="now", start=0.65, end=0.9, score=90), # 间隙 0.05，非连读
            ],
            phonemes=[],
        )
        
        detect_linking(alignment)
        
        # pick 和 up 应该被标记为 has_linking (或类似字段)
        # Note: 我们需要先在 WordAlignment 模型中添加这个字段，或者通过 tag 实现
        # 根据 implementation_plan，我们可能需要扩展模型或者使用 metadata
        assert alignment.words[0].is_linked is True
        assert alignment.words[1].is_linked is False # 它是被连读的那一个，通常标记在前一个词后面

class TestDeriveStableMissingIndices:
    def test_uses_azure_transcript_anchor_even_when_annotation_source_is_gemini(self):
        from src.models import Alignment, WordAlignment, WordTag
        from src.pipeline.analyze import derive_stable_missing_indices

        script_text = "my uncle and aunt will see you at school today"
        alignment = Alignment(
            words=[
                WordAlignment(word="my", start=0.0, end=0.1, tag=WordTag.OK),
                WordAlignment(word="uncle", start=0.1, end=0.2, tag=WordTag.OK),
                WordAlignment(word="and", start=0.2, end=0.3, tag=WordTag.OK),
                WordAlignment(word="aunt", start=0.3, end=0.4, tag=WordTag.OK),
                WordAlignment(word="will", start=0.4, end=0.5, tag=WordTag.OK),
                WordAlignment(word="see", start=0.5, end=0.6, tag=WordTag.OK),
                WordAlignment(word="you", start=0.6, end=0.7, tag=WordTag.OK),
                WordAlignment(word="at", start=0.7, end=0.8, tag=WordTag.OK),
                WordAlignment(word="school", start=0.8, end=0.9, tag=WordTag.OK),
                WordAlignment(word="today", start=0.9, end=1.0, tag=WordTag.OK),
            ],
            phonemes=[],
        )
        engine_raw = {
            "source": "pro_azure_scoring",
            "annotation_source": "gemini",
            "detected_transcript": "my uncle and aunt will see at school today",
            "gemini_missing_indices": [],
        }

        indices, source = derive_stable_missing_indices(alignment, script_text, engine_raw)

        assert indices == [6]
        assert source == "transcript_anchor"

    def test_ignores_gemini_overlay_and_keeps_azure_alignment_missing(self):
        from src.models import Alignment, WordAlignment, WordTag
        from src.pipeline.analyze import derive_stable_missing_indices

        script_text = "my uncle and aunt will see you at school today"
        alignment = Alignment(
            words=[
                WordAlignment(word="my", start=0.0, end=0.1, tag=WordTag.OK),
                WordAlignment(word="uncle", start=0.1, end=0.2, tag=WordTag.OK),
                WordAlignment(word="and", start=0.2, end=0.3, tag=WordTag.OK),
                WordAlignment(word="aunt", start=0.3, end=0.4, tag=WordTag.OK),
                WordAlignment(word="will", start=0.4, end=0.5, tag=WordTag.OK),
                WordAlignment(word="see", start=0.5, end=0.6, tag=WordTag.OK),
                WordAlignment(word="you", start=0.6, end=0.7, tag=WordTag.MISSING),
                WordAlignment(word="at", start=0.7, end=0.8, tag=WordTag.OK),
                WordAlignment(word="school", start=0.8, end=0.9, tag=WordTag.OK),
                WordAlignment(word="today", start=0.9, end=1.0, tag=WordTag.OK),
            ],
            phonemes=[],
        )
        engine_raw = {
            "source": "pro_azure_scoring",
            "annotation_source": "gemini",
            "detected_transcript": "my uncle and aunt will see at school today",
            "gemini_missing_indices": [2],
            "gemini_detected_transcript": "my uncle aunt will see you at school today",
        }

        indices, source = derive_stable_missing_indices(alignment, script_text, engine_raw)

        assert indices == [6]
        assert source == "alignment"


    def test_prefers_phrase_reanchor_for_im_bring_pattern(self):
        from src.models import Alignment, WordAlignment, WordTag
        from src.pipeline.analyze import derive_stable_missing_indices

        script_text = "You too I'm going to bring back some sausages for you"
        alignment = Alignment(
            words=[
                WordAlignment(word="You", start=0.0, end=0.1, tag=WordTag.OK),
                WordAlignment(word="too", start=0.1, end=0.2, tag=WordTag.OK),
                WordAlignment(word="I'm", start=0.2, end=0.3, tag=WordTag.MISSING),
                WordAlignment(word="going", start=0.3, end=0.4, tag=WordTag.OK),
                WordAlignment(word="to", start=0.4, end=0.5, tag=WordTag.OK),
                WordAlignment(word="bring", start=0.5, end=0.6, tag=WordTag.OK),
                WordAlignment(word="back", start=0.6, end=0.7, tag=WordTag.OK),
                WordAlignment(word="some", start=0.7, end=0.8, tag=WordTag.OK),
                WordAlignment(word="sausages", start=0.8, end=0.9, tag=WordTag.OK),
                WordAlignment(word="for", start=0.9, end=1.0, tag=WordTag.OK),
                WordAlignment(word="you", start=1.0, end=1.1, tag=WordTag.OK),
            ],
            phonemes=[],
        )
        engine_raw = {
            "source": "Azure",
            "annotation_source": "gemini",
            "detected_transcript": "You too I'm bring back some sausages for you",
        }

        indices, source = derive_stable_missing_indices(alignment, script_text, engine_raw)

        assert indices == [3, 4]
        assert source in {"transcript_anchor", "alignment_reanchor"}

    def test_preserves_valid_missing_and_reanchors_shifted_phrase(self):
        from src.models import Alignment, WordAlignment, WordTag
        from src.pipeline.analyze import derive_stable_missing_indices

        script_text = "Are you going to ski Yes I can't ski now but I'm going to bring back"
        alignment = Alignment(
            words=[
                WordAlignment(word="Are", start=0.0, end=0.1, tag=WordTag.OK),
                WordAlignment(word="you", start=0.1, end=0.2, tag=WordTag.OK),
                WordAlignment(word="going", start=0.2, end=0.3, tag=WordTag.OK),
                WordAlignment(word="to", start=0.3, end=0.31, tag=WordTag.MISSING),
                WordAlignment(word="ski", start=0.31, end=0.5, tag=WordTag.OK),
                WordAlignment(word="Yes", start=0.5, end=0.6, tag=WordTag.OK),
                WordAlignment(word="I", start=0.6, end=0.7, tag=WordTag.OK),
                WordAlignment(word="can't", start=0.7, end=0.8, tag=WordTag.OK),
                WordAlignment(word="ski", start=0.8, end=0.9, tag=WordTag.OK),
                WordAlignment(word="now", start=0.9, end=1.0, tag=WordTag.OK),
                WordAlignment(word="but", start=1.0, end=1.1, tag=WordTag.OK),
                WordAlignment(word="I'm", start=1.1, end=1.2, tag=WordTag.MISSING),
                WordAlignment(word="going", start=1.2, end=1.3, tag=WordTag.OK),
                WordAlignment(word="to", start=1.3, end=1.4, tag=WordTag.OK),
                WordAlignment(word="bring", start=1.4, end=1.5, tag=WordTag.OK),
                WordAlignment(word="back", start=1.5, end=1.6, tag=WordTag.OK),
            ],
            phonemes=[],
        )
        engine_raw = {
            "source": "Azure",
            "annotation_source": "gemini",
            "detected_transcript": "Are you going ski Yes I can't ski now but I'm bring back",
        }

        indices, source = derive_stable_missing_indices(alignment, script_text, engine_raw)

        assert indices == [3, 12, 13]
        assert source == "alignment_reanchor"

    def test_prefers_phrase_reanchor_for_going_ski_pattern(self):
        from src.models import Alignment, WordAlignment, WordTag
        from src.pipeline.analyze import derive_stable_missing_indices

        script_text = "Are you going to ski"
        alignment = Alignment(
            words=[
                WordAlignment(word="Are", start=0.0, end=0.1, tag=WordTag.OK),
                WordAlignment(word="you", start=0.1, end=0.2, tag=WordTag.OK),
                WordAlignment(word="going", start=0.2, end=0.3, tag=WordTag.OK),
                WordAlignment(word="to", start=0.3, end=0.31, tag=WordTag.MISSING),
                WordAlignment(word="ski", start=0.31, end=0.5, tag=WordTag.OK),
            ],
            phonemes=[],
        )
        engine_raw = {
            "source": "Azure",
            "annotation_source": "gemini",
            "detected_transcript": "Are you going ski",
        }

        indices, source = derive_stable_missing_indices(alignment, script_text, engine_raw)

        assert indices == [3]
        assert source in {"alignment", "transcript_anchor", "alignment_reanchor"}
