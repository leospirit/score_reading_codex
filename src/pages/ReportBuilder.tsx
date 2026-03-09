import { useState, useEffect, useCallback, useMemo, useRef, type ChangeEvent } from 'react';
import { Printer, GripVertical, Check, X, Plus, Eye, Camera, Download } from 'lucide-react';
import JSZip, { type JSZipObject } from 'jszip';
import { API_HOST } from '../config/api';
import { getFeedbackStatusHint } from './reportFeedbackStatus';

// 鍙敤妯″潡瀹氫箟
interface ModuleConfig {
    id: string;
    name: string;
    icon: string;
    description: string;
    isDefault: boolean;
}

const AVAILABLE_MODULES: ModuleConfig[] = [
    { id: 'score_overview', name: '总分概览', icon: '📊', description: '圆环图 + 四维分数', isDefault: true },
    { id: 'text_highlight', name: '朗读对照', icon: '📉', description: '带颜色标注的朗读文本', isDefault: true },
    { id: 'pronunciation_diagnosis', name: '发音诊断', icon: '🎯', description: '弱读词及音素问题', isDefault: true },
    { id: 'ai_feedback', name: '综合反馈', icon: '👩‍🏫', description: '基于事实的表扬与建议', isDefault: true },
    { id: 'fluency_analysis', name: '流利度分析', icon: '〰️', description: '停顿/语速/迟疑', isDefault: false },
    { id: 'intonation_analysis', name: '语调分析', icon: '🗣️', description: '重音与节奏可视化', isDefault: false },
    { id: 'completeness', name: '完整度分析', icon: '📝', description: '漏词统计', isDefault: false },
    { id: 'hesitation', name: '迟疑分析', icon: '⏱️', description: '填充词与长停顿', isDefault: false },
];

// 鍩轰簬瀹為檯 JSON 缁撴瀯鐨勬帴鍙ｅ畾涔?
interface ReportData {
    script_text?: string;
    meta: {
        student_id: string;
        student_name: string;
        timestamp: string;
    };
    scores: {
        overall_100: number;
        pronunciation_100: number;
        fluency_100: number;
        intonation_100: number;
        completeness_100: number;
    };
    alignment: {
        words: Array<{
            word: string;
            tag: string;
            score: number;
            stress?: number;
            expected_stress?: number;
            start?: number;
            end?: number;
            pause?: {
                type: 'good' | 'optional' | 'bad' | 'missed';
                duration: number;
            };
        }>;
        phonemes?: Array<{
            phoneme?: string;
            score?: number;
            in_word?: string;
        }>;
    };
    analysis: {
        weak_words: string[];
        weak_phonemes: string[];
        missing_words: string[];
        missing_indices?: number[];
        mistakes: Array<{
            type: string;
            target: string;
            word: string;
            desc: string;
            severity: string;
            score: number;
        }>;
        hesitations?: {
            total_count: number;
            filler_count: number;
            long_pause_count: number;
            filler_words: string[];
        };
        completeness?: {
            coverage?: number;
            missing_stats?: {
                total?: number;
                keywords?: number;
                function_words?: number;
            };
            expected_words?: number;
            spoken_words?: number;
            missing_count?: number;
        };
        intonation_analysis?: {
            best_sentence?: {
                sentence: string;
                words: Array<{ word: string; is_stressed: boolean; stress_correct: boolean }>;
                stress_accuracy: number;
                tip: string;
            };
            problem_sentences: Array<{
                sentence: string;
                words: Array<{ word: string; is_stressed: boolean; stress_correct: boolean }>;
                stress_accuracy: number;
                tip: string;
            }>;
        };
    };
    engine_raw: {
        source?: string;
        annotation_source?: string;
        feedback_source_tag?: string;
        detected_transcript?: string;
        gemini_detected_transcript?: string;
        gemini_missing_indices?: number[];
        pause_count?: number;
        total_pause_duration?: number;
        wpm?: number;
        pause_profile?: {
            synthetic_timeline?: number;
            low_confidence_timing?: number;
            timing_confidence?: string;
        };
        fluency_components?: {
            pausing_score?: number;
            pace_score?: number;
            hesitation_score?: number;
            final_fluency_score?: number;
        };
        integrated_feedback?: {
            overall_comment: string;
            specific_suggestions: string[];
            practice_tips: string[];
            fun_challenge: string;
        };
    };
    feedback_override?: {
        integrated_feedback_text?: string;
        updated_at?: number;
        updated_by?: string;
    };
    feedback_optimization?: {
        status?: 'pending' | 'optimizing' | 'frozen' | 'final';
        version?: number;
        current_provider?: string;
        current_text?: string;
        updated_at?: number;
        last_error?: string;
        freeze_reason?: string;
    };
}

type FeedbackPhraseCategory = 'praise' | 'issue' | 'advice' | 'encourage';
type TeacherPhraseItem = {
    id: string;
    text: string;
    category: FeedbackPhraseCategory;
    use_count?: number;
    created_at?: number;
    updated_at?: number;
    last_used_at?: number;
    builtin?: boolean;
};
type AiFeedbackSuggestion = {
    id: string;
    category: FeedbackPhraseCategory;
    label: string;
    text: string;
};

type IntonationWordView = { word: string; is_stressed: boolean; stress_correct: boolean };
type IntonationSentenceView = { sentence: string; words: IntonationWordView[]; stress_accuracy: number; tip: string };
type IntonationAnalysisView = { best_sentence?: IntonationSentenceView; problem_sentences: IntonationSentenceView[] };
type WritableFileLike = { write(data: Blob): Promise<void>; close(): Promise<void> };
type SaveFileHandleLike = { createWritable(): Promise<WritableFileLike> };
type DirectoryHandleLike = { getFileHandle(name: string, options: { create: boolean }): Promise<SaveFileHandleLike> };
type WindowWithFsAccess = Window & {
    showSaveFilePicker?: (options?: unknown) => Promise<SaveFileHandleLike>;
    showDirectoryPicker?: (options?: unknown) => Promise<DirectoryHandleLike>;
};
type CaptureTemplate = 'classic' | 'aurora' | 'card' | 'mint' | 'sunset' | 'ink' | 'imported';
type ImportedTemplateAssets = {
    name: string;
    topDataUrl: string;
    middleDataUrl: string;
    bottomDataUrl: string;
    topHeightPx: number;
    bottomHeightPx: number;
    contentPaddingPx: number;
};
type ImportedTemplateManifest = {
    name?: string;
    top_height_px?: number;
    bottom_height_px?: number;
    content_padding_px?: number;
    files?: {
        top?: string;
        middle?: string;
        bottom?: string;
    };
};

const CAPTURE_TEMPLATE_LABEL: Record<CaptureTemplate, string> = {
    classic: '经典白底',
    aurora: '柔彩渐变',
    card: '卡片海报',
    mint: '薄荷清新',
    sunset: '暖阳橙金',
    ink: '墨蓝课堂',
    imported: '导入模板',
};
const CAPTURE_TEMPLATE_STORAGE_KEY = 'score_reading.capture_template';

function isCaptureTemplate(raw: unknown): raw is CaptureTemplate {
    if (typeof raw !== 'string') return false;
    return Object.prototype.hasOwnProperty.call(CAPTURE_TEMPLATE_LABEL, raw);
}

function readCaptureTemplateFromStorage(): CaptureTemplate {
    if (typeof window === 'undefined') return 'classic';
    try {
        const raw = window.localStorage.getItem(CAPTURE_TEMPLATE_STORAGE_KEY);
        if (isCaptureTemplate(raw) && raw !== 'imported') return raw;
    } catch {
        // ignore
    }
    return 'classic';
}

const getErrorMessage = (value: unknown, fallback: string): string => {
    if (value instanceof Error && value.message) return value.message;
    if (typeof value === 'string' && value.trim()) return value.trim();
    if (value && typeof value === 'object' && 'message' in value) {
        const message = String((value as { message?: unknown }).message || '').trim();
        if (message) return message;
    }
    return fallback;
};

const formatActionError = (action: string, value: unknown, fallback: string): string => {
    return `${action} failed: ${getErrorMessage(value, fallback)}`;
};

function formatScoreCompact(value: number, digits = 1): string {
    const n = Number(value);
    if (!Number.isFinite(n)) return '--';
    const factor = Math.pow(10, digits);
    const rounded = Math.round(n * factor) / factor;
    if (Number.isInteger(rounded)) return String(Math.trunc(rounded));
    return rounded.toFixed(digits).replace(/\.0+$/, '').replace(/(\.\d*[1-9])0+$/, '$1');
}

function buildIntegratedFeedbackText(integrated: ReportData['engine_raw']['integrated_feedback'] | undefined): string {
    if (!integrated) return '';
    const lines: string[] = [];
    const overall = String(integrated.overall_comment || '').trim();
    if (overall) lines.push(overall);

    const suggestion = Array.isArray(integrated.specific_suggestions) && integrated.specific_suggestions.length > 0
        ? String(integrated.specific_suggestions[0] || '').trim()
        : '';
    if (suggestion) {
        const normalized = /^建议[:：]/.test(suggestion) ? suggestion : `建议：${suggestion}`;
        lines.push(normalized);
    }

    return lines.join('\n\n').trim();
}

type GradeThresholds = {
    cMin: number;
    bMin: number;
    aMin: number;
    aPlusMin: number;
};

const DEFAULT_GRADE_THRESHOLDS: GradeThresholds = {
    // Default: 0-74 C, 75-84 B, 85-94 A, 95-100 A+
    cMin: 0,
    bMin: 75,
    aMin: 85,
    aPlusMin: 95,
};
const SCORE_VIEW_MODE_STORAGE_KEY = 'score_reading.score_view_mode';
const GRADE_THRESHOLDS_STORAGE_KEY = 'score_reading.grade_thresholds';
const FEEDBACK_PHRASE_CATEGORY_LABEL: Record<FeedbackPhraseCategory, string> = {
    praise: '表扬',
    issue: '问题',
    advice: '建议',
    encourage: '鼓励',
};
const FEEDBACK_PHRASE_CHIP_CLASS: Record<FeedbackPhraseCategory, string> = {
    praise: 'border-emerald-300 text-emerald-700 bg-emerald-50 hover:bg-emerald-100',
    issue: 'border-rose-300 text-rose-700 bg-rose-50 hover:bg-rose-100',
    advice: 'border-amber-300 text-amber-700 bg-amber-50 hover:bg-amber-100',
    encourage: 'border-sky-300 text-sky-700 bg-sky-50 hover:bg-sky-100',
};
const EXPORT_A4_WIDTH_PX = 1240;
const EXPORT_A4_HEIGHT_PX = 1754;
const EXPORT_PIXEL_RATIO = 2;
const TEMPLATE_ZIP_MAX_MB = 20;
const TEMPLATE_DEFAULT_TOP_HEIGHT_PX = 220;
const TEMPLATE_DEFAULT_BOTTOM_HEIGHT_PX = 160;
const TEMPLATE_DEFAULT_CONTENT_PADDING_PX = 72;

const pickZipEntry = (zip: JSZip, names: string[]): JSZipObject | null => {
    for (const name of names) {
        const direct = zip.file(name);
        if (direct) return direct;
        const bySuffix = zip.file(new RegExp(`${name.replace('.', '\\.')}$`, 'i'));
        if (bySuffix && bySuffix.length > 0) return bySuffix[0];
    }
    return null;
};

const imageDataUrlFromZipEntry = async (entry: JSZipObject): Promise<string> => {
    const base64 = await entry.async('base64');
    const lower = entry.name.toLowerCase();
    const mime = lower.endsWith('.jpg') || lower.endsWith('.jpeg')
        ? 'image/jpeg'
        : lower.endsWith('.webp')
            ? 'image/webp'
            : 'image/png';
    return `data:${mime};base64,${base64}`;
};

const fileToDataUrl = (file: File): Promise<string> => {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onerror = () => reject(new Error(`读取文件失败: ${file.name}`));
        reader.onload = () => resolve(String(reader.result || ''));
        reader.readAsDataURL(file);
    });
};

const clamp = (value: number, min: number, max: number): number => {
    return Math.min(max, Math.max(min, value));
};

const loadImageFromDataUrl = (dataUrl: string): Promise<HTMLImageElement> => {
    return new Promise((resolve, reject) => {
        const img = new Image();
        img.onload = () => resolve(img);
        img.onerror = () => reject(new Error('图片加载失败'));
        img.src = dataUrl;
    });
};

const cropImageToDataUrl = (
    image: HTMLImageElement,
    sx: number,
    sy: number,
    sw: number,
    sh: number,
): string => {
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(sw));
    canvas.height = Math.max(1, Math.round(sh));
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('Canvas context unavailable');
    ctx.drawImage(
        image,
        Math.round(sx),
        Math.round(sy),
        Math.round(sw),
        Math.round(sh),
        0,
        0,
        canvas.width,
        canvas.height,
    );
    return canvas.toDataURL('image/png');
};

type RowProfile = {
    r: number;
    g: number;
    b: number;
    edge: number;
};

const mean = (values: number[]): number => {
    if (!values.length) return 0;
    return values.reduce((sum, value) => sum + value, 0) / values.length;
};

const buildRowProfiles = (image: HTMLImageElement): { rows: RowProfile[]; sampleHeight: number; sourceHeight: number } => {
    const sourceWidth = Math.max(1, image.naturalWidth || image.width);
    const sourceHeight = Math.max(1, image.naturalHeight || image.height);
    const sampleWidth = Math.min(320, sourceWidth);
    const ratio = sampleWidth / sourceWidth;
    const sampleHeight = Math.max(240, Math.min(1800, Math.round(sourceHeight * ratio)));

    const canvas = document.createElement('canvas');
    canvas.width = sampleWidth;
    canvas.height = sampleHeight;
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('Canvas context unavailable');
    ctx.drawImage(image, 0, 0, sampleWidth, sampleHeight);

    const data = ctx.getImageData(0, 0, sampleWidth, sampleHeight).data;
    const rows: RowProfile[] = [];
    for (let y = 0; y < sampleHeight; y++) {
        let sumR = 0;
        let sumG = 0;
        let sumB = 0;
        let edge = 0;
        let prevR = 0;
        let prevG = 0;
        let prevB = 0;
        for (let x = 0; x < sampleWidth; x++) {
            const idx = (y * sampleWidth + x) * 4;
            const r = data[idx];
            const g = data[idx + 1];
            const b = data[idx + 2];
            sumR += r;
            sumG += g;
            sumB += b;
            if (x > 0) {
                edge += Math.abs(r - prevR) + Math.abs(g - prevG) + Math.abs(b - prevB);
            }
            prevR = r;
            prevG = g;
            prevB = b;
        }
        rows.push({
            r: sumR / sampleWidth,
            g: sumG / sampleWidth,
            b: sumB / sampleWidth,
            edge: edge / Math.max(1, (sampleWidth - 1) * 3 * 255),
        });
    }
    return { rows, sampleHeight, sourceHeight };
};

const findAutoSliceCuts = (image: HTMLImageElement): { topCutPx: number; bottomCutPx: number } => {
    const { rows, sampleHeight, sourceHeight } = buildRowProfiles(image);
    const edges = rows.map((row) => row.edge);
    const smoothEdges = edges.map((_, y) => {
        const win = [
            edges[Math.max(0, y - 2)],
            edges[Math.max(0, y - 1)],
            edges[y],
            edges[Math.min(sampleHeight - 1, y + 1)],
            edges[Math.min(sampleHeight - 1, y + 2)],
        ];
        return mean(win);
    });

    const topMin = Math.round(sampleHeight * 0.1);
    const topMax = Math.round(sampleHeight * 0.42);
    const bottomMin = Math.round(sampleHeight * 0.56);
    const bottomMax = Math.round(sampleHeight * 0.92);
    const topTarget = Math.round(sampleHeight * 0.22);
    const bottomTarget = Math.round(sampleHeight * 0.82);
    const middleTarget = Math.round(sampleHeight * 0.58);
    const minMiddle = Math.max(50, Math.round(sampleHeight * 0.26));

    let bestTop = topTarget;
    let bestBottom = bottomTarget;
    let bestScore = Number.POSITIVE_INFINITY;

    for (let top = topMin; top <= topMax; top++) {
        for (let bottom = bottomMin; bottom <= bottomMax; bottom++) {
            const middle = bottom - top;
            if (middle < minMiddle) continue;

            const a = rows[top];
            const b = rows[bottom];
            const colorDiff =
                (Math.abs(a.r - b.r) + Math.abs(a.g - b.g) + Math.abs(a.b - b.b)) / (3 * 255);
            const edgePenalty = smoothEdges[top] + smoothEdges[bottom];
            const positionPenalty =
                Math.abs(top - topTarget) / sampleHeight +
                Math.abs(bottom - bottomTarget) / sampleHeight;
            const middlePenalty = Math.abs(middle - middleTarget) / sampleHeight;
            const score = colorDiff * 0.62 + edgePenalty * 0.2 + positionPenalty * 0.1 + middlePenalty * 0.08;

            if (score < bestScore) {
                bestScore = score;
                bestTop = top;
                bestBottom = bottom;
            }
        }
    }

    const sampleToSource = (y: number) => Math.round((y / Math.max(1, sampleHeight - 1)) * Math.max(1, sourceHeight - 1));
    let topCutPx = sampleToSource(bestTop);
    let bottomCutPx = sampleToSource(bestBottom);

    const minTopPx = Math.max(80, Math.round(sourceHeight * 0.1));
    const minBottomPx = Math.max(80, Math.round(sourceHeight * 0.1));
    const minMiddlePx = Math.max(120, Math.round(sourceHeight * 0.22));

    topCutPx = clamp(topCutPx, minTopPx, sourceHeight - minBottomPx - minMiddlePx);
    bottomCutPx = clamp(bottomCutPx, topCutPx + minMiddlePx, sourceHeight - minBottomPx);

    return { topCutPx, bottomCutPx };
};

const getTemplateCaptureStyle = (
    template: CaptureTemplate,
    _importedTemplate: ImportedTemplateAssets | null,
): { backgroundColor: string; style: Record<string, string> } => {
    if (template === 'imported') {
        return {
            backgroundColor: '#ffffff',
            style: {
                background: '#ffffff',
                padding: '0px',
                borderRadius: '0px',
            },
        };
    }
    if (template === 'aurora') {
        return {
            backgroundColor: '#f4f7ff',
            style: {
                background: 'radial-gradient(90% 80% at 12% 8%, rgba(56,189,248,0.22), transparent 60%), radial-gradient(90% 80% at 92% 10%, rgba(251,113,133,0.18), transparent 58%), linear-gradient(160deg, #f8fbff 0%, #eef4ff 100%)',
                padding: '72px',
                borderRadius: '24px',
            },
        };
    }
    if (template === 'card') {
        return {
            backgroundColor: '#eef2f7',
            style: {
                background: 'linear-gradient(160deg, #f7f9fc 0%, #edf2f8 100%)',
                padding: '64px',
                borderRadius: '22px',
            },
        };
    }
    if (template === 'mint') {
        return {
            backgroundColor: '#f2fbf8',
            style: {
                background: 'radial-gradient(95% 85% at 10% 8%, rgba(45, 212, 191, 0.20), transparent 62%), radial-gradient(85% 78% at 92% 12%, rgba(16, 185, 129, 0.16), transparent 60%), linear-gradient(170deg, #f4fffb 0%, #edf8f5 100%)',
                padding: '66px',
                borderRadius: '24px',
            },
        };
    }
    if (template === 'sunset') {
        return {
            backgroundColor: '#fff7ef',
            style: {
                background: 'radial-gradient(88% 82% at 14% 10%, rgba(251, 146, 60, 0.18), transparent 60%), radial-gradient(90% 84% at 90% 12%, rgba(234, 179, 8, 0.18), transparent 62%), linear-gradient(165deg, #fff9f1 0%, #fff1de 100%)',
                padding: '66px',
                borderRadius: '24px',
            },
        };
    }
    if (template === 'ink') {
        return {
            backgroundColor: '#edf3ff',
            style: {
                background: 'radial-gradient(90% 80% at 12% 9%, rgba(37, 99, 235, 0.16), transparent 62%), radial-gradient(88% 76% at 90% 11%, rgba(30, 64, 175, 0.18), transparent 60%), linear-gradient(160deg, #f6f9ff 0%, #eaf1ff 100%)',
                padding: '66px',
                borderRadius: '24px',
            },
        };
    }
    return {
        backgroundColor: '#ffffff',
        style: {
            background: '#ffffff',
            padding: '54px',
            borderRadius: '0px',
        },
    };
};

function normalizeFeedbackPhraseCategory(raw: unknown): FeedbackPhraseCategory {
    const value = String(raw || '').trim().toLowerCase();
    if (value === 'issue' || value === 'advice' || value === 'encourage' || value === 'praise') return value;
    return 'praise';
}

function parseTeacherPhraseItem(raw: unknown): TeacherPhraseItem | null {
    if (!raw || typeof raw !== 'object') return null;
    const source = raw as {
        id?: unknown;
        text?: unknown;
        category?: unknown;
        use_count?: unknown;
        created_at?: unknown;
        updated_at?: unknown;
        last_used_at?: unknown;
        builtin?: unknown;
    };
    const id = String(source.id || '').trim();
    const text = String(source.text || '').trim();
    if (!id || !text) return null;
    return {
        id,
        text,
        category: normalizeFeedbackPhraseCategory(source.category),
        use_count: Number(source.use_count ?? 0),
        created_at: Number(source.created_at ?? 0),
        updated_at: Number(source.updated_at ?? 0),
        last_used_at: Number(source.last_used_at ?? 0),
        builtin: Boolean(source.builtin),
    };
}

function sortTeacherPhraseItems(items: TeacherPhraseItem[]): TeacherPhraseItem[] {
    return [...items].sort((a, b) => {
        const useDiff = Number(b.use_count || 0) - Number(a.use_count || 0);
        if (useDiff !== 0) return useDiff;
        const recentDiff = Number(b.last_used_at || 0) - Number(a.last_used_at || 0);
        if (recentDiff !== 0) return recentDiff;
        const updateDiff = Number(b.updated_at || 0) - Number(a.updated_at || 0);
        if (updateDiff !== 0) return updateDiff;
        return a.text.localeCompare(b.text);
    });
}

function readScoreViewModeFromStorage(): 'score' | 'grade' {
    if (typeof window === 'undefined') return 'score';
    try {
        return window.localStorage.getItem(SCORE_VIEW_MODE_STORAGE_KEY) === 'grade' ? 'grade' : 'score';
    } catch {
        return 'score';
    }
}

function readGradeThresholdsFromStorage(): GradeThresholds {
    if (typeof window === 'undefined') return DEFAULT_GRADE_THRESHOLDS;
    try {
        const raw = window.localStorage.getItem(GRADE_THRESHOLDS_STORAGE_KEY);
        if (!raw) return DEFAULT_GRADE_THRESHOLDS;
        const parsed = JSON.parse(raw) as Partial<GradeThresholds>;
        const normalized = normalizeGradeThresholds(parsed);
        return isLegacyDefaultThresholds(normalized) ? DEFAULT_GRADE_THRESHOLDS : normalized;
    } catch {
        return DEFAULT_GRADE_THRESHOLDS;
    }
}

function parseReportDisplayPayload(raw: unknown): { mode: 'score' | 'grade'; thresholds: GradeThresholds } {
    if (!raw || typeof raw !== 'object') {
        return {
            mode: readScoreViewModeFromStorage(),
            thresholds: readGradeThresholdsFromStorage(),
        };
    }
    const source = raw as {
        score_view_mode?: unknown;
        scoreViewMode?: unknown;
        grade_thresholds?: unknown;
        gradeThresholds?: unknown;
    };
    const mode: 'score' | 'grade' = String(source.score_view_mode ?? source.scoreViewMode ?? '').trim().toLowerCase() === 'grade'
        ? 'grade'
        : 'score';
    const thresholdRaw = (source.grade_thresholds ?? source.gradeThresholds ?? {}) as {
        c_min?: unknown;
        b_min?: unknown;
        a_min?: unknown;
        a_plus_min?: unknown;
        cMin?: unknown;
        bMin?: unknown;
        aMin?: unknown;
        aPlusMin?: unknown;
    };
    const thresholds = normalizeGradeThresholds({
        cMin: Number(thresholdRaw.c_min ?? thresholdRaw.cMin),
        bMin: Number(thresholdRaw.b_min ?? thresholdRaw.bMin),
        aMin: Number(thresholdRaw.a_min ?? thresholdRaw.aMin),
        aPlusMin: Number(thresholdRaw.a_plus_min ?? thresholdRaw.aPlusMin),
    });
    return { mode, thresholds: isLegacyDefaultThresholds(thresholds) ? DEFAULT_GRADE_THRESHOLDS : thresholds };
}

function clampInt(value: number, min: number, max: number): number {
    if (!Number.isFinite(value)) return min;
    return Math.min(max, Math.max(min, Math.round(value)));
}

function normalizeGradeThresholds(raw: Partial<GradeThresholds>): GradeThresholds {
    const cMin = clampInt(Number(raw.cMin), 0, 97);
    const bMin = clampInt(Number(raw.bMin), cMin + 1, 98);
    const aMin = clampInt(Number(raw.aMin), bMin + 1, 99);
    const aPlusMin = clampInt(Number(raw.aPlusMin), aMin + 1, 100);
    return { cMin, bMin, aMin, aPlusMin };
}

function isLegacyDefaultThresholds(thresholds: GradeThresholds): boolean {
    return thresholds.cMin === 61 && thresholds.bMin === 71 && thresholds.aMin === 81 && thresholds.aPlusMin === 86;
}

function getGradeInfo(score: number, thresholds: GradeThresholds): { label: string; color: string } {
    const n = Number(score);
    if (!Number.isFinite(n)) return { label: '--', color: '#6B7280' };
    if (n >= thresholds.aPlusMin) return { label: 'A+', color: '#A855F7' };
    if (n >= thresholds.aMin) return { label: 'A', color: '#22C55E' };
    if (n >= thresholds.bMin) return { label: 'B', color: '#3B82F6' };
    return { label: 'C', color: '#F59E0B' };
}

function isLowConfidenceTimeline(words: ReportData['alignment']['words']): boolean {
    if (!Array.isArray(words) || words.length < 7) return false;
    const gaps: number[] = [];
    for (let i = 0; i < words.length - 1; i += 1) {
        const left = Number(words[i]?.end);
        const right = Number(words[i + 1]?.start);
        if (!Number.isFinite(left) || !Number.isFinite(right)) continue;
        gaps.push(Math.max(0, right - left));
    }
    if (gaps.length < 6) return false;
    const sorted = [...gaps].sort((a, b) => a - b);
    const median = sorted[Math.floor(sorted.length / 2)];
    const near = gaps.filter((g) => Math.abs(g - median) <= 0.02).length / Math.max(1, gaps.length);
    const mean = gaps.reduce((s, g) => s + g, 0) / gaps.length;
    const variance = gaps.reduce((s, g) => s + (g - mean) * (g - mean), 0) / Math.max(1, gaps.length);
    const std = Math.sqrt(variance);
    if (median >= 0.07 && median <= 0.16 && near >= 0.65) return true;
    if (median <= 0.20 && std <= 0.02 && near >= 0.55) return true;
    return false;
}

function inferExpectedStress(wordText: string): boolean {
    const token = String(wordText || '').toLowerCase().replace(/[^a-z']/g, '');
    if (!token) return false;
    const functionWords = new Set([
        'a', 'an', 'the', 'of', 'in', 'on', 'at', 'to', 'for', 'with', 'by',
        'is', 'are', 'am', 'was', 'were', 'be', 'been', 'being',
        'and', 'or', 'but', 'so', 'if', 'as', 'than', 'that', 'this', 'these', 'those',
        'i', 'you', 'he', 'she', 'it', 'we', 'they', 'me', 'him', 'her', 'us', 'them',
        'my', 'your', 'his', 'its', 'our', 'their',
        'do', 'does', 'did', 'can', 'could', 'will', 'would', 'shall', 'should',
        'have', 'has', 'had', 'not', 'no', 'yes', 'oh', 'too',
    ]);
    return !functionWords.has(token);
}

function normalizeDisplayWord(rawText: string): string {
    const raw = String(rawText || '');
    if (!raw) return '';
    return raw
        .replace(/([A-Za-z'])([,.;:!?]+)(?=[A-Za-z'])/g, '$1 ')
        .replace(/\s+/g, ' ')
        .trim();
}

function isMergedPunctuationToken(rawText: string): boolean {
    const raw = String(rawText || '');
    if (!raw) return false;
    return /[A-Za-z'][,.;:!?]+[A-Za-z']/.test(raw);
}

function normalizeWordToken(rawText: string): string {
    return String(rawText || '').toLowerCase().replace(/[^a-z']/g, '');
}

function buildScriptAnchoredWords(
    scriptText: string,
    words: ReportData['alignment']['words'],
    missingIndices: number[] = []
): ReportData['alignment']['words'] {
    const scriptTokens = String(scriptText || '').match(/[A-Za-z']+/g) || [];
    if (!scriptTokens.length) return words;
    const missingSet = new Set<number>();
    missingIndices.forEach((raw) => {
        const idx = Number(raw);
        if (Number.isInteger(idx) && idx >= 0 && idx < scriptTokens.length) {
            missingSet.add(idx);
        }
    });

    const normalizedWords = words.map((w) => normalizeWordToken(w.word));
    const used = new Set<number>();
    let cursor = 0;

    return scriptTokens.map((token, sIdx) => {
        const norm = normalizeWordToken(token);
        let match = -1;
        for (let j = cursor; j < normalizedWords.length; j += 1) {
            if (used.has(j)) continue;
            if (normalizedWords[j] === norm) {
                match = j;
                break;
            }
            if (j - cursor > 6) break;
        }
        if (match < 0 && sIdx < words.length && !used.has(sIdx)) {
            match = sIdx;
        }

        if (match >= 0) {
            used.add(match);
            cursor = Math.max(cursor, match + 1);
            const base = words[match];
            if (missingSet.has(sIdx)) {
                return { ...base, word: token, tag: 'missing', score: 0 };
            }
            return { ...base, word: token };
        }

        if (missingSet.has(sIdx)) {
            return { word: token, tag: 'missing', score: 0 };
        }
        return { word: token, tag: 'ok', score: 100 };
    });
}

function quantile(values: number[], q: number): number {
    if (!Array.isArray(values) || values.length === 0) return 0;
    const sorted = [...values].sort((a, b) => a - b);
    const pos = Math.max(0, Math.min(sorted.length - 1, Math.round((sorted.length - 1) * q)));
    return sorted[pos];
}

function splitIntonationSentences(words: ReportData['alignment']['words']): ReportData['alignment']['words'][] {
    const chunks: ReportData['alignment']['words'][] = [];
    let current: ReportData['alignment']['words'] = [];
    words.forEach((w, idx) => {
        current.push(w);
        const pauseDuration = Number(w.pause?.duration || 0);
        const shouldCut = pauseDuration >= 0.38 || current.length >= 16 || idx === words.length - 1;
        if (shouldCut) {
            if (current.length >= 4) chunks.push(current);
            current = [];
        }
    });
    if (!chunks.length && words.length) chunks.push(words);
    return chunks;
}

function deriveIntonationFallback(words: ReportData['alignment']['words']): IntonationAnalysisView | null {
    if (!Array.isArray(words) || words.length < 4) return null;

    const sentenceChunks = splitIntonationSentences(words);
    if (!sentenceChunks.length) return null;

    const evaluated = sentenceChunks.map((chunk) => {
        const stressValues = chunk.map((w) => {
            const stress = Number(w.stress ?? 0);
            const score = Number(w.score ?? 0);
            return stress > 0.01 ? Math.max(0, Math.min(1, stress)) : Math.max(0, Math.min(1, score / 100));
        });
        const cutoff = Math.max(0.56, Math.min(0.82, quantile(stressValues, 0.68)));

        let stressTargets = 0;
        let stressCorrect = 0;
        const issueWords: string[] = [];
        const tokenViews: IntonationWordView[] = chunk.map((w, idx) => {
            const expected = Number.isFinite(Number(w.expected_stress))
                ? Number(w.expected_stress) >= 0.62
                : inferExpectedStress(w.word);
            const tag = String(w.tag || '').toLowerCase();
            const blocked = tag === 'missing' || tag === 'poor';
            const actual = (stressValues[idx] || 0) >= cutoff;
            if (expected) {
                stressTargets += 1;
                const ok = actual && !blocked;
                if (ok) stressCorrect += 1;
                if (!ok) issueWords.push(w.word);
                return { word: w.word, is_stressed: true, stress_correct: ok };
            }
            const overStress = actual && !blocked;
            if (overStress) issueWords.push(w.word);
            return { word: w.word, is_stressed: false, stress_correct: !overStress };
        });

        const accuracy = stressTargets > 0 ? Math.round((stressCorrect / stressTargets) * 100) : 0;
        return {
            sentence: chunk.map((w) => w.word).join(' '),
            words: tokenViews,
            stress_accuracy: accuracy,
            issueWords: [...new Set(issueWords)].slice(0, 3),
        };
    });

    const sorted = [...evaluated].sort((a, b) => b.stress_accuracy - a.stress_accuracy);
    const best = sorted[0];
    const worst = [...evaluated].sort((a, b) => a.stress_accuracy - b.stress_accuracy)[0];
    if (!best || !worst) return null;

    const bestSentence: IntonationSentenceView = {
        sentence: best.sentence,
        words: best.words,
        stress_accuracy: best.stress_accuracy,
        tip: '这句关键词比较突出，重弱分布较自然。继续保持这个节奏。',
    };

    const issueText = worst.issueWords.length
        ? `重点改进：${worst.issueWords.join(' / ')}`
        : '重点改进：加强重弱对比。';
    const worstSentence: IntonationSentenceView = {
        sentence: worst.sentence,
        words: worst.words,
        stress_accuracy: worst.stress_accuracy,
        tip: `${issueText} 做法：让关键词更突出，连接词更轻一些。`,
    };

    return {
        best_sentence: bestSentence,
        problem_sentences: [worstSentence],
    };
}

export default function ReportBuilder() {
    const REQUEST_TIMEOUT_MS = 15000;
    const [selectedReportId, setSelectedReportId] = useState<string | null>(null);
    const [reportData, setReportData] = useState<ReportData | null>(null);
    const [reports, setReports] = useState<Array<{ id: string; student_name: string; score: number }>>([]);
    const [selectedModules, setSelectedModules] = useState<string[]>(
        AVAILABLE_MODULES.filter(m => m.isDefault).map(m => m.id)
    );
    const [draggedModule, setDraggedModule] = useState<string | null>(null);
    const [isCapturing, setIsCapturing] = useState(false);
    const [captureTemplate, setCaptureTemplate] = useState<CaptureTemplate>(() => readCaptureTemplateFromStorage());
    const [importedTemplate, setImportedTemplate] = useState<ImportedTemplateAssets | null>(null);
    const [scoreViewMode, setScoreViewMode] = useState<'score' | 'grade'>(() => readScoreViewModeFromStorage());
    const [gradeThresholds, setGradeThresholds] = useState<GradeThresholds>(() => readGradeThresholdsFromStorage());

    // 鎵归噺鐢熸垚鐩稿叧鐘舵€?
    const [selectedReportIds, setSelectedReportIds] = useState<Set<string>>(new Set());
    const [batchProgress, setBatchProgress] = useState({ current: 0, total: 0, name: '' });
    const [actionNotice, setActionNotice] = useState<{ type: 'success' | 'error' | 'info'; message: string } | null>(null);
    const [isEditingFeedback, setIsEditingFeedback] = useState(false);
    const [feedbackDraft, setFeedbackDraft] = useState('');
    const [isSavingFeedback, setIsSavingFeedback] = useState(false);
    const [teacherPhrases, setTeacherPhrases] = useState<TeacherPhraseItem[]>([]);
    const [isLoadingTeacherPhrases, setIsLoadingTeacherPhrases] = useState(false);
    const [isAddingTeacherPhrase, setIsAddingTeacherPhrase] = useState(false);
    const [isDeletingTeacherPhraseId, setIsDeletingTeacherPhraseId] = useState('');
    const [newTeacherPhraseText, setNewTeacherPhraseText] = useState('');
    const [newTeacherPhraseCategory, setNewTeacherPhraseCategory] = useState<FeedbackPhraseCategory>('praise');

    const reportRef = useRef<HTMLDivElement>(null);
    const templateZipInputRef = useRef<HTMLInputElement>(null);
    const templateImageInputRef = useRef<HTMLInputElement>(null);
    const templateSingleImageInputRef = useRef<HTMLInputElement>(null);
    const feedbackEditorRef = useRef<HTMLTextAreaElement>(null);
    const win = window as WindowWithFsAccess;
    const fetchWithTimeout = useCallback(async (
        input: RequestInfo | URL,
        init?: RequestInit,
        timeoutMs: number = REQUEST_TIMEOUT_MS
    ): Promise<Response> => {
        const controller = new AbortController();
        const timer = window.setTimeout(() => controller.abort(), timeoutMs);
        try {
            return await fetch(input, {
                ...(init || {}),
                signal: controller.signal,
            });
        } catch (err: unknown) {
            const errorName = err && typeof err === 'object' && 'name' in err
                ? String((err as { name?: unknown }).name || '')
                : '';
            if (errorName === 'AbortError') {
                throw new Error('Request timeout');
            }
            throw err;
        } finally {
            window.clearTimeout(timer);
        }
    }, [REQUEST_TIMEOUT_MS]);

    const lowConfidenceTimeline = useMemo(() => {
        if (!reportData) return false;
        const profile = reportData.engine_raw?.pause_profile;
        if (profile) {
            if (Number(profile.synthetic_timeline || 0) >= 0.5) return true;
            if (Number(profile.low_confidence_timing || 0) >= 0.5) return true;
            if (String(profile.timing_confidence || '').toLowerCase() === 'low') return true;
        }
        return isLowConfidenceTimeline(reportData.alignment?.words || []);
    }, [reportData]);

    const displayWords = useMemo(() => {
        const words = reportData?.alignment?.words || [];
        if (!lowConfidenceTimeline) return words;
        return words.map((w) => {
            if (!w.pause || w.pause.type !== 'missed') return w;
            return { ...w, pause: { ...w.pause, type: 'optional' as const } };
        });
    }, [reportData, lowConfidenceTimeline]);

    const syncScoreDisplaySettings = useCallback(async () => {
        try {
            const controller = new AbortController();
            const timer = window.setTimeout(() => controller.abort(), 6000);
            try {
                const res = await fetch(`${API_HOST}/api/report-display`, { signal: controller.signal });
                if (res.ok) {
                    const data = await res.json();
                    const parsed = parseReportDisplayPayload((data as { report_display?: unknown }).report_display);
                    setScoreViewMode(parsed.mode);
                    setGradeThresholds(parsed.thresholds);
                    try {
                        window.localStorage.setItem(SCORE_VIEW_MODE_STORAGE_KEY, parsed.mode);
                        window.localStorage.setItem(GRADE_THRESHOLDS_STORAGE_KEY, JSON.stringify(parsed.thresholds));
                    } catch {
                        // Storage write is optional; backend config is the source of truth.
                    }
                    return;
                }
            } finally {
                window.clearTimeout(timer);
            }
        } catch {
            // Fallback to local storage if backend is temporarily unavailable.
        }
        setScoreViewMode(readScoreViewModeFromStorage());
        setGradeThresholds(readGradeThresholdsFromStorage());
    }, []);

    useEffect(() => {
        void syncScoreDisplaySettings();
        const handleStorage = (event: StorageEvent) => {
            if (!event.key || event.key === SCORE_VIEW_MODE_STORAGE_KEY || event.key === GRADE_THRESHOLDS_STORAGE_KEY) {
                void syncScoreDisplaySettings();
            }
        };
        const handlePreferencesUpdated = () => {
            void syncScoreDisplaySettings();
        };
        window.addEventListener('storage', handleStorage);
        window.addEventListener('score-report-settings-updated', handlePreferencesUpdated);
        return () => {
            window.removeEventListener('storage', handleStorage);
            window.removeEventListener('score-report-settings-updated', handlePreferencesUpdated);
        };
    }, [syncScoreDisplaySettings]);

    useEffect(() => {
        if (!selectedReportId) return;
        void syncScoreDisplaySettings();
    }, [selectedReportId, syncScoreDisplaySettings]);

    useEffect(() => {
        if (captureTemplate === 'imported' && !importedTemplate) {
            setCaptureTemplate(readCaptureTemplateFromStorage());
            return;
        }
        if (captureTemplate === 'imported') return;
        try {
            window.localStorage.setItem(CAPTURE_TEMPLATE_STORAGE_KEY, captureTemplate);
        } catch {
            // ignore
        }
    }, [captureTemplate, importedTemplate]);

    const readingDisplayWords = useMemo(() => {
        const scriptText = String(reportData?.script_text || '');
        const missingIndices = Array.isArray(reportData?.analysis?.missing_indices) ? reportData.analysis.missing_indices : [];
        return buildScriptAnchoredWords(scriptText, displayWords, missingIndices);
    }, [reportData, displayWords]);

    const completenessDisplayScore = useMemo(() => {
        const score = Number(reportData?.scores?.completeness_100);
        if (Number.isFinite(score)) {
            return score;
        }
        const coverage = Number(reportData?.analysis?.completeness?.coverage);
        return Number.isFinite(coverage) ? coverage : 0;
    }, [reportData]);

    const completenessScriptMap = useMemo(() => {
        const scriptText = String(reportData?.script_text || '');
        const missingIndices = Array.isArray(reportData?.analysis?.missing_indices) ? reportData.analysis.missing_indices : [];
        if (!scriptText.trim()) return [] as Array<{ word: string; missing: boolean }>;

        const scriptTokens = scriptText.match(/[A-Za-z']+/g) || [];

        const missingIndexSet = new Set<number>();
        missingIndices.forEach((raw) => {
            const idx = Number(raw);
            if (Number.isInteger(idx) && idx >= 0 && idx < scriptTokens.length) {
                missingIndexSet.add(idx);
            }
        });

        return scriptTokens.map((word, idx) => ({ word, missing: missingIndexSet.has(idx) }));
    }, [reportData]);

    const feedbackSourceTag = useMemo(() => {
        const rawTag = String(reportData?.engine_raw?.feedback_source_tag || '').trim().toLowerCase();
        if (rawTag === 'db' || rawTag === 'ge' || rawTag === 'az') return rawTag;
        const source = String(reportData?.engine_raw?.source || '').toLowerCase();
        const annotationSource = String(reportData?.engine_raw?.annotation_source || '').toLowerCase();
        if (source.includes('gemini')) return 'ge';
        if (source.includes('azure')) return annotationSource === 'gemini' ? 'ge' : 'az';
        return '';
    }, [reportData]);

    const baseIntegratedFeedbackText = useMemo(
        () => buildIntegratedFeedbackText(reportData?.engine_raw?.integrated_feedback),
        [reportData],
    );

    const activeIntegratedFeedbackText = useMemo(() => {
        const overrideText = String(reportData?.feedback_override?.integrated_feedback_text || '').trim();
        return overrideText || baseIntegratedFeedbackText;
    }, [reportData, baseIntegratedFeedbackText]);

    const feedbackStatusHint = useMemo(
        () => getFeedbackStatusHint({
            feedbackSourceTag,
            feedbackOptimization: reportData?.feedback_optimization,
        }),
        [feedbackSourceTag, reportData?.feedback_optimization],
    );

    const hasFeedbackOverride = useMemo(
        () => String(reportData?.feedback_override?.integrated_feedback_text || '').trim().length > 0,
        [reportData],
    );

    const feedbackOverrideUpdatedAt = useMemo(() => {
        const ts = Number(reportData?.feedback_override?.updated_at || 0);
        return Number.isFinite(ts) && ts > 0 ? ts : 0;
    }, [reportData]);

    const aiFeedbackSuggestions = useMemo<AiFeedbackSuggestion[]>(() => {
        const focusWordRaw = reportData?.analysis?.weak_words?.[0];
        const focusWord = String(focusWordRaw || '').trim();
        const coreWord = focusWord || '目标单词';
        return [
            {
                id: 'ai_praise_fact',
                category: 'praise' as const,
                label: '事实表扬',
                text: '这次朗读语气自然，整体节奏比较稳定。',
            },
            {
                id: 'ai_issue_focus',
                category: 'issue' as const,
                label: '关键问题',
                text: `最核心需要改进的是“${coreWord}”。`,
            },
            {
                id: 'ai_advice_drill',
                category: 'advice' as const,
                label: '针对性建议',
                text: `建议：把“${coreWord}”慢读3遍，再放回原句连读3遍，每次录音回听自检。`,
            },
        ];
    }, [reportData]);

    const teacherPhraseSuggestions = useMemo(() => {
        const seen = new Set<string>(aiFeedbackSuggestions.map((row) => row.text.trim().toLowerCase()));
        return teacherPhrases.filter((row) => {
            const key = row.text.trim().toLowerCase();
            if (!key || seen.has(key)) return false;
            seen.add(key);
            return true;
        });
    }, [aiFeedbackSuggestions, teacherPhrases]);

    const isAzureSource = useMemo(() => {
        const source = String(reportData?.engine_raw?.source || '').toLowerCase();
        return source.includes('azure');
    }, [reportData]);

    const lowFactorWordSet = useMemo(() => {
        const set = new Set<string>();
        const phonemes = reportData?.alignment?.phonemes;
        if (Array.isArray(phonemes)) {
            phonemes.forEach((ph) => {
                if (!ph || typeof ph !== 'object') return;
                const score = Number(ph.score ?? 100);
                if (!Number.isFinite(score) || score >= 50) return;
                const token = normalizeWordToken(String(ph.in_word || ''));
                if (token) set.add(token);
            });
        }
        const mistakes = reportData?.analysis?.mistakes;
        if (!Array.isArray(mistakes)) return set;
        mistakes.forEach((item) => {
            if (!item || typeof item !== 'object') return;
            const type = String((item as { type?: unknown }).type || '').toLowerCase();
            const score = Number((item as { score?: unknown }).score);
            if (type !== 'accuracy' || !Number.isFinite(score) || score >= 50) return;
            const token = normalizeWordToken(String((item as { word?: unknown }).word || (item as { target?: unknown }).target || ''));
            if (token) set.add(token);
        });
        return set;
    }, [reportData]);
    const weakPhonemes = useMemo(() => {
        const raw = reportData?.analysis?.weak_phonemes;
        if (!Array.isArray(raw)) return [];
        const seen = new Set<string>();
        const out: string[] = [];
        for (const item of raw) {
            const token = String(item || '').trim();
            if (!token) continue;
            const key = token.toLowerCase();
            if (seen.has(key)) continue;
            seen.add(key);
            out.push(token);
            if (out.length >= 16) break;
        }
        return out;
    }, [reportData]);
    const loadTeacherPhrases = useCallback(async (silent = true) => {
        setIsLoadingTeacherPhrases(true);
        try {
            const res = await fetchWithTimeout(`${API_HOST}/api/teacher-phrases`, undefined, 10000);
            const payload = await res.json().catch(() => ({}));
            if (!res.ok) {
                const detail = String((payload as { detail?: unknown }).detail || `HTTP ${res.status}`);
                throw new Error(detail);
            }
            const itemsRaw = (payload as { items?: unknown }).items;
            const items = Array.isArray(itemsRaw)
                ? itemsRaw.map(parseTeacherPhraseItem).filter((row): row is TeacherPhraseItem => Boolean(row))
                : [];
            setTeacherPhrases(sortTeacherPhraseItems(items));
        } catch (err) {
            if (!silent) {
                setActionNotice({
                    type: 'error',
                    message: formatActionError('Load teacher phrase bank', err, 'Unknown error'),
                });
            }
            console.error('Failed to load teacher phrase bank:', err);
        } finally {
            setIsLoadingTeacherPhrases(false);
        }
    }, [fetchWithTimeout]);

    useEffect(() => {
        void loadTeacherPhrases(true);
    }, [loadTeacherPhrases]);

    useEffect(() => {
        let cancelled = false;
        const loadReports = async () => {
            try {
                const res = await fetchWithTimeout(`${API_HOST}/api/reports`);
                if (!res.ok) {
                    throw new Error(`HTTP ${res.status}`);
                }
                const data = await res.json();
                if (cancelled) return;
                const rows = Array.isArray(data) ? data : (Array.isArray(data?.items) ? data.items : []);
                setReports(rows as Array<{ id: string; student_name: string; score: number }>);
            } catch (err) {
                if (cancelled) return;
                console.error('Failed to load report list:', err);
                setActionNotice({
                    type: 'error',
                    message: formatActionError('Load report list', err, 'Unknown error'),
                });
            }
        };
        void loadReports();
        return () => {
            cancelled = true;
        };
    }, [fetchWithTimeout]);

    const fetchReportDataById = useCallback(async (reportId: string): Promise<ReportData> => {
        const res = await fetchWithTimeout(`${API_HOST}/api/reports/${reportId}/data`);
        if (!res.ok) {
            throw new Error(`HTTP ${res.status}`);
        }
        return await res.json() as ReportData;
    }, [fetchWithTimeout]);

    const freezeReportFeedback = useCallback(async (reportId: string) => {
        const res = await fetchWithTimeout(
            `${API_HOST}/api/reports/${reportId}/freeze-feedback`,
            { method: 'POST' },
            15000,
        );
        const payload = await res.json().catch(() => ({}));
        if (!res.ok) {
            const detail = String((payload as { detail?: unknown }).detail || `HTTP ${res.status}`);
            throw new Error(detail);
        }
        return payload as {
            feedback_optimization?: ReportData['feedback_optimization'];
            feedback_override?: ReportData['feedback_override'];
        };
    }, [fetchWithTimeout]);

    const freezeBatchFeedback = useCallback(async () => {
        const res = await fetchWithTimeout(
            `${API_HOST}/api/reports/freeze-feedback-batch`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ reason: 'batch_export' }),
            },
            30000,
        );
        const payload = await res.json().catch(() => ({}));
        if (!res.ok) {
            const detail = String((payload as { detail?: unknown }).detail || `HTTP ${res.status}`);
            throw new Error(detail);
        }
        return payload as { frozen_count?: number };
    }, [fetchWithTimeout]);

    useEffect(() => {
        if (!selectedReportId) {
            setReportData(null);
            return;
        }
        let cancelled = false;
        const loadReportData = async () => {
            try {
                const data = await fetchReportDataById(selectedReportId);
                if (cancelled) return;
                setReportData(data);
            } catch (err) {
                if (cancelled) return;
                console.error(`Failed to load report data for ${selectedReportId}:`, err);
                setReportData(null);
                setActionNotice({
                    type: 'error',
                    message: formatActionError('Load selected report', err, 'Unknown error'),
                });
            }
        };
        void loadReportData();
        return () => {
            cancelled = true;
        };
    }, [selectedReportId, fetchReportDataById]);

    useEffect(() => {
        const status = String(reportData?.feedback_optimization?.status || '').trim().toLowerCase();
        const updatedAt = Number(reportData?.feedback_optimization?.updated_at || 0);
        if (!selectedReportId) return;
        if (isCapturing) return;
        if (!(status === 'optimizing' || (status === 'pending' && updatedAt === 0))) return;

        const timer = window.setTimeout(async () => {
            try {
                const data = await fetchReportDataById(selectedReportId);
                setReportData(data);
            } catch (err) {
                console.error(`Failed to refresh feedback optimization for ${selectedReportId}:`, err);
            }
        }, 4000);

        return () => window.clearTimeout(timer);
    }, [
        selectedReportId,
        reportData?.feedback_optimization?.status,
        reportData?.feedback_optimization?.updated_at,
        reportData?.feedback_optimization?.version,
        isCapturing,
        fetchReportDataById,
    ]);

    useEffect(() => {
        setIsEditingFeedback(false);
        setIsSavingFeedback(false);
        setFeedbackDraft(activeIntegratedFeedbackText);
    }, [selectedReportId, activeIntegratedFeedbackText]);

    useEffect(() => {
        if (!actionNotice) return;
        const timer = window.setTimeout(() => setActionNotice(null), 5000);
        return () => window.clearTimeout(timer);
    }, [actionNotice]);

    const handleDragStart = (moduleId: string) => setDraggedModule(moduleId);
    const handleDragOver = (e: React.DragEvent) => e.preventDefault();
    const handleDrop = (e: React.DragEvent) => {
        e.preventDefault();
        if (draggedModule && !selectedModules.includes(draggedModule)) {
            setSelectedModules([...selectedModules, draggedModule]);
        }
        setDraggedModule(null);
    };
    const removeModule = (moduleId: string) => setSelectedModules(selectedModules.filter(id => id !== moduleId));
    const addModule = (moduleId: string) => {
        if (!selectedModules.includes(moduleId)) setSelectedModules([...selectedModules, moduleId]);
    };
    const resetToDefault = () => setSelectedModules(AVAILABLE_MODULES.filter(m => m.isDefault).map(m => m.id));
    const handlePrint = useCallback(() => window.print(), []);

    const handleStartEditFeedback = () => {
        setFeedbackDraft(activeIntegratedFeedbackText);
        setIsEditingFeedback(true);
        if (teacherPhrases.length === 0) {
            void loadTeacherPhrases(true);
        }
        window.requestAnimationFrame(() => {
            feedbackEditorRef.current?.focus();
            const length = feedbackEditorRef.current?.value.length || 0;
            feedbackEditorRef.current?.setSelectionRange(length, length);
        });
    };

    const handleCancelEditFeedback = () => {
        setFeedbackDraft(activeIntegratedFeedbackText);
        setIsEditingFeedback(false);
    };

    const trackTeacherPhraseUsage = useCallback(async (phraseId: string) => {
        const id = String(phraseId || '').trim();
        if (!id) return;
        try {
            const res = await fetchWithTimeout(
                `${API_HOST}/api/teacher-phrases/use`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ phrase_id: id }),
                },
                8000,
            );
            const payload = await res.json().catch(() => ({}));
            if (!res.ok) return;
            const parsed = parseTeacherPhraseItem((payload as { item?: unknown }).item);
            if (!parsed) return;
            setTeacherPhrases((prev) => {
                const next = [...prev];
                const idx = next.findIndex((row) => row.id === parsed.id);
                if (idx >= 0) {
                    next[idx] = parsed;
                    return sortTeacherPhraseItems(next);
                }
                next.push(parsed);
                return sortTeacherPhraseItems(next);
            });
        } catch (err) {
            console.error('Failed to track teacher phrase usage:', err);
        }
    }, [fetchWithTimeout]);

    const handleAddTeacherPhrase = useCallback(async () => {
        const text = String(newTeacherPhraseText || '').trim();
        if (!text) return;
        if (text.length > 220) {
            setActionNotice({ type: 'error', message: '常用语过长（最多 220 字）。' });
            return;
        }
        setIsAddingTeacherPhrase(true);
        setActionNotice(null);
        try {
            const res = await fetchWithTimeout(
                `${API_HOST}/api/teacher-phrases`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        text,
                        category: newTeacherPhraseCategory,
                    }),
                },
                10000,
            );
            const payload = await res.json().catch(() => ({}));
            if (!res.ok) {
                const detail = String((payload as { detail?: unknown }).detail || `HTTP ${res.status}`);
                throw new Error(detail);
            }
            const parsed = parseTeacherPhraseItem((payload as { item?: unknown }).item);
            if (parsed) {
                setTeacherPhrases((prev) => {
                    const next = [...prev];
                    const idx = next.findIndex((row) => row.id === parsed.id);
                    if (idx >= 0) {
                        next[idx] = parsed;
                    } else {
                        next.push(parsed);
                    }
                    return sortTeacherPhraseItems(next);
                });
            }
            setNewTeacherPhraseText('');
            setActionNotice({ type: 'success', message: '已加入教师常用语词库。' });
        } catch (err) {
            console.error('Failed to add teacher phrase:', err);
            setActionNotice({
                type: 'error',
                message: formatActionError('Add teacher phrase', err, 'Unknown error'),
            });
        } finally {
            setIsAddingTeacherPhrase(false);
        }
    }, [fetchWithTimeout, newTeacherPhraseCategory, newTeacherPhraseText]);

    const handleDeleteTeacherPhrase = useCallback(async (phraseId: string) => {
        const id = String(phraseId || '').trim();
        if (!id) return;
        setIsDeletingTeacherPhraseId(id);
        setActionNotice(null);
        try {
            const res = await fetchWithTimeout(
                `${API_HOST}/api/teacher-phrases/${encodeURIComponent(id)}`,
                { method: 'DELETE' },
                10000,
            );
            const payload = await res.json().catch(() => ({}));
            if (!res.ok) {
                const detail = String((payload as { detail?: unknown }).detail || `HTTP ${res.status}`);
                throw new Error(detail);
            }
            setTeacherPhrases((prev) => prev.filter((row) => row.id !== id));
            setActionNotice({ type: 'success', message: '已删除教师自定义短语。' });
        } catch (err) {
            console.error('Failed to delete teacher phrase:', err);
            setActionNotice({
                type: 'error',
                message: formatActionError('Delete teacher phrase', err, 'Unknown error'),
            });
        } finally {
            setIsDeletingTeacherPhraseId('');
        }
    }, [fetchWithTimeout]);

    const insertFeedbackPhrase = (phrase: { id?: string; text: string } | string) => {
        const text = typeof phrase === 'string' ? String(phrase || '').trim() : String(phrase?.text || '').trim();
        const phraseId = typeof phrase === 'string' ? '' : String(phrase?.id || '').trim();
        if (!text) return;
        const textarea = feedbackEditorRef.current;
        if (!textarea) {
            setFeedbackDraft((prev) => `${String(prev || '').trim()}\n${text}`.trim());
            if (phraseId) void trackTeacherPhraseUsage(phraseId);
            return;
        }
        const start = Number.isFinite(textarea.selectionStart) ? textarea.selectionStart : textarea.value.length;
        const end = Number.isFinite(textarea.selectionEnd) ? textarea.selectionEnd : start;
        const current = textarea.value;
        const prefix = current.slice(0, start);
        const suffix = current.slice(end);
        const needsBreak = prefix.length > 0 && !/\s$/.test(prefix);
        const insertion = `${needsBreak ? '\n' : ''}${text}`;
        const nextValue = `${prefix}${insertion}${suffix}`;
        const caret = (prefix + insertion).length;
        setFeedbackDraft(nextValue);
        window.requestAnimationFrame(() => {
            if (!feedbackEditorRef.current) return;
            feedbackEditorRef.current.focus();
            feedbackEditorRef.current.setSelectionRange(caret, caret);
        });
        if (phraseId) void trackTeacherPhraseUsage(phraseId);
    };

    const handleSaveFeedbackOverride = async () => {
        if (!selectedReportId) return;
        const text = String(feedbackDraft || '').trim();
        if (!text) {
            setActionNotice({ type: 'error', message: '反馈内容不能为空。' });
            return;
        }
        setIsSavingFeedback(true);
        setActionNotice(null);
        try {
            const res = await fetchWithTimeout(
                `${API_HOST}/api/reports/${selectedReportId}/feedback-override`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ integrated_feedback_text: text }),
                },
                15000,
            );
            const payload = await res.json().catch(() => ({}));
            if (!res.ok) {
                const detail = String((payload as { detail?: unknown }).detail || `HTTP ${res.status}`);
                throw new Error(detail);
            }
            const override = (payload as { feedback_override?: ReportData['feedback_override'] }).feedback_override || {
                integrated_feedback_text: text,
                updated_at: Math.floor(Date.now() / 1000),
            };
            setReportData((prev) => (prev ? { ...prev, feedback_override: override } : prev));
            setIsEditingFeedback(false);
            setActionNotice({ type: 'success', message: 'Integrated Feedback 已更新。' });
        } catch (err) {
            console.error('Failed to update feedback override:', err);
            setActionNotice({
                type: 'error',
                message: formatActionError('Update feedback', err, 'Unknown error'),
            });
        } finally {
            setIsSavingFeedback(false);
        }
    };

    const handleClearFeedbackOverride = async () => {
        if (!selectedReportId || !hasFeedbackOverride) {
            setIsEditingFeedback(false);
            setFeedbackDraft(baseIntegratedFeedbackText);
            return;
        }
        setIsSavingFeedback(true);
        setActionNotice(null);
        try {
            const res = await fetchWithTimeout(
                `${API_HOST}/api/reports/${selectedReportId}/feedback-override`,
                { method: 'DELETE' },
                15000,
            );
            const payload = await res.json().catch(() => ({}));
            if (!res.ok) {
                const detail = String((payload as { detail?: unknown }).detail || `HTTP ${res.status}`);
                throw new Error(detail);
            }
            setReportData((prev) => (prev ? { ...prev, feedback_override: undefined } : prev));
            setFeedbackDraft(baseIntegratedFeedbackText);
            setIsEditingFeedback(false);
            setActionNotice({ type: 'success', message: '已恢复原始反馈。' });
        } catch (err) {
            console.error('Failed to clear feedback override:', err);
            setActionNotice({
                type: 'error',
                message: formatActionError('Reset feedback', err, 'Unknown error'),
            });
        } finally {
            setIsSavingFeedback(false);
        }
    };

    const handlePickTemplateZip = () => {
        templateZipInputRef.current?.click();
    };

    const handlePickTemplateImages = () => {
        templateImageInputRef.current?.click();
    };

    const handlePickSingleTemplateImage = () => {
        templateSingleImageInputRef.current?.click();
    };

    const handleImportTemplateImages = async (event: ChangeEvent<HTMLInputElement>) => {
        const files = Array.from(event.target.files || []);
        event.target.value = '';
        if (files.length < 3) {
            setActionNotice({ type: 'error', message: '请至少选择 3 张图（top/middle/bottom）。' });
            return;
        }
        const totalSize = files.reduce((sum, file) => sum + file.size, 0);
        if (totalSize > TEMPLATE_ZIP_MAX_MB * 1024 * 1024) {
            setActionNotice({ type: 'error', message: `图片总大小过大，请控制在 ${TEMPLATE_ZIP_MAX_MB}MB 以内。` });
            return;
        }

        const remaining = [...files];
        const takeByKeywords = (keywords: string[]): File | null => {
            const idx = remaining.findIndex((file) => {
                const lower = file.name.toLowerCase();
                return keywords.some((key) => lower.includes(key));
            });
            if (idx < 0) return null;
            return remaining.splice(idx, 1)[0];
        };

        const topFile = takeByKeywords(['top', 'header', 'head']) || remaining.shift() || null;
        const middleFile = takeByKeywords(['middle', 'center', 'body']) || remaining.shift() || null;
        const bottomFile = takeByKeywords(['bottom', 'footer', 'foot']) || remaining.shift() || null;
        if (!topFile || !middleFile || !bottomFile) {
            setActionNotice({ type: 'error', message: '无法识别三段图，请按顺序选择 top/middle/bottom。' });
            return;
        }

        setActionNotice(null);
        setIsCapturing(true);
        try {
            const imported: ImportedTemplateAssets = {
                name: 'Canva三图模板',
                topDataUrl: await fileToDataUrl(topFile),
                middleDataUrl: await fileToDataUrl(middleFile),
                bottomDataUrl: await fileToDataUrl(bottomFile),
                topHeightPx: TEMPLATE_DEFAULT_TOP_HEIGHT_PX,
                bottomHeightPx: TEMPLATE_DEFAULT_BOTTOM_HEIGHT_PX,
                contentPaddingPx: TEMPLATE_DEFAULT_CONTENT_PADDING_PX,
            };
            setImportedTemplate(imported);
            setCaptureTemplate('imported');
            setActionNotice({ type: 'success', message: '已一键导入三图模板。' });
        } catch (err) {
            setActionNotice({ type: 'error', message: formatActionError('Import template images', err, '图片解析失败') });
        } finally {
            setIsCapturing(false);
        }
    };

    const handleAutoSliceTemplateImage = async (event: ChangeEvent<HTMLInputElement>) => {
        const file = event.target.files?.[0];
        event.target.value = '';
        if (!file) return;
        if (!file.type.startsWith('image/')) {
            setActionNotice({ type: 'error', message: '请上传图片文件。' });
            return;
        }
        if (file.size > TEMPLATE_ZIP_MAX_MB * 1024 * 1024) {
            setActionNotice({ type: 'error', message: `图片过大，请控制在 ${TEMPLATE_ZIP_MAX_MB}MB 以内。` });
            return;
        }

        setActionNotice(null);
        setIsCapturing(true);
        try {
            const dataUrl = await fileToDataUrl(file);
            const image = await loadImageFromDataUrl(dataUrl);
            const width = Math.max(1, image.naturalWidth || image.width);
            const height = Math.max(1, image.naturalHeight || image.height);
            if (height < 240 || width < 240) {
                throw new Error('图片尺寸过小，建议至少 800x1200。');
            }

            let topCutPx = Math.round(height * 0.23);
            let bottomCutPx = Math.round(height * 0.84);
            try {
                const cuts = findAutoSliceCuts(image);
                topCutPx = cuts.topCutPx;
                bottomCutPx = cuts.bottomCutPx;
            } catch {
                topCutPx = clamp(topCutPx, 80, Math.round(height * 0.42));
                bottomCutPx = clamp(bottomCutPx, topCutPx + 120, height - 80);
            }

            const topSourceH = topCutPx;
            const middleSourceH = Math.max(40, bottomCutPx - topCutPx);
            const bottomSourceH = Math.max(40, height - bottomCutPx);

            const topDataUrl = cropImageToDataUrl(image, 0, 0, width, topSourceH);
            const middleDataUrl = cropImageToDataUrl(image, 0, topCutPx, width, middleSourceH);
            const bottomDataUrl = cropImageToDataUrl(image, 0, bottomCutPx, width, bottomSourceH);

            const topHeightPx = Math.max(80, Math.round((topSourceH / width) * EXPORT_A4_WIDTH_PX));
            const bottomHeightPx = Math.max(80, Math.round((bottomSourceH / width) * EXPORT_A4_WIDTH_PX));

            const imported: ImportedTemplateAssets = {
                name: `${file.name.replace(/\.[^.]+$/, '') || '单图模板'}-自动切片`,
                topDataUrl,
                middleDataUrl,
                bottomDataUrl,
                topHeightPx,
                bottomHeightPx,
                contentPaddingPx: TEMPLATE_DEFAULT_CONTENT_PADDING_PX,
            };
            setImportedTemplate(imported);
            setCaptureTemplate('imported');
            setActionNotice({ type: 'success', message: '已自动切成三段并应用模板。' });
        } catch (err) {
            setActionNotice({ type: 'error', message: formatActionError('Auto-slice template', err, '自动分切失败') });
        } finally {
            setIsCapturing(false);
        }
    };

    const handleImportTemplateZip = async (event: ChangeEvent<HTMLInputElement>) => {
        const file = event.target.files?.[0];
        event.target.value = '';
        if (!file) return;
        if (!file.name.toLowerCase().endsWith('.zip')) {
            setActionNotice({ type: 'error', message: '模板导入仅支持 .zip 文件。' });
            return;
        }
        if (file.size > TEMPLATE_ZIP_MAX_MB * 1024 * 1024) {
            setActionNotice({ type: 'error', message: `模板包过大，请控制在 ${TEMPLATE_ZIP_MAX_MB}MB 以内。` });
            return;
        }

        setActionNotice(null);
        setIsCapturing(true);
        try {
            const zip = await JSZip.loadAsync(file);
            const manifestEntry = pickZipEntry(zip, ['template.json', 'manifest.json']);
            let manifest: ImportedTemplateManifest = {};
            if (manifestEntry) {
                try {
                    const raw = await manifestEntry.async('string');
                    const parsed = JSON.parse(raw) as ImportedTemplateManifest;
                    if (parsed && typeof parsed === 'object') manifest = parsed;
                } catch {
                    manifest = {};
                }
            }

            const topName = String(manifest.files?.top || 'top.png');
            const middleName = String(manifest.files?.middle || 'middle.png');
            const bottomName = String(manifest.files?.bottom || 'bottom.png');

            const topEntry = pickZipEntry(zip, [topName, 'top.png', 'top.jpg', 'top.jpeg', 'top.webp']);
            const middleEntry = pickZipEntry(zip, [middleName, 'middle.png', 'middle.jpg', 'middle.jpeg', 'middle.webp']);
            const bottomEntry = pickZipEntry(zip, [bottomName, 'bottom.png', 'bottom.jpg', 'bottom.jpeg', 'bottom.webp']);
            if (!topEntry || !middleEntry || !bottomEntry) {
                throw new Error('模板包缺少 top/middle/bottom 三段图片。');
            }

            const imported: ImportedTemplateAssets = {
                name: String(manifest.name || file.name.replace(/\.zip$/i, '')).trim() || '导入模板',
                topDataUrl: await imageDataUrlFromZipEntry(topEntry),
                middleDataUrl: await imageDataUrlFromZipEntry(middleEntry),
                bottomDataUrl: await imageDataUrlFromZipEntry(bottomEntry),
                topHeightPx: Math.max(80, Number(manifest.top_height_px || TEMPLATE_DEFAULT_TOP_HEIGHT_PX)),
                bottomHeightPx: Math.max(80, Number(manifest.bottom_height_px || TEMPLATE_DEFAULT_BOTTOM_HEIGHT_PX)),
                contentPaddingPx: Math.max(24, Number(manifest.content_padding_px || TEMPLATE_DEFAULT_CONTENT_PADDING_PX)),
            };
            setImportedTemplate(imported);
            setCaptureTemplate('imported');
            setActionNotice({ type: 'success', message: `模板导入成功：${imported.name}` });
        } catch (err) {
            setActionNotice({ type: 'error', message: formatActionError('Import template', err, '模板包解析失败') });
        } finally {
            setIsCapturing(false);
        }
    };

    const currentImportedTemplate = captureTemplate === 'imported' ? importedTemplate : null;

    const buildReportImageFileName = useCallback((data: ReportData | null): string => {
        const name = String(data?.meta?.student_name || data?.meta?.student_id || 'student').trim();
        const safe = name.replace(/[\\/:*?"<>|]+/g, '_');
        return `${safe || 'student'}_report.png`;
    }, []);

    const renderReportImageBlob = useCallback(async (): Promise<Blob> => {
        if (!reportRef.current) {
            throw new Error('Report canvas not ready');
        }
        const { toBlob } = await import('html-to-image');
        const templateStyle = getTemplateCaptureStyle(captureTemplate, currentImportedTemplate);
        const widthPx = EXPORT_A4_WIDTH_PX;
        const sourceWidthPx = Math.max(1, reportRef.current.getBoundingClientRect().width);
        const sourceHeightPx = Math.max(1, reportRef.current.scrollHeight);
        const dynamicHeightPx = Math.ceil((sourceHeightPx * widthPx) / sourceWidthPx);
        const heightPx = Math.max(EXPORT_A4_HEIGHT_PX, dynamicHeightPx);
        const blob = await toBlob(reportRef.current, {
            cacheBust: true,
            skipAutoScale: true,
            width: widthPx,
            height: heightPx,
            pixelRatio: EXPORT_PIXEL_RATIO,
            backgroundColor: templateStyle.backgroundColor,
            style: {
                width: `${widthPx}px`,
                minHeight: `${heightPx}px`,
                boxSizing: 'border-box',
                margin: '0',
                transform: 'none',
                ...templateStyle.style,
            },
        });
        if (!blob) {
            throw new Error('Failed to render image blob');
        }
        return blob;
    }, [captureTemplate, currentImportedTemplate]);

    // 鎴浘鍔熻兘 - 浣跨敤鍙﹀瓨涓哄璇濇
    const handleCapture = async () => {
        if (!reportRef.current || !reportData || !selectedReportId) return;
        setActionNotice(null);
        setIsCapturing(true);

        try {
            const freezePayload = await freezeReportFeedback(selectedReportId);
            setReportData((prev) => prev ? ({
                ...prev,
                feedback_optimization: freezePayload.feedback_optimization ?? {
                    ...prev.feedback_optimization,
                    status: 'frozen',
                    freeze_reason: 'single_export',
                },
                feedback_override: freezePayload.feedback_override ?? prev.feedback_override,
            }) : prev);

            const blob = await renderReportImageBlob();
            const fileName = buildReportImageFileName(reportData);

            if (typeof win.showSaveFilePicker === 'function') {
                try {
                    const handle = await win.showSaveFilePicker({
                        suggestedName: fileName,
                        types: [{
                            description: 'PNG Image',
                            accept: { 'image/png': ['.png'] },
                        }],
                    });

                    const writable = await handle.createWritable();
                    await writable.write(blob);
                    await writable.close();
                    return;
                } catch (e: unknown) {
                    const errorName = e && typeof e === 'object' && 'name' in e
                        ? String((e as { name?: unknown }).name || '')
                        : '';
                    if (errorName === 'AbortError') return;
                }
            }

            const link = document.createElement('a');
            link.download = fileName;
            const objectUrl = URL.createObjectURL(blob);
            link.href = objectUrl;
            link.click();
            setTimeout(() => URL.revokeObjectURL(objectUrl), 2000);
        } catch (err) {
            console.error('Capture failed:', err);
            setActionNotice({ type: 'error', message: formatActionError('Capture', err, 'Unknown error') });
        } finally {
            setIsCapturing(false);
        }
    };

    // Batch-generate report images.
    const handleBatchGenerate = async () => {
        if (selectedReportIds.size === 0) {
            setActionNotice({ type: 'info', message: 'Please select reports first.' });
            return;
        }

        setActionNotice(null);
        setIsCapturing(true);
        const ids = Array.from(selectedReportIds);
        setBatchProgress({ current: 0, total: ids.length, name: '' });
        let successCount = 0;
        const errorList: string[] = [];

        let dirHandle: DirectoryHandleLike | null = null;
        const zip = new JSZip();

        try {
            await freezeBatchFeedback();

            if (typeof win.showDirectoryPicker === 'function') {
                try {
                    dirHandle = await win.showDirectoryPicker({ mode: 'readwrite' });
                } catch (e: unknown) {
                    const errorName = e && typeof e === 'object' && 'name' in e
                        ? String((e as { name?: unknown }).name || '')
                        : '';
                    if (errorName === 'AbortError') {
                        setIsCapturing(false);
                        return;
                    }
                    console.warn('Directory permission unavailable, fallback to ZIP mode.');
                }
            }

            for (let i = 0; i < ids.length; i++) {
                const id = ids[i];
                const report = reports.find(r => r.id === id);
                const studentName = report?.student_name || id;
                setBatchProgress({ current: i + 1, total: ids.length, name: studentName });

                try {
                    const data = await fetchReportDataById(id);
                    setReportData(data);

                    await new Promise<void>((resolve) => {
                        requestAnimationFrame(() => {
                            requestAnimationFrame(() => {
                                setTimeout(resolve, 600);
                            });
                        });
                    });

                    if (reportRef.current) {
                        const fileName = buildReportImageFileName(data);
                        const blob = await renderReportImageBlob();

                        if (dirHandle) {
                            const fileHandle = await dirHandle.getFileHandle(fileName, { create: true });
                            const writable = await fileHandle.createWritable();
                            await writable.write(blob);
                            await writable.close();
                            successCount++;
                        } else {
                            zip.file(fileName, blob);
                            successCount++;
                        }

                        await new Promise((resolve) => setTimeout(resolve, 200));
                    }
                } catch (err) {
                    console.error(`Generate report for ${studentName} failed:`, err);
                    errorList.push(studentName);
                }
            }

            if (!dirHandle && successCount > 0) {
                setBatchProgress((prev) => ({ ...prev, name: 'Packing ZIP...' }));
                const content = await zip.generateAsync({ type: 'blob' });
                const link = document.createElement('a');
                link.href = URL.createObjectURL(content);
                link.download = `reports_batch_${new Date().getTime()}.zip`;
                link.click();
            }

            if (errorList.length === 0) {
                setActionNotice({
                    type: 'success',
                    message: `Generated ${successCount} report image(s).${dirHandle ? ' Saved to selected folder.' : ' ZIP download started.'}`,
                });
            } else {
                setActionNotice({
                    type: 'info',
                    message: `Done with partial failures: success ${successCount}, failed ${errorList.length}. Failed: ${errorList.join(', ')}`,
                });
            }
        } catch (err) {
            console.error('Batch generation failed:', err);
            setActionNotice({ type: 'error', message: formatActionError('Batch generation', err, 'Unknown error') });
        } finally {
            setIsCapturing(false);
            setBatchProgress({ current: 0, total: 0, name: '' });
        }
    };
    const toggleSelectAll = () => {
        if (selectedReportIds.size === reports.length) {
            setSelectedReportIds(new Set());
        } else {
            setSelectedReportIds(new Set(reports.map(r => r.id)));
        }
    };

    // 鍒囨崲鍗曚釜閫夋嫨
    const toggleReportSelection = (id: string) => {
        const newSet = new Set(selectedReportIds);
        if (newSet.has(id)) {
            newSet.delete(id);
        } else {
            newSet.add(id);
        }
        setSelectedReportIds(newSet);
    };

    const getDisplayTag = (word: ReportData['alignment']['words'][number]): string => {
        const tag = String(word?.tag || '').toLowerCase();
        const score = Number(word?.score ?? 100);
        const token = normalizeWordToken(String(word?.word || ''));
        if (isAzureSource && tag === 'ok' && Number.isFinite(score) && score < 50) {
            return 'weak';
        }
        if (isAzureSource && tag === 'ok' && token && lowFactorWordSet.has(token)) {
            return 'weak';
        }
        return tag;
    };

    const getTagColor = (tag: string) => {
        switch (tag) {
            case 'ok': return '#22C55E';
            case 'weak': return '#F59E0B';
            case 'poor': case 'missing': return '#EF4444';
            default: return '#1F2937';
        }
    };

    return (
        <div className="min-h-screen bg-[#0a0a0a] pt-20">
            <div className="max-w-7xl mx-auto px-4 py-8 flex gap-6">
                {/* 宸︿晶妯″潡閫夋嫨鍣?*/}
                <div className="w-72 shrink-0 print:hidden">
                    <div className="bg-[#1e1e24] border border-white/10 rounded-2xl p-5 sticky top-24">
                        <h2 className="text-lg font-bold text-white mb-4 flex items-center gap-2">
                            <span className="text-2xl">🧩</span> 可用模块
                        </h2>
                        <p className="text-xs text-gray-500 mb-4">点击 + 或拖拽添加模块</p>

                        <div className="space-y-2">
                            {AVAILABLE_MODULES.map(module => {
                                const isAdded = selectedModules.includes(module.id);
                                return (
                                    <div
                                        key={module.id}
                                        draggable={!isAdded}
                                        onDragStart={() => handleDragStart(module.id)}
                                        className={`p-3 rounded-xl border transition-all cursor-grab active:cursor-grabbing
                                            ${isAdded ? 'bg-primary/10 border-primary/30 opacity-60' : 'bg-white/5 border-white/10 hover:bg-white/10'}`}
                                    >
                                        <div className="flex items-center gap-3">
                                            <GripVertical className="w-4 h-4 text-gray-500" />
                                            <span className="text-xl">{module.icon}</span>
                                            <div className="flex-1 min-w-0">
                                                <div className="text-sm font-medium text-white truncate">{module.name}</div>
                                                <div className="text-xs text-gray-500 truncate">{module.description}</div>
                                            </div>
                                            {isAdded ? (
                                                <Check className="w-4 h-4 text-primary" />
                                            ) : (
                                                <button onClick={() => addModule(module.id)} className="p-1 hover:bg-white/10 rounded">
                                                    <Plus className="w-4 h-4 text-gray-400" />
                                                </button>
                                            )}
                                        </div>
                                    </div>
                                );
                            })}
                        </div>

                        <div className="mt-4 pt-4 border-t border-white/10">
                            <button onClick={resetToDefault} className="w-full py-2 text-sm text-gray-400 hover:text-white transition-colors">
                                重置为默认
                            </button>
                        </div>
                    </div>
                </div>

                {/* 鍙充晶鎶ュ憡棰勮 */}
                <div className="flex-1">
                    {/* 宸ュ叿鏍?*/}
                    <div className="flex flex-col gap-4 mb-6 print:hidden">
                        {actionNotice && (
                            <div className={`rounded-xl border px-3 py-2 text-sm ${
                                actionNotice.type === 'success'
                                    ? 'border-emerald-400/30 bg-emerald-500/10 text-emerald-200'
                                    : actionNotice.type === 'info'
                                        ? 'border-cyan-400/30 bg-cyan-500/10 text-cyan-200'
                                        : 'border-red-400/30 bg-red-500/10 text-red-200'
                            }`}>
                                {actionNotice.message}
                            </div>
                        )}
                        <div className="flex items-center justify-between">
                            <div className="flex items-center gap-4">
                                <h1 className="text-2xl font-bold text-white flex items-center gap-3">
                                    <span className="text-3xl">🧾</span> 报告生成器
                                </h1>
                                <select
                                    value={selectedReportId || ''}
                                    onChange={(e) => setSelectedReportId(e.target.value || null)}
                                    className="bg-[#1e1e24] border border-white/10 rounded-lg px-4 py-2 text-sm text-white focus:outline-none focus:border-primary/50"
                                >
                                    <option value="">选择学生报告...</option>
                                    {reports.map(r => (
                                        <option key={r.id} value={r.id}>
                                            {r.student_name} ({Math.round(r.score)}分)
                                        </option>
                                    ))}
                                </select>
                                <div className="flex items-center gap-2">
                                    <span className="text-xs text-gray-400">图片模板</span>
                                    <select
                                        value={captureTemplate}
                                        onChange={(e) => {
                                            const next = e.target.value as CaptureTemplate;
                                            if (next === 'imported' && !importedTemplate) return;
                                            setCaptureTemplate(next);
                                        }}
                                        className="bg-[#1e1e24] border border-white/10 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-primary/50"
                                    >
                                        {(Object.keys(CAPTURE_TEMPLATE_LABEL) as CaptureTemplate[]).map((key) => (
                                            <option key={key} value={key} disabled={key === 'imported' && !importedTemplate}>
                                                {CAPTURE_TEMPLATE_LABEL[key]}
                                            </option>
                                        ))}
                                    </select>
                                    <button
                                        onClick={handlePickTemplateImages}
                                        type="button"
                                        className="px-3 py-2 rounded-lg text-xs font-semibold border border-cyan-400/30 bg-cyan-500/10 text-cyan-200 hover:bg-cyan-500/20"
                                        title="一键导入 Canva 三图（top/middle/bottom）"
                                    >
                                        一键导入三图
                                    </button>
                                    <button
                                        onClick={handlePickSingleTemplateImage}
                                        type="button"
                                        className="px-3 py-2 rounded-lg text-xs font-semibold border border-emerald-300/25 bg-emerald-500/8 text-emerald-100 hover:bg-emerald-500/16"
                                        title="上传一张背景图，自动切成 top/middle/bottom"
                                    >
                                        单图自动切
                                    </button>
                                    <button
                                        onClick={handlePickTemplateZip}
                                        type="button"
                                        className="px-3 py-2 rounded-lg text-xs font-semibold border border-white/15 bg-white/5 text-slate-200 hover:bg-white/10"
                                        title="导入 Canva 模板包（zip，含 top/middle/bottom）"
                                    >
                                        导入模板包
                                    </button>
                                    <input
                                        ref={templateImageInputRef}
                                        type="file"
                                        accept="image/png,image/jpeg,image/webp"
                                        multiple
                                        onChange={handleImportTemplateImages}
                                        className="hidden"
                                    />
                                    <input
                                        ref={templateSingleImageInputRef}
                                        type="file"
                                        accept="image/png,image/jpeg,image/webp"
                                        onChange={handleAutoSliceTemplateImage}
                                        className="hidden"
                                    />
                                    <input
                                        ref={templateZipInputRef}
                                        type="file"
                                        accept=".zip,application/zip"
                                        onChange={handleImportTemplateZip}
                                        className="hidden"
                                    />
                                </div>
                            </div>

                            <div className="flex gap-2">
                                <button
                                    onClick={handleCapture}
                                    disabled={!reportData || isCapturing}
                                    className="bg-blue-500 text-white px-5 py-2.5 rounded-xl font-bold flex items-center gap-2 hover:bg-blue-600 disabled:opacity-50 transition-all"
                                >
                                    {isCapturing && batchProgress.total === 0 ? <Download className="w-4 h-4 animate-spin" /> : <Camera className="w-4 h-4" />}
                                    一键导出
                                </button>
                                <button
                                    onClick={handlePrint}
                                    disabled={!reportData}
                                    className="bg-primary text-black px-5 py-2.5 rounded-xl font-bold flex items-center gap-2 hover:bg-primary/90 disabled:opacity-50 transition-all"
                                >
                                    <Printer className="w-4 h-4" />
                                    打印报告
                                </button>
                            </div>
                        </div>

                        {/* 鎵归噺鎿嶄綔鍖?*/}
                        <div className="bg-[#1e1e24] border border-white/10 rounded-xl p-4">
                            <div className="flex items-center gap-4">
                                {/* 涓嬫媺閫夋嫨鍣?*/}
                                <div className="relative flex-1">
                                    <div className="flex items-center gap-2">
                                        <button
                                            onClick={toggleSelectAll}
                                            className="flex items-center gap-2 px-3 py-2 rounded-lg bg-white/5 border border-white/10 hover:border-white/30 transition-colors"
                                        >
                                            <div className={`w-4 h-4 border rounded flex items-center justify-center ${selectedReportIds.size === reports.length && reports.length > 0 ? 'bg-primary border-primary' : 'border-gray-500'}`}>
                                                {selectedReportIds.size === reports.length && reports.length > 0 && <Check className="w-3 h-3 text-black" />}
                                            </div>
                                            <span className="text-sm text-gray-300">全选</span>
                                        </button>

                                        <span className="text-sm text-gray-500">已选 {selectedReportIds.size}/{reports.length}</span>
                                    </div>

                                    {/* 鍕鹃€夊垪琛?*/}
                                    <div className="mt-3 max-h-32 overflow-y-auto scrollbar-thin scrollbar-track-gray-800 scrollbar-thumb-gray-600">
                                        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-2">
                                            {reports.map(r => (
                                                <label
                                                    key={r.id}
                                                    className={`flex items-center gap-2 px-3 py-2 rounded-lg cursor-pointer transition-all ${selectedReportIds.has(r.id)
                                                        ? 'bg-primary/20 border border-primary'
                                                        : 'bg-white/5 border border-transparent hover:bg-white/10'
                                                        }`}
                                                >
                                                    <input
                                                        type="checkbox"
                                                        checked={selectedReportIds.has(r.id)}
                                                        onChange={() => toggleReportSelection(r.id)}
                                                        className="w-4 h-4 rounded border-gray-500 text-primary focus:ring-primary focus:ring-offset-0 bg-transparent"
                                                    />
                                                    <span className={`text-sm truncate ${selectedReportIds.has(r.id) ? 'text-primary' : 'text-gray-300'}`}>
                                                        {r.student_name}
                                                    </span>
                                                </label>
                                            ))}
                                        </div>
                                    </div>
                                </div>

                                {/* 鎵归噺鐢熸垚鎸夐挳 */}
                                <button
                                    onClick={handleBatchGenerate}
                                    disabled={selectedReportIds.size === 0 || isCapturing}
                                    className="bg-gradient-to-r from-purple-500 to-pink-500 text-white px-6 py-3 rounded-xl font-bold flex items-center gap-2 hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed transition-all whitespace-nowrap"
                                >
                                    {isCapturing && batchProgress.total > 0 ? (
                                        <>
                                            <Download className="w-4 h-4 animate-spin" />
                                            {batchProgress.current}/{batchProgress.total}
                                        </>
                                    ) : (
                                        <>
                                            <Camera className="w-4 h-4" />
                                            批量生成 ({selectedReportIds.size})
                                        </>
                                    )}
                                </button>
                            </div>

                            {/* 杩涘害鏉?*/}
                            {batchProgress.total > 0 && (
                                <div className="mt-3">
                                    <div className="flex justify-between text-xs text-gray-400 mb-1">
                                        <span>正在生成: {batchProgress.name}</span>
                                        <span>{batchProgress.current}/{batchProgress.total}</span>
                                    </div>
                                    <div className="h-2 bg-gray-700 rounded-full overflow-hidden">
                                        <div
                                            className="h-full bg-gradient-to-r from-purple-500 to-pink-500 transition-all duration-300"
                                            style={{ width: `${(batchProgress.current / batchProgress.total) * 100}%` }}
                                        />
                                    </div>
                                </div>
                            )}
                        </div>
                    </div>

                    {/* A4 棰勮鍖?*/}
                    <div
                        ref={reportRef}
                        onDragOver={handleDragOver}
                        onDrop={handleDrop}
                        className={`report-print-root capture-template-${captureTemplate} rounded-lg shadow-2xl mx-auto print:shadow-none print:rounded-none
                            ${draggedModule ? 'ring-2 ring-primary ring-dashed' : ''}`}
                        style={{
                            width: '210mm',
                            minHeight: '297mm',
                            padding: currentImportedTemplate ? '0' : '12mm',
                        }}
                    >
                        {currentImportedTemplate && (
                            <div className="report-template-bg" aria-hidden>
                                <div
                                    style={{
                                        height: `${currentImportedTemplate.topHeightPx}px`,
                                        backgroundImage: `url(${currentImportedTemplate.topDataUrl})`,
                                        backgroundRepeat: 'no-repeat',
                                        backgroundSize: '100% 100%',
                                        backgroundPosition: 'top center',
                                    }}
                                />
                                <div
                                    style={{
                                        flex: 1,
                                        minHeight: '20px',
                                        backgroundImage: `url(${currentImportedTemplate.middleDataUrl})`,
                                        backgroundRepeat: 'repeat-y',
                                        backgroundSize: '100% auto',
                                        backgroundPosition: 'top center',
                                    }}
                                />
                                <div
                                    style={{
                                        height: `${currentImportedTemplate.bottomHeightPx}px`,
                                        backgroundImage: `url(${currentImportedTemplate.bottomDataUrl})`,
                                        backgroundRepeat: 'no-repeat',
                                        backgroundSize: '100% 100%',
                                        backgroundPosition: 'bottom center',
                                    }}
                                />
                            </div>
                        )}

                        <div
                            className={`report-content-surface ${captureTemplate === 'classic' ? '' : 'report-content-surface-themed'} ${currentImportedTemplate ? 'report-content-surface-imported' : ''}`}
                            style={currentImportedTemplate
                                ? { padding: `${currentImportedTemplate.contentPaddingPx}px` }
                                : undefined}
                        >
                            {!reportData ? (
                                <div className="h-full flex flex-col items-center justify-center text-gray-400 py-32">
                                    <Eye className="w-16 h-16 mb-4 opacity-30" />
                                    <p className="text-lg font-medium">请选择一个学生报告</p>
                                    <p className="text-sm text-gray-500 mt-1">拖拽左侧模块自定义报告内容</p>
                                </div>
                            ) : (
                                <div className="text-gray-800 space-y-4">
                                {/* 鎶ュ憡澶撮儴 */}
                                <div className="report-header text-center pb-3 border-b-2 border-gray-200">
                                    <h1 className="text-xl font-bold text-gray-900">英语朗读评测报告</h1>
                                    <div className="flex justify-center gap-8 mt-1 text-sm text-gray-600">
                                        <span>学生: <strong>{reportData.meta.student_name || reportData.meta.student_id}</strong></span>
                                        <span>日期: {new Date(reportData.meta.timestamp).toLocaleDateString()}</span>
                                    </div>
                                </div>

                                {/* 妯″潡娓叉煋 */}
                                {selectedModules.map(moduleId => (
                                    <div
                                        key={moduleId}
                                        className={`relative group print:break-inside-avoid module-block module-${moduleId}`}
                                    >
                                        <button
                                            onClick={() => removeModule(moduleId)}
                                            className="absolute -right-2 -top-2 w-6 h-6 bg-red-500 text-white rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity print:hidden z-10"
                                        >
                                            <X className="w-3 h-3" />
                                        </button>

                                        {/* 鎬诲垎姒傝 */}
                                        {moduleId === 'score_overview' && (
                                            <div className="border border-gray-200 rounded-lg p-4">
                                                <div className="flex items-center justify-between mb-3">
                                                    <h3 className="text-base font-bold">📊 总分概览</h3>
                                                </div>
                                                <div className="flex items-center gap-6">
                                                    <div className="relative w-20 h-20">
                                                        <svg className="w-full h-full transform -rotate-90">
                                                            <circle cx="40" cy="40" r="35" stroke="#E5E7EB" strokeWidth="6" fill="none" />
                                                            <circle cx="40" cy="40" r="35"
                                                                stroke={getGradeInfo(reportData.scores.overall_100, gradeThresholds).color}
                                                                strokeWidth="6" fill="none"
                                                                strokeDasharray={2 * Math.PI * 35}
                                                                strokeDashoffset={2 * Math.PI * 35 * (1 - reportData.scores.overall_100 / 100)}
                                                                strokeLinecap="round"
                                                            />
                                                        </svg>
                                                        <div className="absolute inset-0 flex items-center justify-center">
                                                            <span
                                                                className={`${scoreViewMode === 'grade' ? 'text-lg' : 'text-xl'} font-bold`}
                                                                style={{ color: getGradeInfo(reportData.scores.overall_100, gradeThresholds).color }}
                                                            >
                                                                {scoreViewMode === 'grade'
                                                                    ? getGradeInfo(reportData.scores.overall_100, gradeThresholds).label
                                                                    : Math.round(reportData.scores.overall_100)}
                                                            </span>
                                                        </div>
                                                    </div>
                                                    <div className="grid grid-cols-2 gap-2 flex-1">
                                                        {[
                                                            { label: '发音', score: reportData.scores.pronunciation_100, color: '#3B82F6' },
                                                            { label: '语调', score: reportData.scores.intonation_100, color: '#22C55E' },
                                                            { label: 'Fluency', score: reportData.scores.fluency_100, color: '#F59E0B' },
                                                            { label: 'Completeness', score: completenessDisplayScore, color: '#A855F7' },
                                                        ].map(item => (
                                                            <div key={item.label} className="p-2 border rounded text-center" style={{ borderColor: item.color + '40' }}>
                                                                <div
                                                                    className="text-lg font-bold"
                                                                    style={{ color: scoreViewMode === 'grade' ? getGradeInfo(item.score, gradeThresholds).color : item.color }}
                                                                >
                                                                    {scoreViewMode === 'grade' ? getGradeInfo(item.score, gradeThresholds).label : Math.round(item.score)}
                                                                </div>
                                                                <div className="text-xs text-gray-500">{item.label}</div>
                                                            </div>
                                                        ))}
                                                    </div>
                                                </div>
                                            </div>
                                        )}

                                        {/* 鏈楄瀵圭収 */}
                                        {moduleId === 'text_highlight' && (
                                            <div className="border border-gray-200 rounded-lg p-4">
                                                <h3 className="text-base font-bold mb-2">📉 朗读对照</h3>
                                                <div className="flex flex-wrap gap-1 leading-loose text-sm">
                                                    {readingDisplayWords.map((word, idx) => (
                                                        <span
                                                            key={idx}
                                                            style={{ color: isMergedPunctuationToken(word.word) ? '#1F2937' : getTagColor(getDisplayTag(word)) }}
                                                            className="font-medium"
                                                        >
                                                            {normalizeDisplayWord(word.word)}
                                                        </span>
                                                    ))}
                                                </div>
                                                <div className="flex gap-4 mt-2 text-xs">
                                                    <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-green-500"></span> 正确</span>
                                                    <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-yellow-500"></span> 待加强</span>
                                                    <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-red-500"></span> 需改进</span>
                                                </div>
                                            </div>
                                        )}

                                        {/* 鏍稿績鍙戦煶璇婃柇 */}
                                        {moduleId === 'pronunciation_diagnosis' && (
                                            <div className="border border-gray-200 rounded-lg p-4">
                                                <h3 className="text-base font-bold mb-2">🎯 核心发音诊断</h3>
                                                {reportData.analysis.weak_words?.length > 0 ? (
                                                    <div className="space-y-2">
                                                        <div className="text-sm text-gray-600">需重点练习的单词:</div>
                                                        <div className="flex flex-wrap gap-2">
                                                            {reportData.analysis.weak_words.slice(0, 8).map((word, idx) => (
                                                                <span key={idx} className="bg-red-100 text-red-700 px-2 py-0.5 rounded text-sm font-medium">
                                                                    {word}
                                                                </span>
                                                            ))}
                                                        </div>
                                                        {weakPhonemes.length > 0 && (
                                                            <div className="mt-2">
                                                                <div className="text-sm text-gray-600">弱读音素:</div>
                                                                <div className="mt-1 space-y-1.5">
                                                                    {weakPhonemes.map((ph, idx) => {
                                                                        return (
                                                                            <div key={`${String(ph || '').trim()}-${idx}`} className="rounded-md border border-yellow-200 bg-yellow-50 px-2 py-1">
                                                                                <div className="flex flex-wrap items-center gap-2">
                                                                                    <span className="bg-yellow-100 text-yellow-700 px-2 py-0.5 rounded text-sm">
                                                                                        /{ph}/
                                                                                    </span>
                                                                                </div>
                                                                            </div>
                                                                        );
                                                                    })}
                                                                </div>
                                                            </div>
                                                        )}
                                                    </div>
                                                ) : (
                                                    <div className="text-green-600 text-sm">发音整体良好。</div>
                                                )}
                                            </div>
                                        )}

                                        {/* 缁煎悎鍙嶉 */}
                                        {moduleId === 'ai_feedback' && reportData.engine_raw?.integrated_feedback && (
                                            <div className="border border-gray-200 rounded-lg p-4">
                                                <div className="mb-2 flex flex-wrap items-start justify-between gap-2">
                                                    <h3 className="text-base font-bold flex items-center gap-2">
                                                        <span>👩‍🏫 综合反馈</span>
                                                        {feedbackSourceTag && (
                                                            <span className="text-[11px] font-semibold px-2 py-0.5 rounded border border-gray-300 text-gray-600 uppercase">
                                                                {feedbackSourceTag}
                                                            </span>
                                                        )}
                                                        {hasFeedbackOverride && (
                                                            <span className="text-[11px] font-semibold px-2 py-0.5 rounded border border-emerald-200 text-emerald-700 bg-emerald-50">
                                                                edited
                                                            </span>
                                                        )}
                                                    </h3>
                                                    <div className="flex flex-wrap items-center gap-2">
                                                        {!isEditingFeedback ? (
                                                            <button
                                                                type="button"
                                                                onClick={handleStartEditFeedback}
                                                                className="px-2.5 py-1 rounded-md border border-gray-300 text-xs font-semibold text-gray-700 hover:bg-gray-50"
                                                            >
                                                                编辑
                                                            </button>
                                                        ) : (
                                                            <>
                                                                <button
                                                                    type="button"
                                                                    onClick={handleClearFeedbackOverride}
                                                                    disabled={isSavingFeedback || !hasFeedbackOverride}
                                                                    className="px-2.5 py-1 rounded-md border border-gray-300 text-xs font-semibold text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                                                                >
                                                                    恢复原始
                                                                </button>
                                                                <button
                                                                    type="button"
                                                                    onClick={handleCancelEditFeedback}
                                                                    disabled={isSavingFeedback}
                                                                    className="px-2.5 py-1 rounded-md border border-gray-300 text-xs font-semibold text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                                                                >
                                                                    取消
                                                                </button>
                                                                <button
                                                                    type="button"
                                                                    onClick={handleSaveFeedbackOverride}
                                                                    disabled={isSavingFeedback || !feedbackDraft.trim()}
                                                                    className="px-3 py-1 rounded-md bg-blue-600 text-xs font-semibold text-white hover:bg-blue-700 disabled:opacity-50"
                                                                >
                                                                    {isSavingFeedback ? '更新中...' : '完成更新'}
                                                                </button>
                                                            </>
                                                        )}
                                                    </div>
                                                </div>
                                                {feedbackStatusHint && (
                                                    <div
                                                        className={`mb-2 text-[11px] ${
                                                            feedbackStatusHint.tone === 'warning'
                                                                ? 'text-amber-700'
                                                                : 'text-gray-500'
                                                        }`}
                                                    >
                                                        {feedbackStatusHint.text}
                                                    </div>
                                                )}
                                                {hasFeedbackOverride && feedbackOverrideUpdatedAt > 0 && (
                                                    <div className="mb-2 text-[11px] text-emerald-700">
                                                        已更新：{new Date(feedbackOverrideUpdatedAt * 1000).toLocaleString()}
                                                    </div>
                                                )}
                                                {!isEditingFeedback ? (
                                                    <div className="text-sm text-gray-700 whitespace-pre-line">
                                                        {activeIntegratedFeedbackText || '暂无反馈。'}
                                                    </div>
                                                ) : (
                                                    <div className="space-y-2">
                                                        <textarea
                                                            ref={feedbackEditorRef}
                                                            value={feedbackDraft}
                                                            onChange={(e) => setFeedbackDraft(e.target.value)}
                                                            className="w-full min-h-[130px] rounded-lg border border-gray-300 px-3 py-2 text-sm leading-relaxed focus:outline-none focus:ring-2 focus:ring-blue-300"
                                                            placeholder="编辑综合反馈..."
                                                        />
                                                        <div className="text-[11px] text-gray-500">
                                                            绿=表扬，红=问题，橙=建议，蓝=鼓励
                                                        </div>
                                                        <div className="space-y-2">
                                                            <div className="text-xs font-semibold text-gray-700">AI预选建议</div>
                                                            <div className="flex flex-wrap gap-2">
                                                                {aiFeedbackSuggestions.map((item) => (
                                                                    <button
                                                                        key={`ai_${item.id}`}
                                                                        type="button"
                                                                        onClick={() => insertFeedbackPhrase(item)}
                                                                        className={`px-2.5 py-1 rounded-full border text-xs font-medium ${FEEDBACK_PHRASE_CHIP_CLASS[item.category]}`}
                                                                        title={item.text}
                                                                    >
                                                                        <span className="font-semibold mr-1">[{FEEDBACK_PHRASE_CATEGORY_LABEL[item.category]}]</span>
                                                                        {item.label}
                                                                    </button>
                                                                ))}
                                                            </div>
                                                            <div className="text-[11px] text-gray-500">点击标签后会插入对应完整句子。</div>
                                                        </div>
                                                        <div className="space-y-2">
                                                            <div className="text-xs font-semibold text-gray-700">教师词库建议</div>
                                                            {teacherPhraseSuggestions.length > 0 ? (
                                                                <div className="flex flex-wrap gap-2">
                                                                    {teacherPhraseSuggestions.map((item) => (
                                                                        <div
                                                                            key={`bank_${item.id}`}
                                                                            className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs font-medium ${FEEDBACK_PHRASE_CHIP_CLASS[item.category]}`}
                                                                            title={item.text}
                                                                        >
                                                                            <button
                                                                                type="button"
                                                                                onClick={() => insertFeedbackPhrase(item)}
                                                                                className="min-w-0 text-left"
                                                                            >
                                                                                <span className="font-semibold mr-1">[{FEEDBACK_PHRASE_CATEGORY_LABEL[item.category]}]</span>
                                                                                {item.text}
                                                                            </button>
                                                                            {!item.builtin && (
                                                                                <button
                                                                                    type="button"
                                                                                    onClick={(e) => {
                                                                                        e.preventDefault();
                                                                                        e.stopPropagation();
                                                                                        void handleDeleteTeacherPhrase(item.id);
                                                                                    }}
                                                                                    disabled={isDeletingTeacherPhraseId === item.id}
                                                                                    className="rounded-full border border-black/10 px-1.5 py-0.5 text-[10px] font-semibold text-gray-600 hover:bg-white/70 disabled:opacity-50"
                                                                                    title="删除该教师短语"
                                                                                >
                                                                                    {isDeletingTeacherPhraseId === item.id ? '...' : '删'}
                                                                                </button>
                                                                            )}
                                                                        </div>
                                                                    ))}
                                                                </div>
                                                            ) : (
                                                                <div className="text-[11px] text-gray-500">词库暂无独立建议，可在下方新增。</div>
                                                            )}
                                                        </div>
                                                        <div className="flex flex-wrap items-center gap-2">
                                                            <select
                                                                value={newTeacherPhraseCategory}
                                                                onChange={(e) => setNewTeacherPhraseCategory(normalizeFeedbackPhraseCategory(e.target.value))}
                                                                className="h-8 rounded-md border border-gray-300 px-2 text-xs text-gray-700 bg-white"
                                                            >
                                                                <option value="praise">表扬</option>
                                                                <option value="issue">问题</option>
                                                                <option value="advice">建议</option>
                                                                <option value="encourage">鼓励</option>
                                                            </select>
                                                            <input
                                                                type="text"
                                                                value={newTeacherPhraseText}
                                                                onChange={(e) => setNewTeacherPhraseText(e.target.value)}
                                                                onKeyDown={(e) => {
                                                                    if (e.key === 'Enter') {
                                                                        e.preventDefault();
                                                                        if (!isAddingTeacherPhrase) void handleAddTeacherPhrase();
                                                                    }
                                                                }}
                                                                placeholder="新增常用语，回车或点“加入词库”"
                                                                className="h-8 min-w-[260px] flex-1 rounded-md border border-gray-300 px-2 text-xs text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-200"
                                                            />
                                                            <button
                                                                type="button"
                                                                onClick={() => void handleAddTeacherPhrase()}
                                                                disabled={isAddingTeacherPhrase || !newTeacherPhraseText.trim()}
                                                                className="h-8 px-3 rounded-md border border-gray-300 text-xs font-semibold text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                                                            >
                                                                {isAddingTeacherPhrase ? '加入中...' : '加入词库'}
                                                            </button>
                                                            {isLoadingTeacherPhrases && (
                                                                <span className="text-[11px] text-gray-500">词库加载中...</span>
                                                            )}
                                                        </div>
                                                        <div className="text-xs text-gray-500">
                                                            点击短语可插入到光标位置，然后点“完成更新”保存。
                                                        </div>
                                                    </div>
                                                )}
                                            </div>
                                        )}

                                        {/* 娴佸埄搴﹀垎鏋?*/}
                                        {moduleId === 'fluency_analysis' && (() => {
                                            // 璁＄畻鍋滈】缁熻
                                            const pauseWords = displayWords.filter(w => w.pause);
                                            const badPauses = pauseWords.filter(w => w.pause?.type === 'bad');
                                            const goodPauses = pauseWords.filter(w => w.pause?.type === 'good');
                                            const totalPauseDuration = pauseWords.reduce((s, w) => s + (w.pause?.duration || 0), 0);
                                            const wpm = reportData.engine_raw.wpm || 0;
                                            const fluencyScore = reportData.scores.fluency_100;
                                            const fluencyComponents = reportData.engine_raw?.fluency_components || {};
                                            const pausingSub = Number(fluencyComponents.pausing_score);
                                            const paceSub = Number(fluencyComponents.pace_score);
                                            const hesitationSub = Number(fluencyComponents.hesitation_score);

                                            // 鍋滈】绗﹀彿娓叉煋
                                            const renderPauseMarker = (pause: { type: string; duration: number }) => {
                                                if (pause.type !== 'bad') return null;
                                                return (
                                                    <span className="mx-1 inline-flex items-center rounded border border-red-200 bg-red-50 px-1.5 py-0.5 text-[11px] font-semibold text-red-700">
                                                        Long Pause
                                                    </span>
                                                );
                                            };

                                            return (
                                                <div className="border border-gray-200 rounded-lg p-4">
                                                    {/* 澶撮儴锛氬垎鏁?+ 鍥句緥 */}
                                                    <div className="flex items-center justify-between mb-4">
                                                        <h3 className="text-base font-bold flex items-center gap-2">
                                                            〰️ 流利度分析
                                                            <span className="text-2xl font-bold text-blue-500">{formatScoreCompact(fluencyScore)}%</span>
                                                        </h3>
                                                        <div className="flex gap-4 text-xs text-gray-600">
                                                            <span className="flex items-center gap-1">
                                                                <span className="inline-flex items-center rounded border border-red-200 bg-red-50 px-1.5 py-0.5 text-[11px] font-semibold text-red-700">Long Pause</span>
                                                                <span>停顿过长</span>
                                                            </span>
                                                        </div>
                                                    </div>
                                                    {lowConfidenceTimeline && (
                                                        <div className="mb-3 text-xs text-slate-500">
                                                            时间轴置信度较低，已隐藏 Missing break 标记。
                                                        </div>
                                                    )}

                                                    <div className="grid grid-cols-3 gap-2 mb-3 text-center text-xs">
                                                        <div className="rounded-md border border-slate-200 bg-slate-50 p-2">
                                                            <div className="font-semibold text-slate-800">{Number.isFinite(pausingSub) ? formatScoreCompact(pausingSub) : '--'}</div>
                                                            <div className="text-slate-500">Pausing</div>
                                                        </div>
                                                        <div className="rounded-md border border-slate-200 bg-slate-50 p-2">
                                                            <div className="font-semibold text-slate-800">{Number.isFinite(paceSub) ? formatScoreCompact(paceSub) : '--'}</div>
                                                            <div className="text-slate-500">Pace</div>
                                                        </div>
                                                        <div className="rounded-md border border-slate-200 bg-slate-50 p-2">
                                                            <div className="font-semibold text-slate-800">{Number.isFinite(hesitationSub) ? formatScoreCompact(hesitationSub) : '--'}</div>
                                                            <div className="text-slate-500">Hesitation</div>
                                                        </div>
                                                    </div>

                                                    {/* 鏈楄鏂囨湰 + 鍋滈】鏍囪 */}
                                                    <div className="bg-gray-50 rounded-lg p-4 mb-4 leading-loose text-base">
                                                        {displayWords.map((word, idx) => (
                                                            <span key={idx}>
                                                                <span className="text-gray-800">{normalizeDisplayWord(word.word)} </span>
                                                                {word.pause && renderPauseMarker(word.pause)}
                                                            </span>
                                                        ))}
                                                    </div>

                                                    {/* 鏁版嵁缁熻鏉?*/}
                                                    <div className="grid grid-cols-4 gap-3 text-center">
                                                        <div className="bg-blue-50 rounded-lg py-2">
                                                            <div className="text-lg font-bold text-blue-600">{Math.round(wpm)}</div>
                                                            <div className="text-xs text-gray-500">词/分钟</div>
                                                        </div>
                                                        <div className="bg-green-50 rounded-lg py-2">
                                                            <div className="text-lg font-bold text-green-600">{goodPauses.length}</div>
                                                            <div className="text-xs text-gray-500">合理停顿</div>
                                                        </div>
                                                        <div className="bg-red-50 rounded-lg py-2">
                                                            <div className="text-lg font-bold text-red-600">{badPauses.length}</div>
                                                            <div className="text-xs text-gray-500">不当停顿</div>
                                                        </div>
                                                        <div className="bg-purple-50 rounded-lg py-2">
                                                            <div className="text-lg font-bold text-purple-600">{formatScoreCompact(totalPauseDuration)}s</div>
                                                            <div className="text-xs text-gray-500">总停顿时长</div>
                                                        </div>
                                                    </div>

                                                    {/* 闂璇嶆彁绀?*/}
                                                    {badPauses.length > 0 && (
                                                        <div className="mt-3 p-2 bg-red-50 rounded-lg text-sm text-red-700">
                                                            卡顿位置: {badPauses.slice(0, 5).map(w => `"${normalizeDisplayWord(w.word)}"`).join('、')}
                                                            {badPauses.length > 5 && ` 等${badPauses.length}处`}
                                                        </div>
                                                    )}
                                                </div>
                                            );
                                        })()}

                                        {/* 闊靛緥鍒嗘瀽 - 寮硅烦鐞冨彲瑙嗗寲 */}
                                        {moduleId === 'intonation_analysis' && (() => {
                                            const intonationRaw = reportData.analysis.intonation_analysis;
                                            const hasStructuredIntonation = Boolean(
                                                intonationRaw?.best_sentence || (intonationRaw?.problem_sentences && intonationRaw.problem_sentences.length > 0),
                                            );
                                            const intonation: IntonationAnalysisView | null = hasStructuredIntonation
                                                ? (intonationRaw || null)
                                                : deriveIntonationFallback(displayWords);
                                            const intonationScore = reportData.scores.intonation_100;

                                            // 娓叉煋寮硅烦鐞冨彞瀛?
                                            const renderBouncingBalls = (words: Array<{ word: string; is_stressed: boolean; stress_correct: boolean }>) => (
                                                <div className="flex flex-wrap items-end gap-1 py-2">
                                                    {words.map((w, i) => (
                                                        <div key={i} className="flex flex-col items-center">
                                                            {/* 鐞?*/}
                                                            <div
                                                                className={`rounded-full transition-all ${w.is_stressed
                                                                    ? w.stress_correct
                                                                        ? 'w-4 h-4 bg-green-500 mb-1'
                                                                        : 'w-4 h-4 bg-red-500 mb-1'
                                                                    : 'w-2 h-2 bg-gray-400 mb-2'
                                                                    }`}
                                                                style={{
                                                                    transform: w.is_stressed ? 'translateY(-8px)' : 'translateY(0)',
                                                                }}
                                                            />
                                                            {/* 鍗曡瘝 */}
                                                            <span className={`text-sm ${w.is_stressed
                                                                ? w.stress_correct ? 'font-bold text-green-700' : 'font-bold text-red-600'
                                                                : 'text-gray-600'
                                                                }`}>
                                                                {w.word}
                                                            </span>
                                                        </div>
                                                    ))}
                                                </div>
                                            );

                                            return (
                                                <div className="border border-gray-200 rounded-lg p-4">
                                                    {/* 澶撮儴 */}
                                                    <div className="flex items-center justify-between mb-4">
                                                        <h3 className="text-base font-bold flex items-center gap-2">
                                                            🗣️ 重弱与节奏提示
                                                            <span className="text-2xl font-bold text-purple-500">{formatScoreCompact(intonationScore)}%</span>
                                                        </h3>
                                                        <div className="flex gap-4 text-xs text-gray-600">
                                                            <span className="flex items-center gap-1">
                                                                <span className="w-3 h-3 rounded-full bg-green-500"></span> 绿色：自然
                                                            </span>
                                                            <span className="flex items-center gap-1">
                                                                <span className="w-3 h-3 rounded-full bg-red-500"></span> 红色：需调整
                                                            </span>
                                                            <span className="flex items-center gap-1">
                                                                <span className="w-2 h-2 rounded-full bg-gray-400"></span> 灰色：轻读
                                                            </span>
                                                        </div>
                                                    </div>
                                                    <div className="mb-3 text-[11px] text-gray-500">
                                                        大球表示相对更突出，小球表示相对更轻。红色表示该处的重弱处理还可调整。
                                                    </div>
                                                    {intonation?.best_sentence ? (
                                                        <div className="space-y-4">
                                                            {/* 鏈€浣冲彞瀛?*/}
                                                            <div className="bg-green-50 rounded-lg p-3">
                                                                <div className="text-xs text-green-600 font-medium mb-2">✅ 节奏自然的一句</div>
                                                                {renderBouncingBalls(intonation.best_sentence.words)}
                                                                <div className="text-xs text-gray-500 mt-2">{intonation.best_sentence.tip}</div>
                                                            </div>

                                                            {/* 闇€鏀硅繘鍙ュ瓙 */}
                                                            {intonation.problem_sentences?.slice(0, 2).map((ps, idx) => (
                                                                <div key={idx} className="bg-yellow-50 rounded-lg p-3">
                                                                    <div className="text-xs text-yellow-700 font-medium mb-2">🔧 最值得调整的一句</div>
                                                                    {renderBouncingBalls(ps.words)}
                                                                    <div className="text-xs text-orange-600 mt-2">{ps.tip}</div>
                                                                </div>
                                                            ))}
                                                        </div>
                                                    ) : (
                                                        <div className="text-gray-500 text-sm">暂无可分析的语调数据（请检查音频清晰度后重试）。</div>
                                                    )}
                                                </div>
                                            );
                                        })()}

                                        {/* 瀹屾暣搴﹀垎鏋?*/}
                                        {moduleId === 'completeness' && (
                                            <div className="border border-gray-200 rounded-lg p-4">
                                                <h3 className="text-base font-bold mb-2">📝 完整度分析</h3>
                                                {reportData.analysis.missing_words?.length > 0 ? (
                                                    <div>
                                                        <div className="text-sm text-gray-600 mb-1">漏读词汇:</div>
                                                        <div className="flex flex-wrap gap-2">
                                                            {reportData.analysis.missing_words.map((word, idx) => (
                                                                <span key={idx} className="bg-red-100 text-red-700 px-2 py-0.5 rounded text-sm">
                                                                    {word}
                                                                </span>
                                                            ))}
                                                        </div>
                                                        {completenessScriptMap.length > 0 && (
                                                            <div className="mt-3 p-3 bg-gray-50 rounded-lg">
                                                                <div className="text-xs text-gray-500 mb-1">漏词位置（脚本文本）</div>
                                                                <div className="flex flex-wrap gap-1 leading-relaxed">
                                                                    {completenessScriptMap.map((item, idx) => (
                                                                        <span
                                                                            key={idx}
                                                                            className={item.missing ? 'bg-red-100 text-red-700 px-1 rounded font-semibold' : 'text-gray-700'}
                                                                        >
                                                                            {item.word}
                                                                        </span>
                                                                    ))}
                                                                </div>
                                                            </div>
                                                        )}
                                                    </div>
                                                ) : (
                                                    <div className="text-green-600 text-sm">✅ 朗读完整，无漏读词汇</div>
                                                )}
                                            </div>
                                        )}

                                        {/* 杩熺枒鍒嗘瀽 */}
                                        {moduleId === 'hesitation' && (
                                            <div className="border border-gray-200 rounded-lg p-4">
                                                <h3 className="text-base font-bold mb-2">⏱️ 迟疑分析</h3>
                                                {reportData.analysis.hesitations ? (
                                                    <div className="grid grid-cols-3 gap-2 text-center">
                                                        <div className="p-2 bg-gray-50 rounded">
                                                            <div className="text-lg font-bold text-blue-600">{reportData.analysis.hesitations.total_count}</div>
                                                            <div className="text-xs text-gray-500">总迟疑次数</div>
                                                        </div>
                                                        <div className="p-2 bg-gray-50 rounded">
                                                            <div className="text-lg font-bold text-yellow-600">{reportData.analysis.hesitations.filler_count}</div>
                                                            <div className="text-xs text-gray-500">填充词</div>
                                                        </div>
                                                        <div className="p-2 bg-gray-50 rounded">
                                                            <div className="text-lg font-bold text-red-600">{reportData.analysis.hesitations.long_pause_count}</div>
                                                            <div className="text-xs text-gray-500">长停顿</div>
                                                        </div>
                                                    </div>
                                                ) : (
                                                    <div className="text-green-600 text-sm">✅ 流利朗读，无明显迟疑</div>
                                                )}
                                            </div>
                                        )}
                                    </div>
                                ))}

                                {selectedModules.length === 0 && (
                                    <div className="py-16 text-center text-gray-400 border-2 border-dashed border-gray-300 rounded-xl">
                                        拖拽左侧模块到此处
                                    </div>
                                )}

                                    <div className="report-footer text-center text-xs text-gray-400 pt-3 border-t border-gray-200">
                                        Generated by SpeechMaster © 2026
                                    </div>
                                </div>
                            )}
                        </div>
                    </div>
                </div>
            </div>

            <style>{`
                .report-print-root {
                    background: #ffffff;
                    position: relative;
                    overflow: hidden;
                }
                .report-content-surface {
                    min-height: calc(297mm - 24mm);
                    position: relative;
                    z-index: 1;
                }
                .report-template-bg {
                    position: absolute;
                    inset: 0;
                    display: flex;
                    flex-direction: column;
                    pointer-events: none;
                    z-index: 0;
                }
                .report-print-root.capture-template-aurora {
                    background:
                        radial-gradient(90% 80% at 12% 8%, rgba(56, 189, 248, 0.2), transparent 60%),
                        radial-gradient(90% 80% at 92% 10%, rgba(251, 113, 133, 0.16), transparent 58%),
                        linear-gradient(160deg, #f8fbff 0%, #eef4ff 100%);
                    padding: 14mm !important;
                }
                .report-print-root.capture-template-card {
                    background: linear-gradient(160deg, #f7f9fc 0%, #edf2f8 100%);
                    padding: 13mm !important;
                }
                .report-print-root.capture-template-mint {
                    background:
                        radial-gradient(95% 85% at 10% 8%, rgba(45, 212, 191, 0.2), transparent 62%),
                        radial-gradient(85% 78% at 92% 12%, rgba(16, 185, 129, 0.16), transparent 60%),
                        linear-gradient(170deg, #f4fffb 0%, #edf8f5 100%);
                    padding: 13mm !important;
                }
                .report-print-root.capture-template-sunset {
                    background:
                        radial-gradient(88% 82% at 14% 10%, rgba(251, 146, 60, 0.18), transparent 60%),
                        radial-gradient(90% 84% at 90% 12%, rgba(234, 179, 8, 0.18), transparent 62%),
                        linear-gradient(165deg, #fff9f1 0%, #fff1de 100%);
                    padding: 13mm !important;
                }
                .report-print-root.capture-template-ink {
                    background:
                        radial-gradient(90% 80% at 12% 9%, rgba(37, 99, 235, 0.16), transparent 62%),
                        radial-gradient(88% 76% at 90% 11%, rgba(30, 64, 175, 0.18), transparent 60%),
                        linear-gradient(160deg, #f6f9ff 0%, #eaf1ff 100%);
                    padding: 13mm !important;
                }
                .report-content-surface.report-content-surface-themed {
                    background: rgba(255, 255, 255, 0.97);
                    border: 1px solid #e5e7eb;
                    border-radius: 14px;
                    padding: 9mm;
                    box-shadow: 0 14px 30px rgba(15, 23, 42, 0.08);
                }
                .report-content-surface.report-content-surface-imported {
                    background: transparent;
                    border: none;
                    border-radius: 0;
                    box-shadow: none;
                    min-height: 100%;
                }

                @media print {
                    @page { size: A4; margin: 8mm; }
                    html, body { width: 210mm; }
                    body {
                        background: white !important;
                        -webkit-print-color-adjust: exact;
                        print-color-adjust: exact;
                        font-size: 10.5pt;
                        line-height: 1.4;
                    }
                    .print\\:hidden { display: none !important; }

                    /* Print canvas and section rhythm */
                    .report-print-root {
                        width: auto !important;
                        min-height: auto !important;
                        background: white !important;
                        padding: 0 !important;
                        margin: 0 !important;
                        box-shadow: none !important;
                        border-radius: 0 !important;
                    }
                    .report-content-surface {
                        min-height: auto !important;
                    }
                    .report-template-bg {
                        display: none !important;
                    }
                    .report-content-surface.report-content-surface-themed {
                        background: transparent !important;
                        border: none !important;
                        border-radius: 0 !important;
                        box-shadow: none !important;
                        padding: 0 !important;
                    }
                    .report-print-root .space-y-4 > :not([hidden]) ~ :not([hidden]) {
                        margin-top: 3mm !important;
                    }
                    .report-header {
                        break-after: avoid-page;
                        page-break-after: avoid;
                        margin-bottom: 3mm !important;
                        padding-bottom: 2.5mm !important;
                    }
                    .report-footer {
                        margin-top: 3mm !important;
                        break-inside: avoid;
                        page-break-inside: avoid;
                    }

                    /* Module baseline */
                    .module-block {
                        break-inside: avoid;
                        page-break-inside: avoid;
                        break-after: auto;
                        page-break-after: auto;
                        margin-bottom: 3mm !important;
                    }
                    .module-block > div {
                        border-radius: 2mm !important;
                        border-color: #CBD5E1 !important;
                        box-shadow: none !important;
                        padding: 2.6mm !important;
                    }
                    .module-block h3 {
                        font-size: 11pt !important;
                        line-height: 1.35 !important;
                        margin-bottom: 2mm !important;
                        break-after: avoid-page;
                        page-break-after: avoid;
                    }
                    .module-block .text-sm {
                        font-size: 9pt !important;
                        line-height: 1.4 !important;
                    }
                    .module-block .text-xs {
                        font-size: 8pt !important;
                        line-height: 1.35 !important;
                    }

                    /* Keep compact modules together */
                    .module-score_overview,
                    .module-pronunciation_diagnosis,
                    .module-ai_feedback,
                    .module-completeness,
                    .module-hesitation {
                        break-inside: avoid;
                        page-break-inside: avoid;
                    }

                    /* Allow long modules to flow to next page when needed */
                    .module-text_highlight,
                    .module-fluency_analysis,
                    .module-intonation_analysis {
                        break-inside: auto !important;
                        page-break-inside: auto !important;
                    }

                    /* Score overview: compact ring + score grid */
                    .module-score_overview .relative.w-20.h-20 {
                        width: 16mm !important;
                        height: 16mm !important;
                    }
                    .module-score_overview .grid.grid-cols-2 {
                        gap: 2mm !important;
                    }

                    /* Text highlight: denser line height for paper */
                    .module-text_highlight .leading-loose {
                        line-height: 1.65 !important;
                    }

                    /* Pronunciation diagnostics chips wrap neatly */
                    .module-pronunciation_diagnosis .flex.flex-wrap.gap-2 {
                        gap: 1.5mm !important;
                    }

                    /* AI feedback: reduce ink-heavy backgrounds */
                    .module-ai_feedback .bg-purple-50 {
                        background: #F8FAFC !important;
                    }

                    /* Fluency: better density and stable sub-block paging */
                    .module-fluency_analysis .flex.items-center.justify-between.mb-4,
                    .module-intonation_analysis .flex.items-center.justify-between.mb-4 {
                        display: block !important;
                        margin-bottom: 2mm !important;
                    }
                    .module-fluency_analysis .flex.gap-4.text-xs.text-gray-600,
                    .module-intonation_analysis .flex.gap-4.text-xs.text-gray-600 {
                        display: grid !important;
                        grid-template-columns: repeat(2, minmax(0, 1fr));
                        column-gap: 2.2mm !important;
                        row-gap: 1mm !important;
                        margin-top: 1.3mm !important;
                    }
                    .module-fluency_analysis .grid.grid-cols-4 {
                        grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
                        gap: 2mm !important;
                    }
                    .module-fluency_analysis .grid.grid-cols-3 {
                        gap: 1.8mm !important;
                    }
                    .module-fluency_analysis .bg-gray-50.rounded-lg {
                        padding: 2.2mm !important;
                        font-size: 8.8pt !important;
                        line-height: 1.55 !important;
                    }
                    .module-fluency_analysis .leading-loose {
                        line-height: 1.65 !important;
                    }
                    .module-fluency_analysis .bg-gray-50,
                    .module-fluency_analysis .grid.grid-cols-3,
                    .module-fluency_analysis .grid.grid-cols-4,
                    .module-fluency_analysis .mt-3 {
                        break-inside: avoid;
                        page-break-inside: avoid;
                    }

                    /* Intonation: keep each sentence card intact */
                    .module-intonation_analysis .space-y-4 > div {
                        padding: 2.3mm !important;
                        break-inside: avoid;
                        page-break-inside: avoid;
                    }

                    /* Completeness and hesitation blocks: tighter spacing */
                    .module-completeness .rounded-lg,
                    .module-hesitation .rounded-lg {
                        padding-top: 2.2mm !important;
                        padding-bottom: 2.2mm !important;
                    }
                    p, li {
                        orphans: 3;
                        widows: 3;
                    }
                }
            `}</style>
        </div>
    );
}





