import { useEffect, useMemo, useState } from 'react';
import {
    AlertCircle,
    CalendarDays,
    ChartColumnBig,
    Clock3,
    GraduationCap,
    PanelsTopLeft,
    RefreshCw,
    Search,
    Sparkles,
    Users,
} from 'lucide-react';
import { API_HOST } from '../config/api';

interface ReportItem {
    id: string;
    url: string;
    timestamp: number;
    student_name: string;
    display_name?: string;
    original_filename?: string | null;
    score?: number | null;
}

type BoardMode = 'teacher' | 'student';
type DateFilter = 'all' | 'today' | '7d' | '30d';

const DATE_FILTER_LABEL: Record<DateFilter, string> = {
    all: '全部时间',
    today: '今天',
    '7d': '近7天',
    '30d': '近30天',
};

const PASS_LINE = 80;
const WEAK_LINE = 60;

const toNumber = (value: unknown): number | null => {
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
};

const formatDateTime = (timestamp: number): string => {
    if (!Number.isFinite(timestamp) || timestamp <= 0) return '--';
    const d = new Date(timestamp * 1000);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
};

const isInRange = (timestamp: number, filter: DateFilter): boolean => {
    if (filter === 'all') return true;
    if (!Number.isFinite(timestamp) || timestamp <= 0) return false;

    const now = new Date();
    const target = new Date(timestamp * 1000);

    if (filter === 'today') {
        return now.getFullYear() === target.getFullYear()
            && now.getMonth() === target.getMonth()
            && now.getDate() === target.getDate();
    }

    const days = filter === '7d' ? 7 : 30;
    const cutoff = Date.now() - days * 24 * 60 * 60 * 1000;
    return target.getTime() >= cutoff;
};

const extractClassKey = (item: ReportItem): string => {
    const raw = String(item.url || '').replace(/^\/+/, '');
    const parts = raw.split('/').filter(Boolean);
    if (parts.length >= 2 && parts[0] === 'reports') {
        const key = decodeURIComponent(parts[1] || '').trim();
        return key || '未分班';
    }
    return '未分班';
};

const prettyClassLabel = (key: string): string => {
    if (key === 'upload') return '上传区';
    if (/^\d+$/.test(key)) return `${key}班`;
    return key;
};

export default function DashboardPro() {
    const [reports, setReports] = useState<ReportItem[]>([]);
    const [isLoading, setIsLoading] = useState(true);
    const [isRefreshing, setIsRefreshing] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [lastUpdatedAt, setLastUpdatedAt] = useState<number | null>(null);
    const [isPgSyncing, setIsPgSyncing] = useState(false);
    const [pgSyncMessage, setPgSyncMessage] = useState<string | null>(null);

    const [mode, setMode] = useState<BoardMode>('teacher');
    const [classFilter, setClassFilter] = useState<string>('all');
    const [dateFilter, setDateFilter] = useState<DateFilter>('7d');
    const [searchTerm, setSearchTerm] = useState('');

    const fetchReports = async (silent = false): Promise<void> => {
        if (silent) {
            setIsRefreshing(true);
        } else {
            setIsLoading(true);
        }
        setError(null);

        try {
            const response = await fetch(`${API_HOST}/api/reports`);
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            const data: unknown = await response.json();
            const list = Array.isArray(data) ? data : [];
            const normalized: ReportItem[] = list
                .map((row) => {
                    const src = (row && typeof row === 'object') ? row as Partial<ReportItem> : {};
                    return {
                        id: String(src.id || '').trim(),
                        url: String(src.url || '').trim(),
                        timestamp: Number(src.timestamp || 0),
                        student_name: String(src.student_name || '').trim(),
                        display_name: String(src.display_name || '').trim() || undefined,
                        original_filename: src.original_filename ? String(src.original_filename) : null,
                        score: toNumber(src.score),
                    };
                })
                .filter((item) => item.id && item.url)
                .sort((a, b) => b.timestamp - a.timestamp);

            setReports(normalized);
            setLastUpdatedAt(Date.now());
        } catch (err: unknown) {
            const message = err instanceof Error ? err.message : '加载失败';
            setError(`加载看板数据失败: ${message}`);
        } finally {
            setIsLoading(false);
            setIsRefreshing(false);
        }
    };

    useEffect(() => {
        void fetchReports(false);
    }, []);

    const triggerPgSync = async (): Promise<void> => {
        if (isPgSyncing) return;
        setPgSyncMessage(null);
        setIsPgSyncing(true);
        try {
            const response = await fetch(`${API_HOST}/api/analytics/pg-sync`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ dry_run: false }),
            });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) {
                const detail = typeof payload?.detail === 'string' ? payload.detail : `HTTP ${response.status}`;
                throw new Error(detail);
            }
            const summary = payload?.summary ?? {};
            const processed = Number(summary.processed || 0);
            const inserted = Number(summary.inserted || 0);
            const updated = Number(summary.updated || 0);
            const skipped = Number(summary.skipped || 0);
            const errors = Number(summary.errors || 0);
            setPgSyncMessage(`PG同步完成：处理${processed}，新增${inserted}，更新${updated}，跳过${skipped}，错误${errors}`);
        } catch (err: unknown) {
            const message = err instanceof Error ? err.message : '触发失败';
            setPgSyncMessage(`PG同步失败：${message}`);
        } finally {
            setIsPgSyncing(false);
        }
    };

    const classOptions = useMemo(() => {
        const keys = new Set<string>();
        for (const item of reports) {
            keys.add(extractClassKey(item));
        }
        return Array.from(keys).sort((a, b) => a.localeCompare(b, 'zh-Hans-CN'));
    }, [reports]);

    const filteredReports = useMemo(() => {
        const query = searchTerm.trim().toLowerCase();
        return reports.filter((item) => {
            const classKey = extractClassKey(item);
            if (classFilter !== 'all' && classKey !== classFilter) return false;
            if (!isInRange(item.timestamp, dateFilter)) return false;

            if (!query) return true;
            const haystack = [
                item.id,
                item.student_name,
                item.display_name || '',
                item.original_filename || '',
                classKey,
                prettyClassLabel(classKey),
            ].join(' ').toLowerCase();
            return haystack.includes(query);
        });
    }, [reports, classFilter, dateFilter, searchTerm]);

    const stats = useMemo(() => {
        const scored = filteredReports.filter((item) => Number.isFinite(item.score));
        const scoreValues = scored
            .map((item) => Number(item.score))
            .filter((value) => Number.isFinite(value));

        const avgScore = scoreValues.length > 0
            ? scoreValues.reduce((sum, val) => sum + val, 0) / scoreValues.length
            : null;
        const passCount = scoreValues.filter((val) => val >= PASS_LINE).length;
        const weakCount = scoreValues.filter((val) => val < WEAK_LINE).length;
        const excellentCount = scoreValues.filter((val) => val >= 90).length;

        const studentSet = new Set<string>();
        for (const item of filteredReports) {
            const key = (item.display_name || item.student_name || '').trim();
            if (key) studentSet.add(key);
        }

        return {
            total: filteredReports.length,
            scored: scoreValues.length,
            avgScore,
            passRate: scoreValues.length > 0 ? (passCount / scoreValues.length) * 100 : null,
            weakCount,
            excellentCount,
            studentCount: studentSet.size,
        };
    }, [filteredReports]);

    const classDistribution = useMemo(() => {
        const counter = new Map<string, number>();
        for (const item of filteredReports) {
            const key = extractClassKey(item);
            counter.set(key, (counter.get(key) || 0) + 1);
        }

        return Array.from(counter.entries())
            .map(([classKey, count]) => ({ classKey, count }))
            .sort((a, b) => b.count - a.count)
            .slice(0, 8);
    }, [filteredReports]);

    const recentRows = useMemo(() => filteredReports.slice(0, 12), [filteredReports]);

    return (
        <div className="pt-24 pb-16 px-4 md:px-8 min-h-screen">
            <div className="max-w-7xl mx-auto dashboard-pro-shell rounded-3xl border border-white/10 p-5 md:p-7">
                <section className="dashboard-pro-rise" style={{ animationDelay: '0.04s' }}>
                    <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
                        <div>
                            <h1 className="text-2xl md:text-3xl font-black text-white flex items-center gap-2">
                                <PanelsTopLeft className="w-7 h-7 text-cyan-300" />
                                Dashboard Pro
                            </h1>
                            <p className="text-sm text-slate-300 mt-2">
                                班级与时间维度统一筛选，教师/学生视图一键切换。
                            </p>
                        </div>

                        <div className="flex items-center gap-2 self-start lg:self-auto">
                            <button
                                type="button"
                                onClick={() => setMode('teacher')}
                                className={`h-9 px-3 rounded-lg text-xs font-semibold border transition ${mode === 'teacher'
                                    ? 'bg-cyan-400/20 text-cyan-100 border-cyan-300/40'
                                    : 'bg-white/5 text-slate-300 border-white/15 hover:bg-white/10'
                                    }`}
                            >
                                教师视图
                            </button>
                            <button
                                type="button"
                                onClick={() => setMode('student')}
                                className={`h-9 px-3 rounded-lg text-xs font-semibold border transition ${mode === 'student'
                                    ? 'bg-cyan-400/20 text-cyan-100 border-cyan-300/40'
                                    : 'bg-white/5 text-slate-300 border-white/15 hover:bg-white/10'
                                    }`}
                            >
                                学生视图
                            </button>
                            <button
                                type="button"
                                onClick={() => void fetchReports(true)}
                                className="h-9 px-3 rounded-lg text-xs font-semibold border border-white/20 bg-white/5 text-slate-100 hover:bg-white/10 inline-flex items-center gap-1"
                                disabled={isRefreshing || isLoading}
                            >
                                <RefreshCw className={`w-4 h-4 ${(isRefreshing || isLoading) ? 'animate-spin' : ''}`} />
                                刷新
                            </button>
                            <button
                                type="button"
                                onClick={() => void triggerPgSync()}
                                className="h-9 px-3 rounded-lg text-xs font-semibold border border-cyan-300/30 bg-cyan-400/10 text-cyan-100 hover:bg-cyan-400/20 inline-flex items-center gap-1"
                                disabled={isPgSyncing}
                                title="将 data/out 报告增量同步到 PG 分析表"
                            >
                                <RefreshCw className={`w-4 h-4 ${isPgSyncing ? 'animate-spin' : ''}`} />
                                同步PG
                            </button>
                        </div>
                    </div>
                </section>

                {pgSyncMessage && (
                    <section className="dashboard-pro-rise mt-4" style={{ animationDelay: '0.08s' }}>
                        <div className="rounded-xl border border-cyan-300/25 bg-cyan-500/10 px-4 py-3 text-sm text-cyan-100">
                            {pgSyncMessage}
                        </div>
                    </section>
                )}

                <section className="dashboard-pro-rise mt-5" style={{ animationDelay: '0.1s' }}>
                    <div className="dashboard-panel">
                        <div className="dashboard-panel-title mb-3">筛选器</div>
                        <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
                            <label className="text-xs text-slate-300">
                                班级筛选
                                <select
                                    value={classFilter}
                                    onChange={(e) => setClassFilter(e.target.value)}
                                    className="mt-1 h-10 w-full rounded-lg border border-white/15 bg-slate-950/40 px-3 text-sm text-slate-100 outline-none focus:border-cyan-300/50"
                                >
                                    <option value="all">全部班级</option>
                                    {classOptions.map((classKey) => (
                                        <option key={classKey} value={classKey}>
                                            {prettyClassLabel(classKey)}
                                        </option>
                                    ))}
                                </select>
                            </label>

                            <label className="text-xs text-slate-300">
                                时间筛选
                                <select
                                    value={dateFilter}
                                    onChange={(e) => setDateFilter(e.target.value as DateFilter)}
                                    className="mt-1 h-10 w-full rounded-lg border border-white/15 bg-slate-950/40 px-3 text-sm text-slate-100 outline-none focus:border-cyan-300/50"
                                >
                                    {(Object.keys(DATE_FILTER_LABEL) as DateFilter[]).map((item) => (
                                        <option key={item} value={item}>{DATE_FILTER_LABEL[item]}</option>
                                    ))}
                                </select>
                            </label>

                            <label className="text-xs text-slate-300 md:col-span-2">
                                搜索
                                <div className="mt-1 h-10 rounded-lg border border-white/15 bg-slate-950/40 px-3 flex items-center gap-2">
                                    <Search className="w-4 h-4 text-slate-400" />
                                    <input
                                        value={searchTerm}
                                        onChange={(e) => setSearchTerm(e.target.value)}
                                        placeholder="学生姓名 / 任务ID / 文件名"
                                        className="w-full bg-transparent text-sm text-slate-100 placeholder:text-slate-500 outline-none"
                                    />
                                </div>
                            </label>
                        </div>
                    </div>
                </section>

                {error && (
                    <section className="dashboard-pro-rise mt-4" style={{ animationDelay: '0.14s' }}>
                        <div className="rounded-xl border border-red-400/30 bg-red-500/10 px-4 py-3 text-sm text-red-200 flex items-center gap-2">
                            <AlertCircle className="w-4 h-4" />
                            {error}
                        </div>
                    </section>
                )}

                <section className="dashboard-pro-rise mt-5 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3" style={{ animationDelay: '0.16s' }}>
                    <div className="dashboard-kpi-card">
                        <div className="dashboard-kpi-title"><ChartColumnBig className="w-4 h-4" />报告总数</div>
                        <div className="dashboard-kpi-value">{stats.total}</div>
                        <div className="dashboard-kpi-sub">已评分 {stats.scored}</div>
                    </div>
                    <div className="dashboard-kpi-card">
                        <div className="dashboard-kpi-title"><Sparkles className="w-4 h-4" />平均分</div>
                        <div className="dashboard-kpi-value">{stats.avgScore === null ? '--' : stats.avgScore.toFixed(1)}</div>
                        <div className="dashboard-kpi-sub">及格率 {stats.passRate === null ? '--' : `${stats.passRate.toFixed(0)}%`}</div>
                    </div>
                    <div className="dashboard-kpi-card">
                        <div className="dashboard-kpi-title"><Users className="w-4 h-4" />学生人数</div>
                        <div className="dashboard-kpi-value">{stats.studentCount}</div>
                        <div className="dashboard-kpi-sub">优秀 {stats.excellentCount} 人</div>
                    </div>
                    <div className="dashboard-kpi-card">
                        <div className="dashboard-kpi-title"><Clock3 className="w-4 h-4" />低分提醒</div>
                        <div className="dashboard-kpi-value">{stats.weakCount}</div>
                        <div className="dashboard-kpi-sub">低于 {WEAK_LINE} 分</div>
                    </div>
                </section>

                <section className="dashboard-pro-rise mt-5 grid grid-cols-1 xl:grid-cols-3 gap-3" style={{ animationDelay: '0.2s' }}>
                    <div className="dashboard-panel xl:col-span-2 overflow-hidden">
                        <div className="flex items-center justify-between mb-3">
                            <div className="dashboard-panel-title">最近报告</div>
                            <div className="text-xs text-slate-400">
                                {lastUpdatedAt ? `更新于 ${new Date(lastUpdatedAt).toLocaleTimeString()}` : ''}
                            </div>
                        </div>

                        {isLoading ? (
                            <div className="py-12 text-center text-sm text-slate-400">加载中...</div>
                        ) : recentRows.length === 0 ? (
                            <div className="py-12 text-center text-sm text-slate-400">当前筛选下暂无数据</div>
                        ) : (
                            <div className="overflow-auto">
                                <table className="min-w-full text-sm">
                                    <thead>
                                        <tr className="text-left text-slate-400 border-b border-white/10">
                                            <th className="py-2 pr-3">学生</th>
                                            <th className="py-2 pr-3">班级</th>
                                            <th className="py-2 pr-3">时间</th>
                                            <th className="py-2 pr-3 text-right">得分</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {recentRows.map((item) => {
                                            const score = toNumber(item.score);
                                            const classKey = extractClassKey(item);
                                            return (
                                                <tr key={item.id} className="border-b border-white/5 text-slate-100">
                                                    <td className="py-2 pr-3">{item.display_name || item.student_name || item.id}</td>
                                                    <td className="py-2 pr-3 text-slate-300">{prettyClassLabel(classKey)}</td>
                                                    <td className="py-2 pr-3 text-slate-300">{formatDateTime(item.timestamp)}</td>
                                                    <td className="py-2 pr-3 text-right font-semibold">
                                                        {score === null ? '--' : score.toFixed(1)}
                                                    </td>
                                                </tr>
                                            );
                                        })}
                                    </tbody>
                                </table>
                            </div>
                        )}
                    </div>

                    <div className="dashboard-panel">
                        <div className="dashboard-panel-title mb-3">班级分布</div>
                        {classDistribution.length === 0 ? (
                            <div className="py-8 text-sm text-slate-400">暂无可用数据</div>
                        ) : (
                            <div className="space-y-2">
                                {classDistribution.map((item) => {
                                    const max = classDistribution[0]?.count || 1;
                                    const pct = Math.max(8, Math.round((item.count / max) * 100));
                                    return (
                                        <div key={item.classKey}>
                                            <div className="flex items-center justify-between text-xs text-slate-300 mb-1">
                                                <span>{prettyClassLabel(item.classKey)}</span>
                                                <span>{item.count}</span>
                                            </div>
                                            <div className="h-2 rounded-full bg-white/10 overflow-hidden">
                                                <div className="h-full rounded-full bg-gradient-to-r from-orange-300 to-cyan-300" style={{ width: `${pct}%` }} />
                                            </div>
                                        </div>
                                    );
                                })}
                            </div>
                        )}
                    </div>
                </section>

                <section className="dashboard-pro-rise mt-5" style={{ animationDelay: '0.24s' }}>
                    <div className="dashboard-panel flex flex-col md:flex-row md:items-center md:justify-between gap-3">
                        <div className="text-sm text-slate-200 flex items-center gap-2">
                            <GraduationCap className="w-4 h-4 text-cyan-300" />
                            {mode === 'teacher'
                                ? '教师视图：以班级管理和分数分层为主。'
                                : '学生视图：保留同一筛选结果，强调近期练习反馈。'}
                        </div>
                        <div className="text-xs text-slate-400 flex items-center gap-1">
                            <CalendarDays className="w-4 h-4" />
                            当前筛选: {classFilter === 'all' ? '全部班级' : prettyClassLabel(classFilter)} / {DATE_FILTER_LABEL[dateFilter]}
                        </div>
                    </div>
                </section>
            </div>
        </div>
    );
}
