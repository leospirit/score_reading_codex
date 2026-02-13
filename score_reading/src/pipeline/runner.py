
import json
import logging
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from src.advice.generator import generate_feedback
from src.analysis.llm_advisor import get_llm_advisor
from src.models import (
    EngineMode,
    Meta,
    ScoringResult,
)
from src.pipeline.analyze import analyze_results, assign_tags
from src.pipeline.normalize import normalize_scores
from src.pipeline.engines.whisper_engine import WhisperEngine
from src.pipeline.phoneme_fallback import ensure_dense_phoneme_alignment
from src.pipeline.preprocess import preprocess_audio
from src.pipeline.router import run_with_fallback
from src.pipeline.script_reference import ensure_script_reference_async
from src.report.render_html import render_html_report

logger = logging.getLogger("score_reading")

def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default






def _extract_display_name(student_id: str) -> str:
    raw = str(student_id or "").strip()
    default_name = "".join([chr(0x540C), chr(0x5B66)])
    if not raw:
        return default_name

    stem = Path(raw).stem
    stem = re.sub(r"(_v|_new)\d+$", "", stem, flags=re.IGNORECASE)

    def _is_cjk(ch: str) -> bool:
        code = ord(ch)
        return 0x4E00 <= code <= 0x9FFF

    leading = []
    for ch in stem:
        if _is_cjk(ch):
            leading.append(ch)
            continue
        if leading:
            break

    if len(leading) >= 2:
        return "".join(leading[:4])

    parts = [part for part in re.split(r"[_\-\s]+", stem) if part]
    for part in parts:
        run = []
        for ch in part:
            if _is_cjk(ch):
                run.append(ch)
            elif run:
                break
        if len(run) >= 2:
            return "".join(run[:4])

    if any(_is_cjk(ch) for ch in stem):
        only_cn = "".join(ch for ch in stem if _is_cjk(ch))
        if len(only_cn) >= 2:
            return only_cn[:4]
        if only_cn:
            return only_cn

    fallback = (parts[0] if parts else stem)[:16]
    return fallback or default_name


def _pick_focus_word(result: ScoringResult) -> str:
    advisor = result.advisor_feedback if isinstance(result.advisor_feedback, dict) else {}
    top_errors = advisor.get("top_errors")
    if isinstance(top_errors, list):
        for item in top_errors:
            if isinstance(item, dict):
                w = str(item.get("word", "")).strip()
                if not w:
                    words = item.get("words")
                    if isinstance(words, list) and words:
                        w = str(words[0]).strip()
            else:
                w = str(item).strip()
            if w:
                return w

    details = advisor.get("word_details")
    if isinstance(details, list):
        low = []
        for d in details:
            if not isinstance(d, dict):
                continue
            w = str(d.get("word", "")).strip()
            s = _safe_float(d.get("score"), 100.0)
            diagnosis = str(d.get("diagnosis", "") or "").strip()
            if w and s < 78 and diagnosis:
                low.append((s, w))
        if low:
            low.sort(key=lambda x: x[0])
            return low[0][1]

    weak_words = getattr(result.analysis, "weak_words", []) if result.analysis else []
    stop_words = {"lily", "today", "you", "your", "i", "we", "the", "a", "an"}
    if isinstance(weak_words, list):
        for w in weak_words:
            t = str(w).strip()
            if not t:
                continue
            key = re.sub(r"[^a-z']+", "", t.lower())
            if key and key not in stop_words:
                return t

    return ""


def _enforce_feedback_style(result: ScoringResult) -> None:
    if result.feedback is None:
        return

    summary = str(getattr(result.feedback, "cn_summary", "") or "").strip()
    actions = list(getattr(result.feedback, "cn_actions", []) or [])
    practice = list(getattr(result.feedback, "practice", []) or [])

    student_id = result.meta.student_id if result.meta else ""
    nickname = _extract_display_name(student_id)
    salutation = f"亲爱的{nickname}同学"

    p_score = _safe_float(getattr(result.scores, "pronunciation_100", 0.0), 0.0)
    f_score = _safe_float(getattr(result.scores, "fluency_100", 0.0), 0.0)
    i_score = _safe_float(getattr(result.scores, "intonation_100", 0.0), 0.0)
    c_score = _safe_float(getattr(result.scores, "completeness_100", 0.0), 0.0)
    highlights = [
        ("发音清晰度", p_score),
        ("流利度", f_score),
        ("语调节奏", i_score),
        ("完整度", c_score),
    ]
    best_metric, best_score = sorted(highlights, key=lambda x: x[1], reverse=True)[0]

    focus_word = _pick_focus_word(result)
    if focus_word:
        core_issue = f"最核心需要改进的是“{focus_word}”，建议慢读3遍再连读3遍，先准后快。"
        one_action = f"核心建议：把“{focus_word}”慢读3遍，再连读3遍，注意词尾发完整。"
    else:
        core_issue = "最核心需要改进的是词尾完整性，建议慢读3遍再连读3遍，先准后快。"
        one_action = "核心建议：选一个易错词慢读3遍，再连读3遍，注意词尾发完整。"

    if not summary:
        summary = (
            f"{salutation}，你这次朗读语气自然，整体节奏比较稳定。"
            f"最突出的优点是{best_metric}（{best_score:.0f}分）。"
            f"{core_issue}"
        )
    else:
        summary = re.sub(r"^\s*亲爱的[^，,。]*同学[，,。 ]*", "", summary)
        summary = re.sub(rf"^\s*{re.escape(nickname)}[，,。 ]*", "", summary)
        summary = f"{salutation}，{summary}"
        if "最核心" not in summary:
            summary = f"{summary.rstrip('。')}。{core_issue}"

    if not actions:
        actions = [one_action]
    else:
        first = str(actions[0]).strip()
        actions = [first or one_action]

    practice = [str(practice[0]).strip()] if practice and str(practice[0]).strip() else []

    result.feedback.cn_summary = summary
    result.feedback.cn_actions = actions
    result.feedback.practice = practice

    if isinstance(result.advisor_feedback, dict):
        result.advisor_feedback["overall_comment"] = summary
        result.advisor_feedback["specific_suggestions"] = actions
        result.advisor_feedback["practice_tips"] = practice
def run_scoring_pipeline(
    mp3_path: Path,
    text: str,
    output_dir: Path,
    student_id: str = "unknown",
    task_id: str = "default",
    submission_id: Optional[str] = None,
    engine_mode: EngineMode = EngineMode.AUTO,
    progress_callback = None
) -> Tuple[ScoringResult, Path, Path]:
    """
    运行完整的评分 Pipeline
    
    Returns:
        (result, json_path, html_path)
    """
    start_time = time.time()
    
    if not submission_id:
        import hashlib
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        random_hash = hashlib.md5(str(time.time()).encode()).hexdigest()[:8]
        submission_id = f"sub_{timestamp}_{random_hash}"
        
    # Ensure output dir exists
    final_output_dir = output_dir / task_id / student_id / submission_id
    final_output_dir.mkdir(parents=True, exist_ok=True)
    
    # Init result
    result = ScoringResult()
    result.meta = Meta(
        task_id=task_id,
        student_id=student_id,
        student_name=student_id,
        submission_id=submission_id,
        timestamp=datetime.now().isoformat(),
    )
    result.script_text = text
    result.engine_raw = {}  # Initialize to empty dict to avoid undefined errors

    def update_progress(desc: str):
        if progress_callback:
            progress_callback(desc)
        logger.info(desc)

    try:
        # Use a stable work directory under data for sibling container (DooD) support
        work_base = Path("data/work")
        work_base.mkdir(parents=True, exist_ok=True)
        
        with tempfile.TemporaryDirectory(dir=work_base) as work_dir:
            work_path = Path(work_dir)
            
            # 1. Preprocess
            update_progress("预处理音频...")
            wav_path, audio_metrics = preprocess_audio(mp3_path, work_path)
            result.audio = audio_metrics
            
            # 1.1 Auto-Transcribe if text is empty
            if not text or not text.strip():
                update_progress("正在自动识别朗读文本...")
                whisper = WhisperEngine()
                transcript_words = whisper._transcribe(wav_path)
                text = " ".join([w["word"] for w in transcript_words])
                result.script_text = text
                result.meta.is_auto_transcribed = True
                logger.info(f"自动识别结果: {text}")
            
            # 2. Run Engine
            update_progress("运行评分引擎...")
            if text and text.strip():
                # Warm up reference pronunciation profile in background cache.
                ensure_script_reference_async(text)
            alignment, engine_raw, engine_used, fallback_chain = run_with_fallback(
                wav_path=wav_path,
                script_text=text,
                work_dir=work_path,
                engine_mode=engine_mode,
                audio_metrics=audio_metrics,
            )
            
            result.alignment = alignment
            # Guarantee per-word phoneme alignment for UI factor breakdown.
            ensure_dense_phoneme_alignment(result.alignment)
            result.engine_raw = engine_raw
            if isinstance(result.engine_raw, dict):
                result.engine_raw["audio_duration_sec"] = float(audio_metrics.duration_sec)
            result.meta.engine_used = engine_used
            result.meta.fallback_chain = fallback_chain
            
            # 3. Normalize
            update_progress("计算分数...")
            result.scores = normalize_scores(
                engine_raw=engine_raw,
                audio_metrics=audio_metrics,
                alignment=alignment,
                script_text=text,
            )
            assign_tags(alignment)
            
            # 4. Analyze
            update_progress("分析结果...")
            result.analysis = analyze_results(alignment, text, engine_raw)

            # 4.1 Re-normalize after analysis so fluency uses finalized pause labels.
            result.scores = normalize_scores(
                engine_raw=engine_raw,
                audio_metrics=audio_metrics,
                alignment=alignment,
                script_text=text,
            )
            
            # 5. Feedback
            update_progress("生成建议...")
            result.feedback = generate_feedback(result.analysis)
            
            # 5.1 LLM Feedback (Priority: Engine-Native Multimodal Feedback)
            update_progress("AI 老师点评中...")
            try:
                # 检查引擎是否已经提供了深度反馈 (如 Gemini 2.0 原生多模态反馈)
                integrated = (result.engine_raw or {}).get("integrated_feedback")
                integrated_source = str((result.engine_raw or {}).get("source", "")).lower()
                
                # 如果引擎已经提供了完整点评 (即 multimodal path)，则直接使用，避免二次调用 LLM 造成质量摊薄
                # Only trust engine-native integrated feedback when it is truly from Gemini path.
                # Wav2Vec2 fallback may include a generic template and should still go through advisor.
                if (
                    integrated
                    and integrated.get("overall_comment")
                    and "gemini" in integrated_source
                ):
                    logger.info("Using engine-native multimodal feedback (High Fidelity Path)")
                    from src.models import Feedback
                    result.feedback = Feedback(
                        cn_summary=integrated.get("overall_comment"),
                        cn_actions=integrated.get("specific_suggestions", []),
                        practice=integrated.get("practice_tips", [])
                    )
                    # 确保 advisor_feedback 也被填充，用于 UI 展现
                    result.advisor_feedback = integrated
                else:
                    # 如果引擎没有集成点评 (如 Wav2Vec2)，则按需调用 Advisor (Slow Path)
                    logger.info("No integrated feedback found, calling LLM Advisor (Standard Path)")
                    advisor = get_llm_advisor()
                    result.feedback, result.advisor_feedback = advisor.generate_feedback(result)
            except Exception as e:
                logger.warning(f"AI 点评失败: {e}")
                # Fallback to a basic message if everything fails
                if not result.feedback:
                     from src.models import Feedback
                     result.feedback = Feedback(cn_summary="评分分析完成，请查收建议。", cn_actions=[], practice=[])
            
            # Enforce a stable final feedback style across all paths.
            _enforce_feedback_style(result)

            # Finalize
            result.meta.processing_time_ms = int((time.time() - start_time) * 1000)
            
            # 6. Save
            update_progress("保存结果...")
            json_path = final_output_dir / f"{submission_id}.json"
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
                
            html_path = final_output_dir / f"{submission_id}.html"
            render_html_report(result, html_path, audio_path=mp3_path)
            
            return result, json_path, html_path

    except Exception as e:
        result.error = str(e)
        result.meta.processing_time_ms = int((time.time() - start_time) * 1000)
        
        # Save error result
        json_path = final_output_dir / f"{submission_id}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
            
        raise e
