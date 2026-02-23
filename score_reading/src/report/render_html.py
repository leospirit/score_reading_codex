"""
口语评分 CLI 框架 - HTML 报告渲染模块

负责将评分结果渲染为 HTML 报告。
支持音频波形嵌入和交互播放。
"""
import base64
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from src.config import config
from src.models import ScoringResult
from src.pipeline.phoneme_fallback import ensure_dense_phoneme_alignment

logger = logging.getLogger(__name__)

# 模板目录
TEMPLATES_DIR = Path(__file__).parent / "templates"

# 发音指导规则
PHONEME_TIPS = {
    "θ": {
        "name": "无声齿擦音",
        "examples": ["three", "think", "thank"],
        "advice": "舌尖轻触上齿，气流从舌尖与上齿间隙中通过。可以用镜子检查舌尖是否可见。"
    },
    "ð": {
        "name": "有声齿擦音",
        "examples": ["the", "this", "that"],
        "advice": "与 /θ/ 相似，但需要振动声带。舌尖轻触上齿，同时发出'嗡嗡'声。"
    },
    "r": {
        "name": "卷舌音",
        "examples": ["red", "run", "right"],
        "advice": "舌尖向后卷曲，不要接触口腔任何部位。嘴唇略微圆起。"
    },
    "l": {
        "name": "舌边音",
        "examples": ["like", "love", "light"],
        "advice": "舌尖顶住上齿龈，气流从舌头两侧通过。结尾的 /l/ 需要把舌尖顶住，不要省略。"
    },
    "v": {
        "name": "唇齿擦音",
        "examples": ["very", "have", "love"],
        "advice": "上齿轻轻咬住下唇，振动声带。注意不要发成 /w/。"
    },
    "w": {
        "name": "圆唇半元音",
        "examples": ["we", "what", "water"],
        "advice": "嘴唇收圆，像吹口哨的嘴形，然后快速过渡到后面的元音。"
    },
    "ŋ": {
        "name": "后鼻音",
        "examples": ["sing", "thing", "king"],
        "advice": "舌根抬起接触软腭，气流从鼻腔通过。不要在结尾加 /g/ 的音。"
    },
    "æ": {
        "name": "开前元音",
        "examples": ["cat", "bad", "apple"],
        "advice": "嘴巴张大，舌头放平并尽量往前，嘴角略微拉开。比中文的'啊'嘴巴张得更大。"
    },
}


def encode_audio_base64(audio_path: Path) -> str | None:
    """
    将音频文件编码为 base64
    
    Args:
        audio_path: 音频文件路径
        
    Returns:
        base64 编码字符串，或 None（如果文件不存在）
    """
    if not audio_path or not audio_path.exists():
        return None
    
    try:
        with open(audio_path, "rb") as f:
            audio_data = f.read()
        return base64.b64encode(audio_data).decode("utf-8")
    except Exception as e:
        logger.warning(f"无法编码音频文件: {e}")
        return None


def _find_uploaded_audio_for_submission(submission_id: str, json_path: Path) -> Path | None:
    """
    Locate uploaded audio by submission id under data/uploads.

    This is used by regenerate_report_from_json so regenerated HTML can still
    include playback audio even when JSON does not embed audio_base64.
    """
    sid = str(submission_id or "").strip()
    if not sid:
        return None

    data_root = None
    for parent in json_path.parents:
        if parent.name == "data":
            data_root = parent
            break
    if data_root is None:
        return None

    uploads_dir = data_root / "uploads"
    if not uploads_dir.exists():
        return None

    allowed_ext = {".mp3", ".wav", ".m4a", ".ogg", ".webm"}
    candidates = [p for p in uploads_dir.rglob(f"{sid}.*") if p.suffix.lower() in allowed_ext]
    if not candidates:
        return None

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def get_phoneme_tips(weak_phonemes: list[str]) -> list[dict[str, Any]]:
    """
    根据弱音素列表获取发音指导
    
    Args:
        weak_phonemes: 弱音素列表
        
    Returns:
        发音指导列表
    """
    tips = []
    seen = set()
    
    for phoneme in weak_phonemes:
        # 清理音素符号
        clean_phoneme = phoneme.strip("/[]").lower()
        
        # 查找匹配的指导
        for key, tip in PHONEME_TIPS.items():
            if key.lower() in clean_phoneme or clean_phoneme in key.lower():
                if key not in seen:
                    tips.append({
                        "phoneme": key,
                        "name": tip["name"],
                        "examples": tip["examples"],
                        "advice": tip["advice"],
                    })
                    seen.add(key)
    
    return tips


def generate_pronunciation_analysis(
    weak_words: list[str],
    weak_phonemes: list[str],
    phoneme_alignments: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    生成发音问题分析数据
    
    当有详细的音素对齐数据时，基于该数据生成分析；
    否则基于 weak_phonemes 和 weak_words 生成分析。
    
    Args:
        weak_words: 弱词列表
        weak_phonemes: 弱音素列表
        phoneme_alignments: 音素对齐数据（包含 in_word 字段）
        
    Returns:
        发音问题分析列表，用于模板渲染
    """
    analysis = []
    
    # 扩展的音素信息映射（包括大小写变体）
    phoneme_tips_extended = {
        # 原始键
        **PHONEME_TIPS,
        # 小写变体
        "æ": PHONEME_TIPS.get("æ", {"name": "开前元音 æ", "advice": "嘴巴张大，舌头放平并往前。"}),
        "ɛ": {"name": "中前元音 ɛ", "advice": "嘴巴半开，舌头中位靠前。类似中文'诶'但更放松。"},
        "ɪ": {"name": "短元音 ɪ", "advice": "嘴巴微开，舌头高位靠前。类似中文'衣'但更短促。"},
        "ɔ": {"name": "中后元音 ɔ", "advice": "嘴巴半开，舌头中位靠后。类似中文'哦'但嘴型更圆。"},
        # 大写变体（用于匹配 Whisper 输出）
        "Æ": {"name": "开前元音 æ", "advice": "嘴巴张大，舌头放平并往前。"},
        "Ɛ": {"name": "中前元音 ɛ", "advice": "嘴巴半开，舌头中位靠前。类似中文'诶'但更放松。"},
    }
    
    # 1. 优先使用详细的音素对齐数据
    if phoneme_alignments:
        # 按音素分组
        phoneme_groups: dict[str, list[str]] = {}
        for pa in phoneme_alignments:
            phoneme = pa.get("phoneme", "")
            in_word = pa.get("in_word", "")
            if phoneme and in_word:
                if phoneme not in phoneme_groups:
                    phoneme_groups[phoneme] = []
                if in_word not in phoneme_groups[phoneme]:
                    phoneme_groups[phoneme].append(in_word)
        
        # 为每个音素生成分析
        for phoneme, words in list(phoneme_groups.items())[:3]:
            phoneme_info = phoneme_tips_extended.get(phoneme) or phoneme_tips_extended.get(phoneme.lower())
            if phoneme_info:
                analysis.append({
                    "target": phoneme,
                    "name": phoneme_info.get("name", f"音素 {phoneme}"),
                    "mistakes": [{
                        "actual": "发音需改进",
                        "desc": phoneme_info.get("advice", "注意发音位置和气流控制。"),
                        "words": [{"text": w, "ipa": f"/{phoneme}/"} for w in words[:4]],
                    }],
                })
    
    # 2. 如果没有详细数据，使用 weak_phonemes 列表
    if not analysis and weak_phonemes:
        for phoneme in weak_phonemes[:3]:
            # 尝试匹配音素（大小写不敏感）
            phoneme_info = phoneme_tips_extended.get(phoneme) or phoneme_tips_extended.get(phoneme.lower())
            if phoneme_info:
                # 找出可能相关的弱词
                related_words = []
                for word in weak_words:
                    related_words.append({"text": word, "ipa": f"/{phoneme}/"})
                    if len(related_words) >= 4:
                        break
                
                analysis.append({
                    "target": phoneme,
                    "name": phoneme_info.get("name", f"音素 {phoneme}"),
                    "mistakes": [{
                        "actual": "发音需练习",
                        "desc": phoneme_info.get("advice", "注意发音位置和气流控制。"),
                        "words": related_words if related_words else [{"text": "(无示例词)", "ipa": ""}],
                    }],
                })
    
    # 3. 最后回退：如果没有音素分析但有弱词
    if not analysis and weak_words:
        # 尝试从弱词中提取一些常见的音素挑战（简单规则）
        challenges = []
        for word in weak_words:
            w_lower = word.lower()
            if 'th' in w_lower: challenges.append("θ/ð")
            if 'v' in w_lower: challenges.append("v")
            if 'l' in w_lower: challenges.append("l")
            if 'r' in w_lower: challenges.append("r")
            if 'ng' in w_lower: challenges.append("ŋ")
        
        # 提取独特的挑战
        unique_challenges = list(dict.fromkeys(challenges))[:2]
        challenge_desc = f"重点关注音素: {', '.join(unique_challenges)}" if unique_challenges else "整体发音清晰度"

        analysis.append({
            "target": "📖",
            "name": "重点词汇练习",
            "is_fallback": True,
            "mistakes": [{
                "actual": challenge_desc,
                "desc": "以下单词的发音得分较低，建议反复跟读，特别注意元音的饱满度和辅音的清晰度。",
                "words": [{"text": w, "ipa": ""} for w in weak_words[:5]],
            }],
        })
    
    return analysis


def _sanitize_advisor_feedback(advisor_feedback: Any) -> dict[str, Any] | None:
    """Normalize advisor payload so pronunciation cards never render as empty shells."""
    if not isinstance(advisor_feedback, dict):
        return None

    cleaned = dict(advisor_feedback)
    raw_top_errors = cleaned.get("top_errors")
    valid_top_errors: list[dict[str, Any]] = []

    if isinstance(raw_top_errors, list):
        for item in raw_top_errors:
            if not isinstance(item, dict):
                continue

            phoneme = str(item.get("phoneme", "") or "").strip()
            description = str(item.get("description", "") or "").strip()
            improvement = str(item.get("improvement", "") or "").strip()

            words_raw = item.get("words")
            words: list[str] = []
            if isinstance(words_raw, list):
                for w in words_raw:
                    if isinstance(w, dict):
                        txt = str(w.get("text", "") or "").strip()
                    else:
                        txt = str(w or "").strip()
                    if txt:
                        words.append(txt)

            # Drop truly empty rows that would only show headings.
            if not (phoneme or description or improvement or words):
                continue

            if not phoneme:
                phoneme = "Key Sound"
            if not description:
                description = "Detected pronunciation instability on key words."
            if not improvement:
                improvement = "Slow down slightly and keep stressed vowels full; finish word endings clearly."

            valid_top_errors.append(
                {
                    "phoneme": phoneme,
                    "description": description,
                    "improvement": improvement,
                    "words": words[:6],
                }
            )

    if valid_top_errors:
        cleaned["top_errors"] = valid_top_errors[:3]
    else:
        cleaned["top_errors"] = []

    return cleaned


def _normalize_token(text: str) -> str:
    return re.sub(r"[^A-Za-z']+", "", str(text or "").strip().lower())


_ARPABET_TO_IPA = {
    "AA": "ɑ",
    "AE": "æ",
    "AH": "ʌ",
    "AO": "ɔ",
    "AW": "aʊ",
    "AY": "aɪ",
    "B": "b",
    "CH": "tʃ",
    "D": "d",
    "DH": "ð",
    "EH": "e",
    "ER": "ɝ",
    "EY": "eɪ",
    "F": "f",
    "G": "ɡ",
    "HH": "h",
    "IH": "ɪ",
    "IY": "i",
    "JH": "dʒ",
    "K": "k",
    "L": "l",
    "M": "m",
    "N": "n",
    "NG": "ŋ",
    "OW": "oʊ",
    "OY": "ɔɪ",
    "P": "p",
    "R": "ɹ",
    "S": "s",
    "SH": "ʃ",
    "T": "t",
    "TH": "θ",
    "UH": "ʊ",
    "UW": "u",
    "V": "v",
    "W": "w",
    "Y": "j",
    "Z": "z",
    "ZH": "ʒ",
}

_PSEUDO_TO_IPA = {
    "th": "θ",
    "sh": "ʃ",
    "ch": "tʃ",
    "ph": "f",
    "ng": "ŋ",
    "ee": "iː",
    "oo": "uː",
    "ea": "iː",
    "ai": "eɪ",
    "ay": "eɪ",
    "oa": "oʊ",
    "ow": "oʊ",
    "ou": "aʊ",
    "oi": "ɔɪ",
    "oy": "ɔɪ",
    "er": "ɚ",
    "ir": "ɚ",
    "ur": "ɚ",
    "ar": "ɑr",
    "or": "ɔr",
    "qu": "kw",
}

_LETTER_TO_IPA = {
    "a": "æ",
    "b": "b",
    "c": "k",
    "d": "d",
    "e": "e",
    "f": "f",
    "g": "ɡ",
    "h": "h",
    "i": "ɪ",
    "j": "dʒ",
    "k": "k",
    "l": "l",
    "m": "m",
    "n": "n",
    "o": "o",
    "p": "p",
    "q": "k",
    "r": "ɹ",
    "s": "s",
    "t": "t",
    "u": "ʌ",
    "v": "v",
    "w": "w",
    "x": "ks",
    "y": "j",
    "z": "z",
}


def _to_ipa_symbol(symbol: str) -> str:
    raw = str(symbol or "").strip()
    if not raw:
        return ""

    core = raw.strip("/[] ").strip()
    if not core:
        return ""

    # Keep existing IPA as-is.
    if re.search(r"[ɑæʌɔəɚɝɜɪʊiueθðʃʒŋɹɾː]", core):
        return core

    arpabet = re.sub(r"\d+$", "", core.upper())
    if arpabet in _ARPABET_TO_IPA:
        return _ARPABET_TO_IPA[arpabet]

    lower = core.lower()
    if lower in _PSEUDO_TO_IPA:
        return _PSEUDO_TO_IPA[lower]

    if len(lower) == 1 and lower in _LETTER_TO_IPA:
        return _LETTER_TO_IPA[lower]

    return core


def _build_word_phoneme_breakdown(result: ScoringResult) -> list[list[dict[str, Any]]]:
    """
    Build per-word phoneme score breakdown for tooltip rendering.
    """
    words = list(result.alignment.words or [])
    phonemes = list(result.alignment.phonemes or [])
    buckets: list[list[dict[str, Any]]] = [[] for _ in words]
    if not words:
        return buckets

    # Fallback: some engines attach phonemes per word but not to alignment.phonemes.
    if not phonemes:
        for wi, w in enumerate(words):
            row = []
            for p in list(getattr(w, "phonemes", []) or []):
                symbol = _to_ipa_symbol(str(getattr(p, "phoneme", "") or "").strip())
                if not symbol:
                    continue
                row.append(
                    {
                        "symbol": symbol,
                        "score": float(getattr(p, "score", 0.0) or 0.0),
                        "start": float(getattr(p, "start", 0.0) or 0.0),
                    }
                )
            row.sort(key=lambda x: float(x.get("start", 0.0)))
            for item in row:
                item.pop("start", None)
            buckets[wi] = row
        return buckets

    # Pass 1: prioritize lexical in_word mapping from engine output.
    assigned: list[bool] = [False] * len(phonemes)
    token_slots: dict[str, list[int]] = defaultdict(list)
    for wi, w in enumerate(words):
        token = _normalize_token(getattr(w, "word", ""))
        if token:
            token_slots[token].append(wi)

    token_cursor: dict[str, int] = defaultdict(int)
    for pi, p in enumerate(phonemes):
        p_symbol = _to_ipa_symbol(str(getattr(p, "phoneme", "") or "").strip())
        if not p_symbol:
            continue

        token = _normalize_token(getattr(p, "in_word", ""))
        slots = token_slots.get(token) or []
        if not slots:
            continue

        try:
            p_start = float(getattr(p, "start", 0.0) or 0.0)
            p_end = float(getattr(p, "end", -1.0) or -1.0)
        except Exception:
            p_start = 0.0
            p_end = -1.0

        chosen_idx = None
        best_overlap = 0.0
        if p_end > p_start >= 0.0:
            for wi in slots:
                try:
                    ws = float(getattr(words[wi], "start", 0.0))
                    we = float(getattr(words[wi], "end", 0.0))
                except Exception:
                    continue
                overlap = min(we, p_end) - max(ws, p_start)
                if overlap > best_overlap:
                    best_overlap = overlap
                    chosen_idx = wi

        if chosen_idx is None:
            cursor = token_cursor[token]
            slot_pos = min(cursor, len(slots) - 1)
            chosen_idx = slots[slot_pos]
            token_cursor[token] = cursor + 1
        else:
            # Keep occurrence mapping monotonic for repeated tokens.
            try:
                slot_pos = slots.index(chosen_idx)
                token_cursor[token] = max(token_cursor[token], slot_pos + 1)
            except ValueError:
                pass

        buckets[chosen_idx].append(
            {
                "symbol": p_symbol,
                "score": float(getattr(p, "score", 0.0) or 0.0),
                "start": max(0.0, p_start),
            }
        )
        assigned[pi] = True

    # Pass 2: assign remaining phonemes by time overlap when timestamps are available.
    for pi, p in enumerate(phonemes):
        if assigned[pi]:
            continue

        p_symbol = _to_ipa_symbol(str(getattr(p, "phoneme", "") or "").strip())
        if not p_symbol:
            continue

        try:
            p_start = float(getattr(p, "start", -1.0))
            p_end = float(getattr(p, "end", -1.0))
        except Exception:
            p_start = -1.0
            p_end = -1.0

        if p_start < 0 or p_end <= p_start:
            continue

        best_idx = None
        best_overlap = 0.0
        for wi, w in enumerate(words):
            try:
                ws = float(getattr(w, "start", 0.0))
                we = float(getattr(w, "end", 0.0))
            except Exception:
                continue
            overlap = min(we, p_end) - max(ws, p_start)
            if overlap > best_overlap:
                best_overlap = overlap
                best_idx = wi

        if best_idx is None or best_overlap <= 0.003:
            continue

        buckets[best_idx].append(
            {
                "symbol": p_symbol,
                "score": float(getattr(p, "score", 0.0) or 0.0),
                "start": p_start,
            }
        )
        assigned[pi] = True

    # Stable ordering inside each word.
    for row in buckets:
        row.sort(key=lambda x: float(x.get("start", 0.0)))
        for item in row:
            item.pop("start", None)

    return buckets


def render_html_report(
    result: ScoringResult,
    output_path: Path,
    audio_path: Path | None = None,
) -> None:
    """
    渲染 HTML 报告
    
    Args:
        result: 评分结果
        output_path: 输出文件路径
        audio_path: 音频文件路径（用于嵌入播放）
    """
    logger.info(f"开始渲染 HTML 报告: {output_path}")
    
    # 加载模板
    # Safety net for legacy/sparse engine outputs: keep per-word phoneme detail dense.
    try:
        ensure_dense_phoneme_alignment(result.alignment)
    except Exception as e:
        logger.warning(f"ensure_dense_phoneme_alignment skipped in report rendering: {e}")

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )
    template = env.get_template("report.html.j2")
    
    # 获取颜色配置
    colors = {
        "ok": config.get("report.colors.ok", "#4CAF50"),
        "weak": config.get("report.colors.weak", "#FFC107"),
        "missing": config.get("report.colors.missing", "#F44336"),
        "poor": config.get("report.colors.poor", "#E91E63"),
    }
    
    # 准备模板数据
    # 将 WordAlignment 转换为模板可用的字典格式（包含时间戳和高级属性）
    phoneme_breakdown = _build_word_phoneme_breakdown(result)

    alignment_words = []
    for idx, word in enumerate(result.alignment.words):
        word_data = {
            "word": word.word,
            "score": word.score,
            "tag": word.tag.value,
            "start": word.start,
            "end": word.end,
            "stress": word.stress,
            "expected_stress": word.expected_stress,
            "is_linked": word.is_linked,
            "diagnosis": getattr(word, "diagnosis", "") or "",
            "phoneme_breakdown": phoneme_breakdown[idx] if idx < len(phoneme_breakdown) else [],
        }
        # 如果存在 Pause 信息，转换为字典
        if word.pause:
            word_data["pause"] = {
                "type": word.pause.type,
                "duration": word.pause.duration,
                "issue": getattr(word.pause, "issue", "") or "",
                "target_min": float(getattr(word.pause, "target_min", 0.0) or 0.0),
                "target_max": float(getattr(word.pause, "target_max", 0.0) or 0.0),
                "adjust_sec": float(getattr(word.pause, "adjust_sec", 0.0) or 0.0),
                "expected_type": getattr(word.pause, "expected_type", "") or "",
            }
        alignment_words.append(word_data)
    
    # 编码音频为 base64
    audio_base64 = encode_audio_base64(audio_path) if audio_path else None
    
    # 获取发音指导
    phoneme_tips = get_phoneme_tips(result.analysis.weak_phonemes or [])
    
    # 提取 Hesitations 数据
    hesitations_data = None
    if result.analysis.hesitations:
        hesitations_data = {
            "score_label": result.analysis.hesitations.score_label,
            "desc": result.analysis.hesitations.desc,
            "fillers": result.analysis.hesitations.fillers,
            "examples": result.analysis.hesitations.examples,
            "tips": result.analysis.hesitations.tips
        }
        
    # 提取 Completeness 数据
    completeness_data = None
    if result.analysis.completeness:
        completeness_data = {
            "title": result.analysis.completeness.title,
            "score_label": result.analysis.completeness.score_label,
            "coverage": result.analysis.completeness.coverage,
            "missing_stats": result.analysis.completeness.missing_stats,
            "insight": result.analysis.completeness.insight,
            "tips": result.analysis.completeness.tips
        }

    # 提取 Pace Chart 数据
    pace_chart_data = [{"x": p.x, "y": p.y} for p in result.analysis.pace_chart_data]

    template_data = {
        "meta": {
            "task_id": result.meta.task_id,
            "student_id": result.meta.student_id,
            "student_name": result.meta.student_name,
            "submission_id": result.meta.submission_id,
            "timestamp": result.meta.timestamp,
            "engine_used": result.meta.engine_used,
        },
        "scores": {
            "overall_100": result.scores.overall_100,
            "pronunciation_100": result.scores.pronunciation_100,
            "fluency_100": result.scores.fluency_100,
            "intonation_100": result.scores.intonation_100,
            "completeness_100": result.scores.completeness_100,
        },
        "alignment": {
            "words": alignment_words,
        },
        "analysis": {
            "weak_words": result.analysis.weak_words,
            "weak_phonemes": result.analysis.weak_phonemes,
            "missing_words": result.analysis.missing_words,
            "mistakes": result.analysis.mistakes,
        },
        "pronunciation_analysis": generate_pronunciation_analysis(
            result.analysis.weak_words,
            result.analysis.weak_phonemes,
            [p.to_dict() if hasattr(p, "to_dict") else vars(p) for p in result.alignment.phonemes]
        ),
        "hesitations": hesitations_data,
        "completeness_analysis": completeness_data,
        "pace_chart_data": pace_chart_data,
        "pitch_contour": [{"t": p.t, "f": p.f0} for p in result.analysis.pitch_contour],
        "feedback": {
            "cn_summary": result.feedback.cn_summary,
            "cn_actions": result.feedback.cn_actions,
            "practice": result.feedback.practice,
        },
        "feedback": {
            "cn_summary": result.feedback.cn_summary,
            "cn_actions": result.feedback.cn_actions,
            "practice": result.feedback.practice,
        },
        "advisor_feedback": _sanitize_advisor_feedback(result.advisor_feedback),
        "colors": colors,
        "audio_base64": audio_base64,
        "phoneme_tips": phoneme_tips,
        "pronunciation_analysis": generate_pronunciation_analysis(
            result.analysis.weak_words or [],
            result.analysis.weak_phonemes or [],
            [{"phoneme": p.phoneme, "in_word": p.in_word, "score": p.score} 
             for p in result.alignment.phonemes] if result.alignment.phonemes else None,
        ),
        "engine_raw": result.engine_raw,
        # 优先使用音频文件名，如果没有则使用输出文件名，最后回退到 unknown
        "audio_stem": audio_path.stem if audio_path else (output_path.stem if output_path else "Unknown"),
    }
    
    # 渲染 HTML
    html_content = template.render(**template_data)
    
    # 确保输出目录存在
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # 写入文件
    output_path.write_text(html_content, encoding="utf-8")
    
    logger.info(f"HTML 报告已生成: {output_path}")


def regenerate_report_from_json(json_path: Path, output_path: Path) -> None:
    """
    从 JSON 文件重新生成 HTML 报告
    
    Args:
        json_path: JSON 结果文件路径
        output_path: 输出 HTML 文件路径
    """
    import json
    
    if not json_path.exists():
        raise FileNotFoundError(f"JSON 文件不存在: {json_path}")

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    
    # 加载 JSON
    
    # Backward compatibility for old JSON:
    # 1) top up sparse phoneme alignment
    # 2) inject per-word phoneme_breakdown used by Reading Analysis tooltip
    try:
        alignment_data = data.get("alignment", {}) if isinstance(data, dict) else {}
        words_data = alignment_data.get("words", []) if isinstance(alignment_data, dict) else []
        phonemes_data = alignment_data.get("phonemes", []) if isinstance(alignment_data, dict) else []
        if isinstance(words_data, list) and words_data:
            from src.models import Alignment, PhonemeAlignment, PhonemeTag, WordAlignment, WordTag

            def _to_word_tag(raw: Any) -> WordTag:
                tag = str(raw or "ok").strip().lower()
                if tag == "weak":
                    return WordTag.WEAK
                if tag == "poor":
                    return WordTag.POOR
                if tag == "missing":
                    return WordTag.MISSING
                return WordTag.OK

            def _to_phoneme_tag(raw: Any) -> PhonemeTag:
                tag = str(raw or "ok").strip().lower()
                if tag == "poor":
                    return PhonemeTag.POOR
                if tag == "weak":
                    return PhonemeTag.WEAK
                return PhonemeTag.OK

            words = []
            for row in words_data:
                if not isinstance(row, dict):
                    continue
                words.append(
                    WordAlignment(
                        word=str(row.get("word", "") or ""),
                        start=float(row.get("start", 0.0) or 0.0),
                        end=float(row.get("end", 0.0) or 0.0),
                        score=float(row.get("score", 0.0) or 0.0),
                        tag=_to_word_tag(row.get("tag")),
                        diagnosis=str(row.get("diagnosis", "") or ""),
                    )
                )

            phonemes = []
            if isinstance(phonemes_data, list):
                for p in phonemes_data:
                    if not isinstance(p, dict):
                        continue
                    symbol = str(p.get("phoneme", "") or "").strip()
                    if not symbol:
                        continue
                    phonemes.append(
                        PhonemeAlignment(
                            phoneme=symbol,
                            start=float(p.get("start", 0.0) or 0.0),
                            end=float(p.get("end", 0.0) or 0.0),
                            tag=_to_phoneme_tag(p.get("tag")),
                            score=float(p.get("score", 0.0) or 0.0),
                            in_word=str(p.get("in_word", "") or ""),
                        )
                    )

            aligned = Alignment(words=words, phonemes=phonemes)
            ensure_dense_phoneme_alignment(aligned)

            temp = ScoringResult()
            temp.alignment = aligned
            breakdown = _build_word_phoneme_breakdown(temp)

            for idx, row in enumerate(words_data):
                if isinstance(row, dict):
                    row["phoneme_breakdown"] = breakdown[idx] if idx < len(breakdown) else []

            alignment_data["phonemes"] = [
                {
                    "phoneme": p.phoneme,
                    "start": round(float(p.start), 3),
                    "end": round(float(p.end), 3),
                    "tag": p.tag.value,
                    "score": round(float(p.score), 1),
                    "in_word": p.in_word,
                }
                for p in aligned.phonemes
            ]
            data["alignment"] = alignment_data
    except Exception as e:
        logger.warning(f"regenerate_report_from_json enrichment skipped: {e}")
    
    # 加载模板
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )
    template = env.get_template("report.html.j2")
    
    # 获取颜色配置
    colors = {
        "ok": config.get("report.colors.ok", "#4CAF50"),
        "weak": config.get("report.colors.weak", "#FFC107"),
        "missing": config.get("report.colors.missing", "#F44336"),
        "poor": config.get("report.colors.poor", "#E91E63"),
    }
    
    # 添加颜色到数据
    data["colors"] = colors
    
    # 获取发音指导
    weak_phonemes = data.get("analysis", {}).get("weak_phonemes", [])
    data["phoneme_tips"] = get_phoneme_tips(weak_phonemes or [])
    
    # Try to preserve/recover audio for regenerated HTML.
    audio_base64 = data.get("audio_base64")
    if not audio_base64:
        meta = data.get("meta", {}) if isinstance(data.get("meta"), dict) else {}
        submission_id = str(meta.get("submission_id", "") or "")
        recovered_audio_path = _find_uploaded_audio_for_submission(submission_id, json_path)
        if recovered_audio_path:
            audio_base64 = encode_audio_base64(recovered_audio_path)
            if audio_base64:
                logger.info(f"Recovered audio for regenerated report: {recovered_audio_path}")
    data["audio_base64"] = audio_base64

    # 确保 advisor_feedback 存在，并清洗 top_errors 的空壳项
    if "advisor_feedback" not in data:
        data["advisor_feedback"] = None
    data["advisor_feedback"] = _sanitize_advisor_feedback(data.get("advisor_feedback"))

    # 确保 engine_raw 存在 (解决 UndefinedError)
    if "engine_raw" not in data:
        data["engine_raw"] = {}
    
    # 渲染 HTML
    html_content = template.render(**data)
    
    # 写入文件
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_content, encoding="utf-8")
    
    logger.info(f"HTML 报告已重新生成: {output_path}")

