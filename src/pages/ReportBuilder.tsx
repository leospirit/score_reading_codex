import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { Printer, GripVertical, Check, X, Plus, Eye, Camera, Download } from 'lucide-react';
import JSZip from 'jszip';

// 可用模块定义
interface ModuleConfig {
    id: string;
    name: string;
    icon: string;
    description: string;
    isDefault: boolean;
}

const AVAILABLE_MODULES: ModuleConfig[] = [
    { id: 'score_overview', name: '总分概览', icon: '📊', description: '圆环图 + 四维分数', isDefault: true },
    { id: 'text_highlight', name: '朗读对照', icon: '📖', description: '带颜色标注的朗读文本', isDefault: true },
    { id: 'pronunciation_diagnosis', name: '核心发音诊断', icon: '🎯', description: '弱读单词及错误详情', isDefault: true },
    { id: 'ai_feedback', name: '综合反馈', icon: '👩‍🏫', description: '基于事实的鼓励与建议', isDefault: true },
    { id: 'fluency_analysis', name: '流利度分析', icon: '〰️', description: '停顿/语速/迟疑', isDefault: false },
    { id: 'intonation_analysis', name: '韵律分析', icon: '🗣️', description: '重音与节奏可视化', isDefault: false },
    { id: 'completeness', name: '完整度分析', icon: '📝', description: '漏读词统计', isDefault: false },
    { id: 'hesitation', name: '迟疑分析', icon: '⚡', description: '填充词检测', isDefault: false },
];

// 基于实际 JSON 结构的接口定义
interface ReportData {
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
            pause?: {
                type: 'good' | 'optional' | 'bad' | 'missed';
                duration: number;
            };
        }>;
    };
    analysis: {
        weak_words: string[];
        weak_phonemes: string[];
        missing_words: string[];
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
            expected_words: number;
            spoken_words: number;
            missing_count: number;
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
        integrated_feedback: {
            overall_comment: string;
            specific_suggestions: string[];
            practice_tips: string[];
            fun_challenge: string;
        };
    };
}

function formatScoreCompact(value: number, digits = 1): string {
    const n = Number(value);
    if (!Number.isFinite(n)) return '--';
    const factor = Math.pow(10, digits);
    const rounded = Math.round(n * factor) / factor;
    if (Number.isInteger(rounded)) return String(Math.trunc(rounded));
    return rounded.toFixed(digits).replace(/\.0+$/, '').replace(/(\.\d*[1-9])0+$/, '$1');
}

function isLowConfidenceTimeline(words: ReportData['alignment']['words']): boolean {
    if (!Array.isArray(words) || words.length < 7) return false;
    const gaps: number[] = [];
    for (let i = 0; i < words.length - 1; i += 1) {
        const left = Number((words[i] as any)?.end);
        const right = Number((words[i + 1] as any)?.start);
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

export default function ReportBuilder() {
    const [selectedReportId, setSelectedReportId] = useState<string | null>(null);
    const [reportData, setReportData] = useState<ReportData | null>(null);
    const [reports, setReports] = useState<Array<{ id: string; student_name: string; score: number }>>([]);
    const [selectedModules, setSelectedModules] = useState<string[]>(
        AVAILABLE_MODULES.filter(m => m.isDefault).map(m => m.id)
    );
    const [draggedModule, setDraggedModule] = useState<string | null>(null);
    const [isCapturing, setIsCapturing] = useState(false);

    // 批量生成相关状态
    const [selectedReportIds, setSelectedReportIds] = useState<Set<string>>(new Set());
    const [batchProgress, setBatchProgress] = useState({ current: 0, total: 0, name: '' });

    const reportRef = useRef<HTMLDivElement>(null);
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

    useEffect(() => {
        fetch('http://localhost:8000/api/reports')
            .then(res => res.json())
            .then(data => setReports(data))
            .catch(console.error);
    }, []);

    useEffect(() => {
        if (selectedReportId) {
            fetch(`http://localhost:8000/api/reports/${selectedReportId}/data`)
                .then(res => res.json())
                .then(data => setReportData(data))
                .catch(console.error);
        }
    }, [selectedReportId]);

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

    // 截图功能 - 使用另存为对话框
    const handleCapture = async () => {
        if (!reportRef.current || !reportData) return;
        setIsCapturing(true);

        try {
            const { toPng, toBlob } = await import('html-to-image');

            // html-to-image 对现代 CSS 支持更好
            const dataUrl = await toPng(reportRef.current, {
                backgroundColor: '#ffffff',
                cacheBust: true,
                pixelRatio: 2,
            });

            const fileName = `${reportData.meta.student_name || reportData.meta.student_id}_report.png`;

            // 尝试使用 File System Access API
            if ('showSaveFilePicker' in window) {
                try {
                    const blob = await toBlob(reportRef.current, {
                        backgroundColor: '#ffffff',
                        pixelRatio: 2,
                    });

                    if (blob) {
                        const handle = await (window as any).showSaveFilePicker({
                            suggestedName: fileName,
                            types: [{
                                description: 'PNG 图片',
                                accept: { 'image/png': ['.png'] },
                            }],
                        });

                        const writable = await handle.createWritable();
                        await writable.write(blob);
                        await writable.close();
                        return;
                    }
                } catch (e: any) {
                    if (e.name === 'AbortError') return;
                }
            }

            // 回退下载
            const link = document.createElement('a');
            link.download = fileName;
            link.href = dataUrl;
            link.click();
        } catch (err) {
            console.error('截图失败:', err);
            alert(`截图失败: ${err instanceof Error ? err.message : '未知错误'}`);
        } finally {
            setIsCapturing(false);
        }
    };

    // 批量生成图片
    const handleBatchGenerate = async () => {
        if (selectedReportIds.size === 0) {
            alert('请先选择要生成的报告');
            return;
        }

        setIsCapturing(true);
        const ids = Array.from(selectedReportIds);
        setBatchProgress({ current: 0, total: ids.length, name: '' });
        let successCount = 0;
        let errorList: string[] = [];

        let dirHandle: any = null;
        const zip = new JSZip();

        try {
            console.log('检查 showDirectoryPicker 支持情况...', 'showDirectoryPicker' in window);
            if ('showDirectoryPicker' in window) {
                try {
                    dirHandle = await (window as any).showDirectoryPicker({
                        mode: 'readwrite'
                    });
                } catch (e: any) {
                    if (e.name === 'AbortError') {
                        setIsCapturing(false);
                        return;
                    }
                    console.warn('无法获取目录权限，将回退到 ZIP 打包模式');
                }
            } else {
                console.warn('当前浏览器不支持 Directory Picker API，将使用 ZIP 打包模式');
            }

            const { toBlob } = await import('html-to-image');

            for (let i = 0; i < ids.length; i++) {
                const id = ids[i];
                const report = reports.find(r => r.id === id);
                const studentName = report?.student_name || id;
                setBatchProgress({ current: i + 1, total: ids.length, name: studentName });

                try {
                    const res = await fetch(`http://localhost:8000/api/reports/${id}/data`);
                    if (!res.ok) throw new Error(`HTTP ${res.status}`);
                    const data = await res.json();
                    setReportData(data);

                    // 等待渲染
                    await new Promise<void>(resolve => {
                        requestAnimationFrame(() => {
                            requestAnimationFrame(() => {
                                setTimeout(resolve, 600);
                            });
                        });
                    });

                    if (reportRef.current) {
                        const fileName = `${data.meta?.student_name || data.meta?.student_id || studentName}_report.png`;

                        if (dirHandle) {
                            // 直接写入文件夹
                            const blob = await toBlob(reportRef.current, { backgroundColor: '#ffffff', pixelRatio: 2 });
                            if (blob) {
                                const fileHandle = await dirHandle.getFileHandle(fileName, { create: true });
                                const writable = await fileHandle.createWritable();
                                await writable.write(blob);
                                await writable.close();
                                successCount++;
                            }
                        } else {
                            // 添加到 ZIP
                            const blob = await toBlob(reportRef.current, { backgroundColor: '#ffffff', pixelRatio: 2 });
                            if (blob) {
                                zip.file(fileName, blob);
                                successCount++;
                            }
                        }

                        await new Promise(resolve => setTimeout(resolve, 200));
                    }
                } catch (err) {
                    console.error(`生成 ${studentName} 报告失败:`, err);
                    errorList.push(studentName);
                }
            }

            // 如果使用的是 ZIP 模式，最后触发一次下载
            if (!dirHandle && successCount > 0) {
                setBatchProgress(prev => ({ ...prev, name: '正在打包 ZIP...' }));
                const content = await zip.generateAsync({ type: 'blob' });
                const link = document.createElement('a');
                link.href = URL.createObjectURL(content);
                link.download = `reports_batch_${new Date().getTime()}.zip`;
                link.click();
            }

            if (errorList.length === 0) {
                alert(`✅ 已成功生成 ${successCount} 份报告！${dirHandle ? '文件已存入指定目录。' : '正在下载压缩包。'}`);
            } else {
                alert(`⚠️ 完成！成功 ${successCount} 份，失败 ${errorList.length} 份\n失败: ${errorList.join(', ')}`);
            }
        } catch (err) {
            console.error('批量生成失败:', err);
            alert(`批量生成失败: ${err instanceof Error ? err.message : '未知错误'}`);
        } finally {
            setIsCapturing(false);
            setBatchProgress({ current: 0, total: 0, name: '' });
        }
    };

    // 全选/取消全选
    const toggleSelectAll = () => {
        if (selectedReportIds.size === reports.length) {
            setSelectedReportIds(new Set());
        } else {
            setSelectedReportIds(new Set(reports.map(r => r.id)));
        }
    };

    // 切换单个选择
    const toggleReportSelection = (id: string) => {
        const newSet = new Set(selectedReportIds);
        if (newSet.has(id)) {
            newSet.delete(id);
        } else {
            newSet.add(id);
        }
        setSelectedReportIds(newSet);
    };

    const getTagColor = (tag: string) => {
        switch (tag) {
            case 'ok': return '#22C55E';
            case 'weak': return '#F59E0B';
            case 'poor': case 'missing': return '#EF4444';
            default: return '#1F2937';
        }
    };

    const getLevelLabel = (score: number) => {
        if (score >= 90) return { label: 'Native Like', color: '#A855F7' };
        if (score >= 80) return { label: 'Advanced', color: '#22C55E' };
        if (score >= 60) return { label: 'High-Intermediate', color: '#3B82F6' };
        return { label: 'Beginner', color: '#EF4444' };
    };

    return (
        <div className="min-h-screen bg-[#0a0a0a] pt-20">
            <div className="max-w-7xl mx-auto px-4 py-8 flex gap-6">
                {/* 左侧模块选择器 */}
                <div className="w-72 shrink-0 print:hidden">
                    <div className="bg-[#1e1e24] border border-white/10 rounded-2xl p-5 sticky top-24">
                        <h2 className="text-lg font-bold text-white mb-4 flex items-center gap-2">
                            <span className="text-2xl">📦</span> 可用模块
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

                {/* 右侧报告预览 */}
                <div className="flex-1">
                    {/* 工具栏 */}
                    <div className="flex flex-col gap-4 mb-6 print:hidden">
                        <div className="flex items-center justify-between">
                            <div className="flex items-center gap-4">
                                <h1 className="text-2xl font-bold text-white flex items-center gap-3">
                                    <span className="text-3xl">📋</span> 报告生成器
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
                            </div>

                            <div className="flex gap-2">
                                <button
                                    onClick={handleCapture}
                                    disabled={!reportData || isCapturing}
                                    className="bg-blue-500 text-white px-5 py-2.5 rounded-xl font-bold flex items-center gap-2 hover:bg-blue-600 disabled:opacity-50 transition-all"
                                >
                                    {isCapturing && batchProgress.total === 0 ? <Download className="w-4 h-4 animate-spin" /> : <Camera className="w-4 h-4" />}
                                    保存图片
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

                        {/* 批量操作区 */}
                        <div className="bg-[#1e1e24] border border-white/10 rounded-xl p-4">
                            <div className="flex items-center gap-4">
                                {/* 下拉选择器 */}
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

                                    {/* 勾选列表 */}
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

                                {/* 批量生成按钮 */}
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

                            {/* 进度条 */}
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

                    {/* A4 预览区 */}
                    <div
                        ref={reportRef}
                        onDragOver={handleDragOver}
                        onDrop={handleDrop}
                        className={`bg-white rounded-lg shadow-2xl mx-auto print:shadow-none print:rounded-none
                            ${draggedModule ? 'ring-2 ring-primary ring-dashed' : ''}`}
                        style={{ width: '210mm', minHeight: '297mm', padding: '12mm' }}
                    >
                        {!reportData ? (
                            <div className="h-full flex flex-col items-center justify-center text-gray-400 py-32">
                                <Eye className="w-16 h-16 mb-4 opacity-30" />
                                <p className="text-lg font-medium">请选择一个学生报告</p>
                                <p className="text-sm text-gray-500 mt-1">拖拽左侧模块自定义报告内容</p>
                            </div>
                        ) : (
                            <div className="text-gray-800 space-y-4">
                                {/* 报告头部 */}
                                <div className="text-center pb-3 border-b-2 border-gray-200">
                                    <h1 className="text-xl font-bold text-gray-900">英语朗读评测报告</h1>
                                    <div className="flex justify-center gap-8 mt-1 text-sm text-gray-600">
                                        <span>学生: <strong>{reportData.meta.student_name || reportData.meta.student_id}</strong></span>
                                        <span>日期: {new Date(reportData.meta.timestamp).toLocaleDateString()}</span>
                                    </div>
                                </div>

                                {/* 模块渲染 */}
                                {selectedModules.map(moduleId => (
                                    <div key={moduleId} className="relative group print:break-inside-avoid">
                                        <button
                                            onClick={() => removeModule(moduleId)}
                                            className="absolute -right-2 -top-2 w-6 h-6 bg-red-500 text-white rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity print:hidden z-10"
                                        >
                                            <X className="w-3 h-3" />
                                        </button>

                                        {/* 总分概览 */}
                                        {moduleId === 'score_overview' && (
                                            <div className="border border-gray-200 rounded-lg p-4">
                                                <h3 className="text-base font-bold mb-3">📊 总分概览</h3>
                                                <div className="flex items-center gap-6">
                                                    <div className="relative w-20 h-20">
                                                        <svg className="w-full h-full transform -rotate-90">
                                                            <circle cx="40" cy="40" r="35" stroke="#E5E7EB" strokeWidth="6" fill="none" />
                                                            <circle cx="40" cy="40" r="35"
                                                                stroke={getLevelLabel(reportData.scores.overall_100).color}
                                                                strokeWidth="6" fill="none"
                                                                strokeDasharray={2 * Math.PI * 35}
                                                                strokeDashoffset={2 * Math.PI * 35 * (1 - reportData.scores.overall_100 / 100)}
                                                                strokeLinecap="round"
                                                            />
                                                        </svg>
                                                        <div className="absolute inset-0 flex items-center justify-center">
                                                            <span className="text-xl font-bold" style={{ color: getLevelLabel(reportData.scores.overall_100).color }}>
                                                                {Math.round(reportData.scores.overall_100)}
                                                            </span>
                                                        </div>
                                                    </div>
                                                    <div className="grid grid-cols-2 gap-2 flex-1">
                                                        {[
                                                            { label: '发音', score: reportData.scores.pronunciation_100, color: '#3B82F6' },
                                                            { label: '语调', score: reportData.scores.intonation_100, color: '#22C55E' },
                                                            { label: '流利度', score: reportData.scores.fluency_100, color: '#F59E0B' },
                                                            { label: '完整度', score: reportData.scores.completeness_100, color: '#A855F7' },
                                                        ].map(item => (
                                                            <div key={item.label} className="p-2 border rounded text-center" style={{ borderColor: item.color + '40' }}>
                                                                <div className="text-lg font-bold" style={{ color: item.color }}>{Math.round(item.score)}</div>
                                                                <div className="text-xs text-gray-500">{item.label}</div>
                                                            </div>
                                                        ))}
                                                    </div>
                                                </div>
                                            </div>
                                        )}

                                        {/* 朗读对照 */}
                                        {moduleId === 'text_highlight' && (
                                            <div className="border border-gray-200 rounded-lg p-4">
                                                <h3 className="text-base font-bold mb-2">📖 朗读对照</h3>
                                                <div className="flex flex-wrap gap-1 leading-loose text-sm">
                                                    {displayWords.map((word, idx) => (
                                                        <span key={idx} style={{ color: getTagColor(word.tag) }} className="font-medium">
                                                            {word.word}
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

                                        {/* 核心发音诊断 */}
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
                                                        {reportData.analysis.weak_phonemes?.length > 0 && (
                                                            <div className="mt-2">
                                                                <div className="text-sm text-gray-600">弱读音素:</div>
                                                                <div className="flex gap-2 mt-1">
                                                                    {reportData.analysis.weak_phonemes.map((ph, idx) => (
                                                                        <span key={idx} className="bg-yellow-100 text-yellow-700 px-2 py-0.5 rounded text-sm">
                                                                            /{ph}/
                                                                        </span>
                                                                    ))}
                                                                </div>
                                                            </div>
                                                        )}
                                                    </div>
                                                ) : (
                                                    <div className="text-green-600 text-sm">✓ 发音整体良好！</div>
                                                )}
                                            </div>
                                        )}

                                        {/* 综合反馈 */}
                                        {moduleId === 'ai_feedback' && reportData.engine_raw?.integrated_feedback && (
                                            <div className="border border-gray-200 rounded-lg p-4">
                                                <h3 className="text-base font-bold mb-2">👩‍🏫 综合反馈</h3>
                                                <div className="text-sm text-gray-700 mb-2">
                                                    {reportData.engine_raw.integrated_feedback.overall_comment}
                                                </div>
                                                {reportData.engine_raw.integrated_feedback.specific_suggestions?.length > 0 && (
                                                    <ul className="text-sm text-gray-600 space-y-1 mb-2">
                                                        {reportData.engine_raw.integrated_feedback.specific_suggestions.slice(0, 2).map((s, i) => (
                                                            <li key={i}>• {s}</li>
                                                        ))}
                                                    </ul>
                                                )}
                                                {reportData.engine_raw.integrated_feedback.fun_challenge && (
                                                    <div className="mt-2 p-2 bg-purple-50 rounded text-sm text-purple-700">
                                                        {reportData.engine_raw.integrated_feedback.fun_challenge}
                                                    </div>
                                                )}
                                            </div>
                                        )}

                                        {/* 流利度分析 */}
                                        {moduleId === 'fluency_analysis' && (() => {
                                            // 计算停顿统计
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

                                            // 停顿符号渲染
                                            const renderPauseMarker = (pause: { type: string; duration: number }) => {
                                                if (pause.type === 'good') {
                                                    return <span className="mx-1 text-green-500 text-lg">●</span>;
                                                } else if (pause.type === 'optional') {
                                                    return <span className="mx-1 text-gray-400 text-sm">●</span>;
                                                } else if (pause.type === 'bad') {
                                                    return <span className="mx-1 text-red-500 font-bold">‖</span>;
                                                } else if (pause.type === 'missed') {
                                                    if (lowConfidenceTimeline) return null;
                                                    return <span className="mx-1 text-red-400 text-xs">▲</span>;
                                                }
                                                return null;
                                            };

                                            return (
                                                <div className="border border-gray-200 rounded-lg p-4">
                                                    {/* 头部：分数 + 图例 */}
                                                    <div className="flex items-center justify-between mb-4">
                                                        <h3 className="text-base font-bold flex items-center gap-2">
                                                            〰️ 流利度分析
                                                            <span className="text-2xl font-bold text-blue-500">{formatScoreCompact(fluencyScore)}%</span>
                                                        </h3>
                                                        <div className="flex gap-4 text-xs text-gray-600">
                                                            <span className="flex items-center gap-1">
                                                                <span className="text-green-500 text-lg">●</span> 合理停顿
                                                            </span>
                                                            <span className="flex items-center gap-1">
                                                                <span className="text-gray-400">●</span> 可选停顿
                                                            </span>
                                                            <span className="flex items-center gap-1">
                                                                <span className="text-red-500 font-bold">‖</span> 不当卡顿
                                                            </span>
                                                            {!lowConfidenceTimeline && (
                                                                <span className="flex items-center gap-1" title="Missing break">
                                                                    <span className="inline-flex items-center justify-center h-4 min-w-4 px-1 rounded border border-red-200 bg-red-50 text-[10px] font-bold text-red-500">MB</span>
                                                                </span>
                                                            )}
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

                                                    {/* 朗读文本 + 停顿标记 */}
                                                    <div className="bg-gray-50 rounded-lg p-4 mb-4 leading-loose text-base">
                                                        {displayWords.map((word, idx) => (
                                                            <span key={idx}>
                                                                <span className="text-gray-800">{word.word} </span>
                                                                {word.pause && renderPauseMarker(word.pause)}
                                                            </span>
                                                        ))}
                                                    </div>

                                                    {/* 数据统计条 */}
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
                                                            <div className="text-xs text-gray-500">不当卡顿</div>
                                                        </div>
                                                        <div className="bg-purple-50 rounded-lg py-2">
                                                            <div className="text-lg font-bold text-purple-600">{formatScoreCompact(totalPauseDuration)}s</div>
                                                            <div className="text-xs text-gray-500">总停顿时长</div>
                                                        </div>
                                                    </div>

                                                    {/* 问题词提示 */}
                                                    {badPauses.length > 0 && (
                                                        <div className="mt-3 p-2 bg-red-50 rounded-lg text-sm text-red-700">
                                                            ⚠️ 卡顿位置: {badPauses.slice(0, 5).map(w => `"${w.word}"`).join('、')}
                                                            {badPauses.length > 5 && ` 等${badPauses.length}处`}
                                                        </div>
                                                    )}
                                                </div>
                                            );
                                        })()}

                                        {/* 韵律分析 - 弹跳球可视化 */}
                                        {moduleId === 'intonation_analysis' && (() => {
                                            const intonation = reportData.analysis.intonation_analysis;
                                            const intonationScore = reportData.scores.intonation_100;

                                            // 渲染弹跳球句子
                                            const renderBouncingBalls = (words: Array<{ word: string; is_stressed: boolean; stress_correct: boolean }>) => (
                                                <div className="flex flex-wrap items-end gap-1 py-2">
                                                    {words.map((w, i) => (
                                                        <div key={i} className="flex flex-col items-center">
                                                            {/* 球 */}
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
                                                            {/* 单词 */}
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
                                                    {/* 头部 */}
                                                    <div className="flex items-center justify-between mb-4">
                                                        <h3 className="text-base font-bold flex items-center gap-2">
                                                            🗣️ 韵律分析
                                                            <span className="text-2xl font-bold text-purple-500">{formatScoreCompact(intonationScore)}%</span>
                                                        </h3>
                                                        <div className="flex gap-4 text-xs text-gray-600">
                                                            <span className="flex items-center gap-1">
                                                                <span className="w-3 h-3 rounded-full bg-green-500"></span> 重读正确
                                                            </span>
                                                            <span className="flex items-center gap-1">
                                                                <span className="w-3 h-3 rounded-full bg-red-500"></span> 重读错误
                                                            </span>
                                                            <span className="flex items-center gap-1">
                                                                <span className="w-2 h-2 rounded-full bg-gray-400"></span> 非重读
                                                            </span>
                                                        </div>
                                                    </div>

                                                    {intonation?.best_sentence ? (
                                                        <div className="space-y-4">
                                                            {/* 最佳句子 */}
                                                            <div className="bg-green-50 rounded-lg p-3">
                                                                <div className="text-xs text-green-600 font-medium mb-2">✨ 最佳句子 (重读准确率 {intonation.best_sentence.stress_accuracy.toFixed(0)}%)</div>
                                                                {renderBouncingBalls(intonation.best_sentence.words)}
                                                                <div className="text-xs text-gray-500 mt-2">{intonation.best_sentence.tip}</div>
                                                            </div>

                                                            {/* 需改进句子 */}
                                                            {intonation.problem_sentences?.slice(0, 2).map((ps, idx) => (
                                                                <div key={idx} className="bg-yellow-50 rounded-lg p-3">
                                                                    <div className="flex items-center justify-between mb-2">
                                                                        <span className="text-xs text-yellow-600 font-medium">需改进</span>
                                                                        <span className="text-xs text-gray-500">准确率 {ps.stress_accuracy.toFixed(0)}%</span>
                                                                    </div>
                                                                    {renderBouncingBalls(ps.words)}
                                                                    <div className="text-xs text-orange-600 mt-2">{ps.tip}</div>
                                                                </div>
                                                            ))}
                                                        </div>
                                                    ) : (
                                                        <div className="text-gray-500 text-sm">语调数据分析中...</div>
                                                    )}
                                                </div>
                                            );
                                        })()}

                                        {/* 完整度分析 */}
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
                                                    </div>
                                                ) : (
                                                    <div className="text-green-600 text-sm">✓ 朗读完整，无漏读词汇</div>
                                                )}
                                            </div>
                                        )}

                                        {/* 迟疑分析 */}
                                        {moduleId === 'hesitation' && (
                                            <div className="border border-gray-200 rounded-lg p-4">
                                                <h3 className="text-base font-bold mb-2">⚡ 迟疑分析</h3>
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
                                                    <div className="text-green-600 text-sm">✓ 流利朗读，无明显迟疑</div>
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

                                <div className="text-center text-xs text-gray-400 pt-3 border-t border-gray-200">
                                    Generated by SpeechMaster © 2026
                                </div>
                            </div>
                        )}
                    </div>
                </div>
            </div>

            <style>{`
                @media print {
                    body { background: white !important; }
                    .print\\:hidden { display: none !important; }
                    @page { size: A4; margin: 0; }
                }
            `}</style>
        </div>
    );
}
