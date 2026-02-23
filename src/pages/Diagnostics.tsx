import { useCallback, useEffect, useMemo, useState } from 'react';
import { Activity, RefreshCw, AlertCircle, HardDrive, FileAudio, History } from 'lucide-react';
import { API_HOST } from '../config/api';

interface DiagnosticsSummaryResponse {
    status?: string;
    generated_at?: number;
    reports?: {
        count?: number;
        latest_timestamp?: number;
    };
    uploads?: {
        gb?: number;
        file_count?: number;
        linked_file_count?: number;
        orphan_file_count?: number;
        warn_gb?: number;
        over_warn?: boolean;
    };
    jobs?: {
        total?: number;
        queued?: number;
        processing?: number;
        completed?: number;
        failed?: number;
        active?: number;
    };
    failed_recent?: Array<{
        job_id?: string;
        submission_id?: string;
        timestamp?: number;
        error?: string;
    }>;
    disk?: {
        total_bytes?: number;
        used_bytes?: number;
        free_bytes?: number;
    };
}

const REQUEST_TIMEOUT_MS = 12000;

const fetchWithTimeout = async (input: RequestInfo | URL, init?: RequestInit, timeoutMs: number = REQUEST_TIMEOUT_MS): Promise<Response> => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
        return await fetch(input, {
            ...(init || {}),
            signal: controller.signal,
        });
    } finally {
        window.clearTimeout(timer);
    }
};

const toGb = (bytes: number): string => (bytes / (1024 ** 3)).toFixed(2);
const toPercent = (used: number, total: number): string => {
    if (total <= 0) return '--';
    return `${Math.round((used / total) * 100)}%`;
};

export default function DiagnosticsPage() {
    const [data, setData] = useState<DiagnosticsSummaryResponse | null>(null);
    const [isLoading, setIsLoading] = useState(true);
    const [isRefreshing, setIsRefreshing] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const fetchSummary = useCallback(async (background: boolean = false) => {
        if (!background) setIsLoading(true);
        else setIsRefreshing(true);
        setError(null);
        try {
            const response = await fetchWithTimeout(`${API_HOST}/api/diagnostics/summary?failed_limit=12`);
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            const payload = await response.json() as DiagnosticsSummaryResponse;
            setData(payload);
        } catch (err: unknown) {
            const message = err instanceof Error ? err.message : 'Failed to load diagnostics';
            setError(message);
        } finally {
            if (!background) setIsLoading(false);
            else setIsRefreshing(false);
        }
    }, []);

    useEffect(() => {
        void fetchSummary(false);
        const timer = window.setInterval(() => {
            void fetchSummary(true);
        }, 15000);
        return () => window.clearInterval(timer);
    }, [fetchSummary]);

    const generatedAtLabel = useMemo(() => {
        const ts = Number(data?.generated_at || 0);
        if (!Number.isFinite(ts) || ts <= 0) return '--';
        return new Date(ts * 1000).toLocaleString();
    }, [data?.generated_at]);

    if (isLoading) {
        return (
            <div className="pt-24 pb-20 px-8 max-w-7xl mx-auto min-h-screen">
                <div className="flex items-center gap-3 text-gray-400">
                    <RefreshCw className="w-5 h-5 animate-spin" />
                    Loading diagnostics...
                </div>
            </div>
        );
    }

    const reportsCount = Number(data?.reports?.count || 0);
    const uploadsCount = Number(data?.uploads?.file_count || 0);
    const uploadsGb = Number(data?.uploads?.gb || 0);
    const jobsTotal = Number(data?.jobs?.total || 0);
    const jobsFailed = Number(data?.jobs?.failed || 0);
    const jobsActive = Number(data?.jobs?.active || 0);
    const warnGb = Number(data?.uploads?.warn_gb || 0);
    const overWarn = Boolean(data?.uploads?.over_warn);
    const diskUsed = Number(data?.disk?.used_bytes || 0);
    const diskTotal = Number(data?.disk?.total_bytes || 0);
    const diskFree = Number(data?.disk?.free_bytes || 0);
    const failedRecent = Array.isArray(data?.failed_recent) ? data!.failed_recent! : [];

    return (
        <div className="pt-24 pb-20 px-8 max-w-7xl mx-auto min-h-screen">
            <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 mb-6">
                <div>
                    <h1 className="text-3xl font-black text-white flex items-center gap-3">
                        <Activity className="w-8 h-8 text-primary" />
                        Diagnostics
                    </h1>
                    <p className="text-gray-500 text-sm mt-1 ml-11">
                        Snapshot at {generatedAtLabel}
                    </p>
                </div>
                <button
                    onClick={() => void fetchSummary(true)}
                    disabled={isRefreshing}
                    className="h-10 min-w-[140px] px-4 rounded-lg text-sm font-bold inline-flex items-center justify-center gap-2 bg-cyan-500/10 border border-cyan-400/20 text-cyan-200 hover:bg-cyan-500/20 disabled:opacity-60"
                >
                    <RefreshCw className={`w-4 h-4 ${isRefreshing ? 'animate-spin' : ''}`} />
                    {isRefreshing ? 'Refreshing...' : 'Refresh'}
                </button>
            </div>

            {error && (
                <div className="mb-6 rounded-xl border border-red-400/25 bg-red-500/10 px-4 py-3 text-sm text-red-200">
                    {error}
                </div>
            )}

            {overWarn && (
                <div className="mb-6 rounded-xl border border-red-400/30 bg-red-500/10 px-4 py-3 text-sm">
                    <div className="text-red-200 font-bold">Upload storage over warning threshold</div>
                    <div className="text-red-200/80 text-xs mt-1">
                        Current {uploadsGb.toFixed(2)}GB / {warnGb.toFixed(2)}GB
                    </div>
                </div>
            )}

            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4 mb-6">
                <div className="rounded-xl border border-white/10 bg-white/5 p-4">
                    <div className="text-xs uppercase tracking-wider text-gray-400">Reports</div>
                    <div className="mt-2 text-3xl font-black text-white">{reportsCount}</div>
                </div>
                <div className="rounded-xl border border-white/10 bg-white/5 p-4">
                    <div className="text-xs uppercase tracking-wider text-gray-400 flex items-center gap-2">
                        <FileAudio className="w-4 h-4" />
                        Upload Audio
                    </div>
                    <div className="mt-2 text-3xl font-black text-white">{uploadsCount}</div>
                    <div className="text-xs text-gray-500 mt-1">{uploadsGb.toFixed(2)}GB</div>
                </div>
                <div className="rounded-xl border border-white/10 bg-white/5 p-4">
                    <div className="text-xs uppercase tracking-wider text-gray-400">Jobs</div>
                    <div className="mt-2 text-3xl font-black text-white">{jobsTotal}</div>
                    <div className="text-xs text-gray-500 mt-1">Active {jobsActive} / Failed {jobsFailed}</div>
                </div>
                <div className="rounded-xl border border-white/10 bg-white/5 p-4">
                    <div className="text-xs uppercase tracking-wider text-gray-400 flex items-center gap-2">
                        <HardDrive className="w-4 h-4" />
                        Disk
                    </div>
                    <div className="mt-2 text-3xl font-black text-white">{toPercent(diskUsed, diskTotal)}</div>
                    <div className="text-xs text-gray-500 mt-1">Free {toGb(diskFree)}GB</div>
                </div>
            </div>

            <div className="rounded-xl border border-white/10 bg-[#1e1e24] overflow-hidden">
                <div className="px-4 py-3 border-b border-white/10 text-sm font-bold text-gray-200 flex items-center gap-2">
                    <AlertCircle className="w-4 h-4 text-red-300" />
                    Recent Failed Jobs
                </div>
                {failedRecent.length === 0 ? (
                    <div className="px-4 py-6 text-sm text-gray-500">No recent failed jobs.</div>
                ) : (
                    <div className="divide-y divide-white/5">
                        {failedRecent.map((row, idx) => {
                            const ts = Number(row?.timestamp || 0);
                            const tsLabel = ts > 0 ? new Date(ts * 1000).toLocaleString() : '--';
                            const sid = String(row?.submission_id || '').trim();
                            const err = String(row?.error || '').trim() || 'No error message';
                            return (
                                <div key={`${sid}-${idx}`} className="px-4 py-3 text-sm">
                                    <div className="flex flex-wrap items-center gap-3 text-xs text-gray-400 mb-1">
                                        <span className="inline-flex items-center gap-1"><History className="w-3 h-3" /> {tsLabel}</span>
                                        <span className="font-mono text-gray-500">SID: {sid || '--'}</span>
                                    </div>
                                    <div className="text-red-200/90 break-all">{err}</div>
                                </div>
                            );
                        })}
                    </div>
                )}
            </div>
        </div>
    );
}
