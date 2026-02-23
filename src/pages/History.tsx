import { useState, useEffect, useRef } from 'react';
import { History, Search, Calendar, ExternalLink, Loader2, AlertCircle, TrendingUp, Trash2, RefreshCw, Copy, Clock3 } from 'lucide-react';
import { API_HOST } from '../config/api';

interface Report {
    id: string;
    url: string;
    timestamp: number;
    student_name: string;
    display_name?: string;
    original_filename?: string;
    score?: number;
    status?: string;
    job_id?: string;
    error?: string;
}

interface ReportListResponse {
    items: Report[];
    total: number;
    page: number;
    page_size: number;
    total_pages: number;
    has_prev: boolean;
    has_next: boolean;
}

interface JobItem {
    id: string;
    status: string;
    submission_id: string;
    student_id?: string;
    filename?: string;
    timestamp: number;
    error?: string;
}

interface BatchRetryProgress {
    total: number;
    done: number;
    queued: number;
    failed: number;
}

interface BatchRetryFailureItem {
    id: string;
    error: string;
}

interface BatchRetrySummary {
    runAt: number;
    targetCount: number;
    processed: number;
    queued: number;
    failed: number;
    skipped: number;
    stopped: boolean;
    successIds: string[];
    failedItems: BatchRetryFailureItem[];
}

interface CleanupFailedResult {
    kind: 'success' | 'empty' | 'error';
    runAt: number;
    hours: number;
    matched: number;
    deleted: number;
    message: string;
}

interface JobStats {
    queued: number;
    processing: number;
    failed: number;
    completed: number;
    total: number;
    active: number;
}

interface JobOverviewResponse {
    status?: string;
    stats?: Partial<JobStats>;
    active_jobs?: JobItem[];
    failed_jobs?: JobItem[];
    timestamp?: number;
}

interface UploadStorageUsageResponse {
    uploads_file_count?: number;
    uploads_gb?: number;
    linked_audio_file_count?: number;
    orphan_audio_file_count?: number;
    warn_gb?: number;
    over_warn?: boolean;
}

interface ConfirmDialogState {
    title: string;
    message: string;
    confirmLabel: string;
    tone: 'danger' | 'primary';
}

type BackendHealthState = 'checking' | 'ok' | 'down';
type HistoryStatusFilter = 'all' | 'queued' | 'processing' | 'failed' | 'completed';
type HistoryDateRangeFilter = 'all' | 'today' | '7d';

const READ_REPORT_IDS_STORAGE_KEY = 'readReportIds';
const CLEANUP_FAILED_HOURS_STORAGE_KEY = 'cleanupFailedHours';
const LAST_CLEANUP_RESULT_STORAGE_KEY = 'lastCleanupResult';
const STATUS_FILTER_STORAGE_KEY = 'historyStatusFilter';
const DATE_RANGE_FILTER_STORAGE_KEY = 'historyDateRangeFilter';
const SEARCH_TERM_STORAGE_KEY = 'historySearchTerm';
const MAX_READ_REPORT_IDS = 2000;
const DEFAULT_REQUEST_TIMEOUT_MS = 15000;
const HEALTH_REQUEST_TIMEOUT_MS = 5000;
const REFRESH_COOLDOWN_MS = 2000;
const CLEANUP_HOUR_PRESETS = ['24', '72', '168'] as const;
const DEFAULT_CLEANUP_HOURS = '24';
const MAX_SEARCH_TERM_LENGTH = 120;
const STORAGE_NEAR_WARN_RATIO = 0.85;

const normalizeReadReportIds = (raw: unknown): string[] => {
    const list = Array.isArray(raw) ? raw : [];
    const seen = new Set<string>();
    const normalized: string[] = [];
    for (const item of list) {
        const id = String(item || '').trim();
        if (!id || seen.has(id)) continue;
        seen.add(id);
        normalized.push(id);
    }
    if (normalized.length > MAX_READ_REPORT_IDS) {
        return normalized.slice(normalized.length - MAX_READ_REPORT_IDS);
    }
    return normalized;
};

const normalizeCleanupResult = (raw: unknown): CleanupFailedResult | null => {
    if (!raw || typeof raw !== 'object') return null;
    const source = raw as Partial<CleanupFailedResult>;
    const kind = source.kind;
    if (kind !== 'success' && kind !== 'empty' && kind !== 'error') return null;
    const runAt = Number(source.runAt);
    const hours = Number(source.hours);
    const matched = Number(source.matched);
    const deleted = Number(source.deleted);
    const message = String(source.message || '').trim();
    if (!Number.isFinite(runAt) || runAt <= 0) return null;
    if (!Number.isFinite(hours) || hours < 0) return null;
    if (!Number.isFinite(matched) || matched < 0) return null;
    if (!Number.isFinite(deleted) || deleted < 0) return null;
    if (!message) return null;
    return { kind, runAt, hours, matched, deleted, message };
};

const normalizeStatusFilter = (raw: unknown): HistoryStatusFilter => {
    const value = String(raw || '').trim().toLowerCase();
    if (value === 'queued' || value === 'processing' || value === 'failed' || value === 'completed') {
        return value;
    }
    return 'all';
};

const normalizeDateRangeFilter = (raw: unknown): HistoryDateRangeFilter => {
    const value = String(raw || '').trim().toLowerCase();
    if (value === 'today' || value === '7d') return value;
    return 'all';
};

const normalizeSearchTerm = (raw: unknown): string => {
    return String(raw || '').slice(0, MAX_SEARCH_TERM_LENGTH).trim();
};

const normalizeCleanupHours = (raw: unknown): string => {
    const value = String(raw || '').trim();
    if (CLEANUP_HOUR_PRESETS.includes(value as typeof CLEANUP_HOUR_PRESETS[number])) {
        return value;
    }
    return DEFAULT_CLEANUP_HOURS;
};

const fetchWithTimeout = async (
    input: RequestInfo | URL,
    init?: RequestInit,
    timeoutMs: number = DEFAULT_REQUEST_TIMEOUT_MS
): Promise<Response> => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
        const mergedInit: RequestInit = {
            ...(init || {}),
            signal: controller.signal,
        };
        return await fetch(input, mergedInit);
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
};

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

export default function HistoryPage() {
    const [reports, setReports] = useState<Report[]>([]);
    const [reportTotal, setReportTotal] = useState(0);
    const [serverTotalPages, setServerTotalPages] = useState(1);
    const [isLoading, setIsLoading] = useState(true);
    const [isRefreshing, setIsRefreshing] = useState(false);
    const [isRefreshCoolingDown, setIsRefreshCoolingDown] = useState(false);
    const [refreshCooldownSeconds, setRefreshCooldownSeconds] = useState(0);
    const [lastSyncAt, setLastSyncAt] = useState<number | null>(null);
    const [backendHealth, setBackendHealth] = useState<BackendHealthState>('checking');
    const [backendHealthError, setBackendHealthError] = useState<string | null>(null);
    const [backendHealthAt, setBackendHealthAt] = useState<number | null>(null);
    const [uploadStorageStats, setUploadStorageStats] = useState<{
        fileCount: number;
        usedGb: number;
        linkedCount: number;
        orphanCount: number;
        warnGb: number;
        overWarn: boolean;
        usageRatio: number;
    } | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [copiedReportId, setCopiedReportId] = useState<string | null>(null);
    const [actionNotice, setActionNotice] = useState<{ type: 'success' | 'error' | 'info'; title: string; message: string } | null>(null);
    const [confirmDialog, setConfirmDialog] = useState<ConfirmDialogState | null>(null);
    const [rescorePendingIds, setRescorePendingIds] = useState<Set<string>>(new Set());
    const [searchTerm, setSearchTerm] = useState(() => {
        try {
            const saved = localStorage.getItem(SEARCH_TERM_STORAGE_KEY);
            return normalizeSearchTerm(saved);
        } catch {
            return "";
        }
    });
    const [searchDebounced, setSearchDebounced] = useState(() => {
        try {
            const saved = localStorage.getItem(SEARCH_TERM_STORAGE_KEY);
            return normalizeSearchTerm(saved);
        } catch {
            return "";
        }
    });
    const [currentPage, setCurrentPage] = useState(1);
    const itemsPerPage = 8;

    // State for Jobs
    const [activeJobs, setActiveJobs] = useState<JobItem[]>([]);
    const [failedJobs, setFailedJobs] = useState<JobItem[]>([]);
    const [jobStats, setJobStats] = useState<JobStats>({
        queued: 0,
        processing: 0,
        failed: 0,
        completed: 0,
        total: 0,
        active: 0,
    });
    const [statusFilter, setStatusFilter] = useState<HistoryStatusFilter>(() => {
        try {
            const saved = localStorage.getItem(STATUS_FILTER_STORAGE_KEY);
            return normalizeStatusFilter(saved);
        } catch {
            return 'all';
        }
    });
    const [dateRangeFilter, setDateRangeFilter] = useState<HistoryDateRangeFilter>(() => {
        try {
            const saved = localStorage.getItem(DATE_RANGE_FILTER_STORAGE_KEY);
            return normalizeDateRangeFilter(saved);
        } catch {
            return 'all';
        }
    });
    // Polling ref
    const pollInterval = useRef<ReturnType<typeof setInterval> | null>(null);
    const refreshCooldownTimerRef = useRef<number | null>(null);
    const refreshCooldownTickerRef = useRef<number | null>(null);
    const confirmResolverRef = useRef<((confirmed: boolean) => void) | null>(null);
    const fetchDataRef = useRef<(options?: { background?: boolean }) => Promise<void>>(async () => undefined);
    const fetchReportsRef = useRef<() => Promise<void>>(async () => undefined);
    const fetchJobsRef = useRef<() => Promise<void>>(async () => undefined);
    const fetchBackendHealthRef = useRef<() => Promise<void>>(async () => undefined);
    const fetchUploadStorageRef = useRef<() => Promise<void>>(async () => undefined);
    const startPollingRef = useRef<() => void>(() => undefined);
    const stopPollingRef = useRef<() => void>(() => undefined);

    useEffect(() => {
        const timer = setTimeout(() => {
            setCurrentPage(1);
            setSearchDebounced(searchTerm.trim());
        }, 300);
        return () => clearTimeout(timer);
    }, [searchTerm]);

    useEffect(() => {
        void fetchDataRef.current();
        // Start polling if there are active jobs
        startPollingRef.current();
        return () => stopPollingRef.current();
    }, [currentPage, searchDebounced, statusFilter, dateRangeFilter]);

    useEffect(() => {
        return () => {
            if (refreshCooldownTimerRef.current !== null) {
                window.clearTimeout(refreshCooldownTimerRef.current);
                refreshCooldownTimerRef.current = null;
            }
            if (refreshCooldownTickerRef.current !== null) {
                window.clearInterval(refreshCooldownTickerRef.current);
                refreshCooldownTickerRef.current = null;
            }
        };
    }, []);

    useEffect(() => {
        if (!copiedReportId) return;
        const timer = window.setTimeout(() => {
            setCopiedReportId(null);
        }, 1600);
        return () => window.clearTimeout(timer);
    }, [copiedReportId]);

    useEffect(() => {
        if (!actionNotice) return;
        const timer = window.setTimeout(() => {
            setActionNotice(null);
        }, 4200);
        return () => window.clearTimeout(timer);
    }, [actionNotice]);

    useEffect(() => {
        return () => {
            if (confirmResolverRef.current) {
                confirmResolverRef.current(false);
                confirmResolverRef.current = null;
            }
        };
    }, []);

    useEffect(() => {
        if (!confirmDialog) return;
        const onKeyDown = (event: KeyboardEvent) => {
            const resolver = confirmResolverRef.current;
            if (!resolver) return;
            if (event.key === 'Escape') {
                event.preventDefault();
                confirmResolverRef.current = null;
                setConfirmDialog(null);
                resolver(false);
                return;
            }
            if (event.key === 'Enter' && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey) {
                event.preventDefault();
                confirmResolverRef.current = null;
                setConfirmDialog(null);
                resolver(true);
            }
        };
        window.addEventListener('keydown', onKeyDown);
        return () => window.removeEventListener('keydown', onKeyDown);
    }, [confirmDialog]);

    const resolveConfirmDialog = (confirmed: boolean) => {
        const resolver = confirmResolverRef.current;
        confirmResolverRef.current = null;
        setConfirmDialog(null);
        if (resolver) resolver(confirmed);
    };

    const requestConfirmation = (options: ConfirmDialogState): Promise<boolean> => {
        if (confirmResolverRef.current) {
            confirmResolverRef.current(false);
            confirmResolverRef.current = null;
        }
        setConfirmDialog(options);
        return new Promise<boolean>((resolve) => {
            confirmResolverRef.current = resolve;
        });
    };

    const fetchData = async (options?: { background?: boolean }) => {
        const isBackground = !!options?.background;
        const needReports = statusFilter === 'all' || statusFilter === 'completed';
        if (!isBackground) setIsLoading(true);
        setError(null);
        let success = false;
        try {
            const tasks: Promise<void>[] = [
                fetchJobs().catch(e => console.error("Job fetch failed", e)),
                fetchBackendHealth().catch(e => console.error("Health fetch failed", e)),
                fetchUploadStorage().catch(() => undefined),
            ];
            if (needReports) {
                tasks.unshift(fetchReports().catch(e => console.error("Report fetch failed", e)));
            }
            await Promise.all(tasks);
            success = true;
        } catch (err: unknown) {
            setError(getErrorMessage(err, 'Failed to load history data'));
        } finally {
            if (!isBackground) setIsLoading(false);
            if (success) setLastSyncAt(Date.now());
        }
    };

    const fetchBackendHealth = async () => {
        try {
            const response = await fetchWithTimeout(`${API_HOST}/api/health`, undefined, HEALTH_REQUEST_TIMEOUT_MS);
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            const payload = await response.json().catch(() => null);
            if (String(payload?.status || '').toLowerCase() !== 'ok') {
                throw new Error('Unexpected health payload');
            }
            setBackendHealth('ok');
            setBackendHealthError(null);
            setBackendHealthAt(Date.now());
        } catch (err: unknown) {
            setBackendHealth('down');
            setBackendHealthError(getErrorMessage(err, 'Health check failed'));
            setBackendHealthAt(Date.now());
        }
    };

    const fetchUploadStorage = async () => {
        try {
            const response = await fetchWithTimeout(`${API_HOST}/api/storage/uploads-usage`, undefined, HEALTH_REQUEST_TIMEOUT_MS);
            if (!response.ok) return;
            const payload = await response.json() as UploadStorageUsageResponse;
            const fileCount = Number(payload?.uploads_file_count ?? 0);
            const usedGb = Number(payload?.uploads_gb ?? 0);
            const linkedCount = Number(payload?.linked_audio_file_count ?? 0);
            const orphanCount = Number(payload?.orphan_audio_file_count ?? 0);
            const warnGb = Number(payload?.warn_gb ?? 0);
            const overWarn = Boolean(payload?.over_warn);
            if (!Number.isFinite(fileCount) || !Number.isFinite(usedGb) || !Number.isFinite(linkedCount) || !Number.isFinite(orphanCount) || !Number.isFinite(warnGb)) return;
            const safeWarnGb = Math.max(0, warnGb);
            const usageRatio = safeWarnGb > 0 ? (usedGb / safeWarnGb) : 0;
            setUploadStorageStats({
                fileCount: Math.max(0, Math.trunc(fileCount)),
                usedGb: Math.max(0, usedGb),
                linkedCount: Math.max(0, Math.trunc(linkedCount)),
                orphanCount: Math.max(0, Math.trunc(orphanCount)),
                warnGb: safeWarnGb,
                overWarn,
                usageRatio: Number.isFinite(usageRatio) ? Math.max(0, usageRatio) : 0,
            });
        } catch {
            // Ignore transient storage fetch failures.
        }
    };

    const fetchReports = async () => {
        const params = new URLSearchParams();
        params.set('page', String(currentPage));
        params.set('page_size', String(itemsPerPage));
        if (searchDebounced) params.set('search', searchDebounced);
        if (dateRangeFilter !== 'all') params.set('date_range', dateRangeFilter);

        const response = await fetchWithTimeout(`${API_HOST}/api/reports?${params.toString()}`);
        if (!response.ok) throw new Error('Failed to fetch reports');
        const data = await response.json();
        if (Array.isArray(data)) {
            setReports(data);
            setReportTotal(data.length);
            setServerTotalPages(1);
            return;
        }
        const payload = data as Partial<ReportListResponse>;
        setReports(Array.isArray(payload.items) ? payload.items : []);
        setReportTotal(Number(payload.total || 0));
        setServerTotalPages(Math.max(1, Number(payload.total_pages || 1)));
        const resolvedPage = Math.max(1, Number(payload.page || 1));
        if (resolvedPage !== currentPage) {
            setCurrentPage(resolvedPage);
        }
        setLastSyncAt(Date.now());
    };

    const applyJobSnapshot = (
        activeData: JobItem[],
        failedData: JobItem[],
        statsLike?: Partial<JobStats>
    ) => {
        setActiveJobs(activeData);
        setFailedJobs(failedData);

        const queuedFallback = activeData.filter((j) => j.status === 'queued').length;
        const processingFallback = activeData.filter((j) => j.status === 'processing').length;
        const failedFallback = failedData.length;
        const queued = Number(statsLike?.queued ?? queuedFallback);
        const processing = Number(statsLike?.processing ?? processingFallback);
        const failed = Number(statsLike?.failed ?? failedFallback);
        const completed = Number(statsLike?.completed ?? 0);
        const total = Number(statsLike?.total ?? (queued + processing + failed + completed));
        const active = Number(statsLike?.active ?? (queued + processing));

        setJobStats({
            queued,
            processing,
            failed,
            completed,
            total,
            active,
        });

        if (active > 0) {
            startPolling();
        } else {
            stopPolling();
        }
    };

    const fetchJobs = async () => {
        try {
            const overviewResponse = await fetchWithTimeout(`${API_HOST}/api/jobs/overview?active_limit=200&failed_limit=500`);
            if (overviewResponse.ok) {
                const payload = await overviewResponse.json() as JobOverviewResponse;
                const activeData = Array.isArray(payload?.active_jobs) ? payload.active_jobs : [];
                const failedData = Array.isArray(payload?.failed_jobs) ? payload.failed_jobs : [];
                applyJobSnapshot(activeData, failedData, payload?.stats || {});
                return;
            }

            // Fallback to legacy multi-call path.
            const [statsResponse, activeResponse, failedResponse] = await Promise.all([
                fetchWithTimeout(`${API_HOST}/api/jobs/stats`),
                fetchWithTimeout(`${API_HOST}/api/jobs?status=active&limit=200`),
                fetchWithTimeout(`${API_HOST}/api/jobs?status=failed&limit=500`),
            ]);
            const activeData = activeResponse.ok ? (await activeResponse.json() as JobItem[]) : [];
            const failedData = failedResponse.ok ? (await failedResponse.json() as JobItem[]) : [];
            const statsData = statsResponse.ok ? await statsResponse.json() : {};
            applyJobSnapshot(activeData, failedData, statsData || {});
        } catch (e) {
            console.error("Failed to fetch jobs", e);
        }
    };

    const startPolling = () => {
        if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
        if (pollInterval.current) return;
        pollInterval.current = setInterval(async () => {
            if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
            const needReports = statusFilter === 'all' || statusFilter === 'completed';
            // Background refresh
            const tasks: Promise<unknown>[] = [
                fetchJobs().catch(() => null),
                fetchBackendHealth().catch(() => null),
                fetchUploadStorage().catch(() => null),
            ];
            if (needReports) {
                tasks.unshift(fetchReports().catch(() => null));
            }
            await Promise.all(tasks);
        }, 3000); // 3 seconds
    };

    const stopPolling = () => {
        if (pollInterval.current) {
            clearInterval(pollInterval.current);
            pollInterval.current = null;
        }
    };
    fetchDataRef.current = fetchData;
    fetchReportsRef.current = fetchReports;
    fetchJobsRef.current = fetchJobs;
    fetchBackendHealthRef.current = fetchBackendHealth;
    fetchUploadStorageRef.current = fetchUploadStorage;
    startPollingRef.current = startPolling;
    stopPollingRef.current = stopPolling;

    const handleManualRefresh = async () => {
        if (isRefreshing || isRefreshCoolingDown) return;
        setIsRefreshCoolingDown(true);
        const cooldownEndsAt = Date.now() + REFRESH_COOLDOWN_MS;
        setRefreshCooldownSeconds(Math.max(1, Math.ceil(REFRESH_COOLDOWN_MS / 1000)));
        if (refreshCooldownTimerRef.current !== null) {
            window.clearTimeout(refreshCooldownTimerRef.current);
            refreshCooldownTimerRef.current = null;
        }
        if (refreshCooldownTickerRef.current !== null) {
            window.clearInterval(refreshCooldownTickerRef.current);
            refreshCooldownTickerRef.current = null;
        }
        refreshCooldownTimerRef.current = window.setTimeout(() => {
            setIsRefreshCoolingDown(false);
            setRefreshCooldownSeconds(0);
            refreshCooldownTimerRef.current = null;
            if (refreshCooldownTickerRef.current !== null) {
                window.clearInterval(refreshCooldownTickerRef.current);
                refreshCooldownTickerRef.current = null;
            }
        }, REFRESH_COOLDOWN_MS);
        refreshCooldownTickerRef.current = window.setInterval(() => {
            const msLeft = cooldownEndsAt - Date.now();
            if (msLeft <= 0) {
                setRefreshCooldownSeconds(0);
                if (refreshCooldownTickerRef.current !== null) {
                    window.clearInterval(refreshCooldownTickerRef.current);
                    refreshCooldownTickerRef.current = null;
                }
                return;
            }
            setRefreshCooldownSeconds(Math.max(1, Math.ceil(msLeft / 1000)));
        }, 200);
        setIsRefreshing(true);
        try {
            await fetchData({ background: true });
        } finally {
            setIsRefreshing(false);
        }
    };

    const triggerSearchNow = () => {
        setCurrentPage(1);
        setSearchDebounced(searchTerm.trim());
    };

    const clearSearchNow = () => {
        if (!searchTerm && !searchDebounced) return;
        setSearchTerm("");
        setSearchDebounced("");
        setCurrentPage(1);
    };

    const handleResetView = () => {
        const hasOverrides = statusFilter !== 'all' || dateRangeFilter !== 'all' || !!searchTerm || !!searchDebounced;
        if (!hasOverrides && !isSelectionMode) return;
        setStatusFilter('all');
        setDateRangeFilter('all');
        clearSearchNow();
        setCurrentPage(1);
        setIsSelectionMode(false);
        setSelectedIds(new Set());
    };

    const handleSearchKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            triggerSearchNow();
            return;
        }
        if (e.key === 'Escape') {
            e.preventDefault();
            if (searchTerm || searchDebounced) {
                clearSearchNow();
                return;
            }
            if (statusFilter !== 'all' || dateRangeFilter !== 'all' || isSelectionMode) {
                handleResetView();
            }
        }
    };

    useEffect(() => {
        const onGlobalKeyDown = (event: KeyboardEvent) => {
            const key = String(event.key || '').toLowerCase();
            const isSlash = key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey;
            const isCommandPaletteStyle = key === 'k' && (event.ctrlKey || event.metaKey) && !event.altKey;
            if (!isSlash && !isCommandPaletteStyle) return;
            if (confirmDialog) return;
            const target = event.target as HTMLElement | null;
            const tagName = String(target?.tagName || '').toLowerCase();
            const isTypingTarget = !!target && (target.isContentEditable || tagName === 'input' || tagName === 'textarea' || tagName === 'select');
            if (isTypingTarget) return;
            event.preventDefault();
            searchInputRef.current?.focus();
            searchInputRef.current?.select();
        };
        window.addEventListener('keydown', onGlobalKeyDown);
        return () => window.removeEventListener('keydown', onGlobalKeyDown);
    }, [confirmDialog]);

    const switchStatusFilter = (next: HistoryStatusFilter) => {
        setStatusFilter((prev) => (prev === next ? 'all' : next));
        setCurrentPage(1);
        setIsSelectionMode(false);
        setSelectedIds(new Set());
    };

    const handleToggleFailedView = () => {
        switchStatusFilter(statusFilter === 'failed' ? 'all' : 'failed');
    };

    const isFailedOnly = statusFilter === 'failed';
    const isJobOnlyView = statusFilter === 'failed' || statusFilter === 'queued' || statusFilter === 'processing';
    const usesReportPagination = statusFilter === 'all' || statusFilter === 'completed';
    const hasActiveViewOverrides = statusFilter !== 'all' || dateRangeFilter !== 'all' || !!searchTerm || !!searchDebounced;

    const isWithinDateRange = (timestampSec: number): boolean => {
        if (dateRangeFilter === 'all') return true;
        const ts = Number(timestampSec || 0);
        if (!Number.isFinite(ts) || ts <= 0) return false;
        const tsMs = ts * 1000;
        const nowMs = Date.now();
        if (dateRangeFilter === '7d') {
            return tsMs >= nowMs - (7 * 24 * 60 * 60 * 1000);
        }
        const now = new Date(nowMs);
        const d = new Date(tsMs);
        return d.getUTCFullYear() === now.getUTCFullYear()
            && d.getUTCMonth() === now.getUTCMonth()
            && d.getUTCDate() === now.getUTCDate();
    };

    // Merge Reports and Jobs for display
    // Logic: 
    // 1. Create a map of active jobs by submission_id
    // 2. Filter out jobs that are already in reports (completed)
    // 3. Combine list

    // We treat "Job" as a potential "Report" locally for display
    const mergedList = [...reports];
    let activeJobsOnTop = 0;

    if (currentPage === 1 && (statusFilter === 'all' || statusFilter === 'queued' || statusFilter === 'processing')) {
        activeJobs.forEach(job => {
            if (statusFilter === 'queued' && job.status !== 'queued') return;
            if (statusFilter === 'processing' && job.status !== 'processing') return;
            if (!isWithinDateRange(job.timestamp)) return;
            // If job status is COMPLETED, it *should* be in reports. If not yet, we can optionally show it as "Finalizing..."
            // Or if status is QUEUED/PROCESSING, definitely show.
            // We use submission_id to dedupe. Job.submission_id vs Report.id
            const exists = reports.some(r => r.id === job.submission_id);
            if (!exists) {
                if (searchDebounced) {
                    const query = searchDebounced.toLowerCase();
                    const blob = [
                        String(job.submission_id || ''),
                        String(job.student_id || ''),
                        String(job.filename || ''),
                    ].join(' ').toLowerCase();
                    if (!blob.includes(query)) return;
                }
                // Mock a report object for display
                mergedList.unshift({
                    id: job.submission_id,
                    url: '#', // No link yet
                    timestamp: job.timestamp,
                    student_name: String(job.student_id || job.submission_id || 'unknown'),
                    display_name: job.filename ? String(job.filename).replace(/\.[^/.]+$/, '') : job.student_id,
                    original_filename: job.filename || undefined,
                    score: undefined, // Signal for Spinner
                    status: job.status, // Custom field for our UI
                    job_id: job.id
                });
                activeJobsOnTop += 1;
            }
        });
    }

    // Sort again because we inserted at top but timestamps might vary
    mergedList.sort((a, b) => b.timestamp - a.timestamp);


    const filteredFailedJobs = failedJobs
        .filter((job) => isWithinDateRange(job.timestamp))
        .filter((job) => {
            if (!searchDebounced) return true;
            const query = searchDebounced.toLowerCase();
            const blob = [
                String(job.submission_id || ''),
                String(job.student_id || ''),
                String(job.filename || ''),
                String(job.error || ''),
            ].join(' ').toLowerCase();
            return blob.includes(query);
        })
        .map((job) => ({
            id: String(job.submission_id || job.id || ''),
            url: '#',
            timestamp: Number(job.timestamp || 0),
            student_name: String(job.student_id || 'unknown'),
            display_name: job.filename ? String(job.filename).replace(/\.[^/.]+$/, '') : String(job.submission_id || job.id || 'failed job'),
            original_filename: job.filename || undefined,
            score: undefined,
            status: 'failed',
            error: String(job.error || ''),
        } as Report & { error?: string }));

    const filteredQueuedJobs = activeJobs
        .filter((job) => job.status === 'queued')
        .filter((job) => isWithinDateRange(job.timestamp))
        .filter((job) => {
            if (!searchDebounced) return true;
            const query = searchDebounced.toLowerCase();
            const blob = [
                String(job.submission_id || ''),
                String(job.student_id || ''),
                String(job.filename || ''),
            ].join(' ').toLowerCase();
            return blob.includes(query);
        })
        .map((job) => ({
            id: String(job.submission_id || job.id || ''),
            url: '#',
            timestamp: Number(job.timestamp || 0),
            student_name: String(job.student_id || 'unknown'),
            display_name: job.filename ? String(job.filename).replace(/\.[^/.]+$/, '') : String(job.submission_id || job.id || 'queued job'),
            original_filename: job.filename || undefined,
            score: undefined,
            status: 'queued',
            error: String(job.error || ''),
        } as Report & { error?: string }));

    const filteredProcessingJobs = activeJobs
        .filter((job) => job.status === 'processing')
        .filter((job) => isWithinDateRange(job.timestamp))
        .filter((job) => {
            if (!searchDebounced) return true;
            const query = searchDebounced.toLowerCase();
            const blob = [
                String(job.submission_id || ''),
                String(job.student_id || ''),
                String(job.filename || ''),
            ].join(' ').toLowerCase();
            return blob.includes(query);
        })
        .map((job) => ({
            id: String(job.submission_id || job.id || ''),
            url: '#',
            timestamp: Number(job.timestamp || 0),
            student_name: String(job.student_id || 'unknown'),
            display_name: job.filename ? String(job.filename).replace(/\.[^/.]+$/, '') : String(job.submission_id || job.id || 'processing job'),
            original_filename: job.filename || undefined,
            score: undefined,
            status: 'processing',
            error: String(job.error || ''),
        } as Report & { error?: string }));

    const statusFilteredReports =
        statusFilter === 'failed'
            ? filteredFailedJobs
            : statusFilter === 'queued'
                ? filteredQueuedJobs
                : statusFilter === 'processing'
                    ? filteredProcessingJobs
                    : statusFilter === 'completed'
                        ? reports
                        : mergedList;
    const filteredReports = statusFilteredReports.filter((row) => isWithinDateRange(Number(row.timestamp || 0)));

    const formatTimestamp = (ts: number) => {
        return new Date(ts * 1000).toLocaleString();
    };

    // Helper for level text
    const getLevelInfo = (score?: number, status?: string) => {
        if (status === 'queued') return { label: 'Queued', color: 'text-slate-300', borderColor: 'border-slate-500' };
        if (status === 'processing') return { label: 'Analyzing...', color: 'text-cyan-300', borderColor: 'border-cyan-500' };
        if (status === 'failed') return { label: 'Failed', color: 'text-red-400', borderColor: 'border-red-500' };

        if (!score) return { label: 'Pending', color: 'text-gray-500', borderColor: 'border-gray-700' };
        if (score >= 90) return { label: 'Native Like', color: 'text-purple-400', borderColor: 'border-purple-500' };
        if (score >= 80) return { label: 'Advanced', color: 'text-green-400', borderColor: 'border-green-500' };
        if (score >= 60) return { label: 'Intermediate', color: 'text-yellow-400', borderColor: 'border-yellow-500' };
        return { label: 'Beginner', color: 'text-red-400', borderColor: 'border-red-500' };
    };

    // Server-side pagination
    const totalPages = usesReportPagination ? Math.max(1, serverTotalPages) : 1;
    const paginatedReports = filteredReports;

    const CircularScore = ({ score, status }: { score?: number, status?: string }) => {
        if (status === 'queued') {
            return (
                <div className="relative w-16 h-16 flex items-center justify-center">
                    <Clock3 className="w-8 h-8 text-slate-300" />
                </div>
            );
        }
        if (status === 'processing') {
            return (
                <div className="relative w-16 h-16 flex items-center justify-center">
                    <Loader2 className="w-8 h-8 text-cyan-400 animate-spin" />
                </div>
            );
        }
        if (status === 'failed') {
            return (
                <div className="relative w-16 h-16 flex items-center justify-center">
                    <AlertCircle className="w-8 h-8 text-red-500" />
                </div>
            );
        }

        const radius = 24;
        const circumference = 2 * Math.PI * radius;
        // Use 0 if score is undefined, null, or NaN
        const safeScore = (score && !isNaN(score)) ? score : 0;
        const offset = circumference - (safeScore / 100) * circumference;
        const { color } = getLevelInfo(safeScore);

        return (
            <div className="relative w-16 h-16 flex items-center justify-center">
                <svg className="w-full h-full transform -rotate-90">
                    <circle cx="32" cy="32" r={radius} stroke="currentColor" strokeWidth="4" fill="transparent" className="text-white/10" />
                    <circle
                        cx="32" cy="32" r={radius}
                        stroke="currentColor" strokeWidth="4"
                        fill="transparent"
                        strokeDasharray={circumference}
                        strokeDashoffset={offset}
                        strokeLinecap="round"
                        className={`${color} transition-all duration-1000 ease-out`}
                    />
                </svg>
                <div className="absolute inset-0 flex items-center justify-center">
                    <span className={`text-sm font-bold ${color}`}>
                        {/* Only show numeric if score > 0, else -- */}
                        {safeScore > 0 ? Math.round(safeScore) + "%" : "--"}
                    </span>
                </div>
            </div>
        );
    };

    // Batch Selection Logic
    const [isSelectionMode, setIsSelectionMode] = useState(false);
    const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
    const [isCleaningFailed, setIsCleaningFailed] = useState(false);
    const [cleanupDeleteUploads, setCleanupDeleteUploads] = useState(false);
    const [cleanupFailedHours, setCleanupFailedHours] = useState(() => {
        try {
            const saved = localStorage.getItem(CLEANUP_FAILED_HOURS_STORAGE_KEY);
            return normalizeCleanupHours(saved);
        } catch {
            return DEFAULT_CLEANUP_HOURS;
        }
    });
    const [isRetryingFailedBatch, setIsRetryingFailedBatch] = useState(false);
    const [isRetryStopRequested, setIsRetryStopRequested] = useState(false);
    const [retryProgress, setRetryProgress] = useState<BatchRetryProgress>({
        total: 0,
        done: 0,
        queued: 0,
        failed: 0,
    });
    const [lastRetrySummary, setLastRetrySummary] = useState<BatchRetrySummary | null>(null);
    const [showRetryDetails, setShowRetryDetails] = useState(false);
    const [lastCleanupResult, setLastCleanupResult] = useState<CleanupFailedResult | null>(() => {
        try {
            const saved = localStorage.getItem(LAST_CLEANUP_RESULT_STORAGE_KEY);
            if (!saved) return null;
            return normalizeCleanupResult(JSON.parse(saved));
        } catch {
            return null;
        }
    });
    const retryStopRef = useRef(false);
    const searchInputRef = useRef<HTMLInputElement | null>(null);

    // Read Status Logic (Client-side)
    const [readReportIds, setReadReportIds] = useState<Set<string>>(() => {
        try {
            const saved = localStorage.getItem(READ_REPORT_IDS_STORAGE_KEY);
            if (!saved) return new Set();
            const parsed = JSON.parse(saved);
            const normalized = normalizeReadReportIds(parsed);
            // Keep storage compact and deduplicated.
            localStorage.setItem(READ_REPORT_IDS_STORAGE_KEY, JSON.stringify(normalized));
            return new Set(normalized);
        } catch {
            return new Set();
        }
    });

    const markAsRead = (id: string) => {
        const safeId = String(id || '').trim();
        if (!safeId) return;
        setReadReportIds((prev) => {
            if (prev.has(safeId)) return prev;
            const ordered = Array.from(prev);
            ordered.push(safeId);
            if (ordered.length > MAX_READ_REPORT_IDS) {
                ordered.splice(0, ordered.length - MAX_READ_REPORT_IDS);
            }
            try {
                localStorage.setItem(READ_REPORT_IDS_STORAGE_KEY, JSON.stringify(ordered));
            } catch (err) {
                console.warn('Failed to persist readReportIds', err);
            }
            return new Set(ordered);
        });
    };

    useEffect(() => {
        const raw = normalizeCleanupHours(cleanupFailedHours);
        if (raw !== cleanupFailedHours) {
            setCleanupFailedHours(raw);
            return;
        }
        try {
            localStorage.setItem(CLEANUP_FAILED_HOURS_STORAGE_KEY, raw);
        } catch (err) {
            console.warn('Failed to persist cleanupFailedHours', err);
        }
    }, [cleanupFailedHours]);

    useEffect(() => {
        try {
            localStorage.setItem(STATUS_FILTER_STORAGE_KEY, statusFilter);
        } catch (err) {
            console.warn('Failed to persist statusFilter', err);
        }
    }, [statusFilter]);

    useEffect(() => {
        try {
            localStorage.setItem(DATE_RANGE_FILTER_STORAGE_KEY, dateRangeFilter);
        } catch (err) {
            console.warn('Failed to persist dateRangeFilter', err);
        }
    }, [dateRangeFilter]);

    useEffect(() => {
        const normalized = normalizeSearchTerm(searchTerm);
        try {
            if (!normalized) {
                localStorage.removeItem(SEARCH_TERM_STORAGE_KEY);
                return;
            }
            localStorage.setItem(SEARCH_TERM_STORAGE_KEY, normalized);
        } catch (err) {
            console.warn('Failed to persist searchTerm', err);
        }
    }, [searchTerm]);

    useEffect(() => {
        try {
            if (!lastCleanupResult) {
                localStorage.removeItem(LAST_CLEANUP_RESULT_STORAGE_KEY);
                return;
            }
            localStorage.setItem(LAST_CLEANUP_RESULT_STORAGE_KEY, JSON.stringify(lastCleanupResult));
        } catch (err) {
            console.warn('Failed to persist lastCleanupResult', err);
        }
    }, [lastCleanupResult]);

    const handleToggleSelect = (id: string) => {
        if (!isSelectionMode) return;
        const newSelected = new Set(selectedIds);
        if (newSelected.has(id)) {
            newSelected.delete(id);
        } else {
            newSelected.add(id);
        }
        setSelectedIds(newSelected);
    };

    const handleSelectAll = () => {
        if (selectedIds.size === paginatedReports.length && paginatedReports.length > 0) {
            setSelectedIds(new Set());
        } else {
            setSelectedIds(new Set(paginatedReports.map(r => r.id)));
        }
    };

    useEffect(() => {
        const handleVisibilityChange = () => {
            if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
                stopPollingRef.current();
                return;
            }
            const needReports = statusFilter === 'all' || statusFilter === 'completed';
            const tasks: Promise<unknown>[] = [
                fetchJobsRef.current().catch(() => null),
                fetchBackendHealthRef.current().catch(() => null),
                fetchUploadStorageRef.current().catch(() => null),
            ];
            if (needReports) {
                tasks.unshift(fetchReportsRef.current().catch(() => null));
            }
            Promise.all(tasks).then(() => {
                startPollingRef.current();
            });
        };

        document.addEventListener('visibilitychange', handleVisibilityChange);
        window.addEventListener('focus', handleVisibilityChange);
        return () => {
            document.removeEventListener('visibilitychange', handleVisibilityChange);
            window.removeEventListener('focus', handleVisibilityChange);
        };
    }, [currentPage, searchDebounced, statusFilter, dateRangeFilter]);

    const handleBatchDelete = async () => {
        if (selectedIds.size === 0) return;

        const confirmed = await requestConfirmation({
            title: 'Batch Delete',
            message: `Delete ${selectedIds.size} selected record(s)? This cannot be undone.`,
            confirmLabel: 'Delete',
            tone: 'danger',
        });
        if (!confirmed) {
            return;
        }

        try {
            const response = await fetch(`${API_HOST}/api/reports/batch-delete`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ids: Array.from(selectedIds) })
            });

            if (!response.ok) {
                let detail = '';
                try {
                    const payload = await response.json();
                    detail = String(payload?.detail || '').trim();
                } catch {
                    detail = '';
                }
                throw new Error(detail || `HTTP ${response.status}`);
            }
            await fetchReports();
            await fetchUploadStorage();
            setSelectedIds(new Set());
            setIsSelectionMode(false); // Exit mode after delete
        } catch (err) {
            console.error(err);
            setActionNotice({ type: 'error', title: 'Batch Delete', message: formatActionError('Batch Delete', err, 'Please retry') });
        }
    };

    // Rescore Function
    const handleRescore = async (id: string) => {
        if (rescorePendingIds.has(id)) return;
        if (backendHealth === 'down') {
            setActionNotice({ type: 'error', title: 'Rescore', message: 'API is currently down. Please retry after backend recovers.' });
            return;
        }
        const confirmed = await requestConfirmation({
            title: 'Rescore',
            message: 'Start re-evaluation with Gemini 3? A new entry with "_new01" suffix will be created.',
            confirmLabel: 'Start Rescore',
            tone: 'primary',
        });
        if (!confirmed) {
            return;
        }

        setRescorePendingIds((prev) => {
            const next = new Set(prev);
            next.add(id);
            return next;
        });
        try {
            // OPTIMISTIC UPDATE or just background refresh. 
            // Do NOT set global loading state to avoid UI flash/freeze.

            const response = await fetch(`${API_HOST}/api/jobs/${id}/rescore`, {
                method: 'POST'
            });

            if (!response.ok) {
                let detail = '';
                try {
                    const payload = await response.json();
                    detail = String(payload?.detail || '').trim();
                } catch {
                    detail = '';
                }
                throw new Error(detail || 'Rescore failed');
            }

            setActionNotice({ type: 'success', title: 'Rescore', message: 'Re-evaluation started. The new report will appear in the list shortly.' });

            // Trigger immediate job fetch to see the new queued item
            void fetchJobs();
            // Ensure polling is active
            startPolling();

        } catch (err: unknown) {
            console.error(err);
            setActionNotice({ type: 'error', title: 'Rescore', message: formatActionError('Rescore', err, 'Unknown error') });
        } finally {
            setRescorePendingIds((prev) => {
                const next = new Set(prev);
                next.delete(id);
                return next;
            });
        }
    };

    const copyReportId = async (id: string) => {
        const value = String(id || '').trim();
        if (!value) return;
        try {
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(value);
            } else {
                const temp = document.createElement('textarea');
                temp.value = value;
                temp.style.position = 'fixed';
                temp.style.left = '-9999px';
                document.body.appendChild(temp);
                temp.focus();
                temp.select();
                document.execCommand('copy');
                document.body.removeChild(temp);
            }
            setCopiedReportId(value);
        } catch (err) {
            console.error('Failed to copy report id', err);
            setActionNotice({ type: 'error', title: 'Copy ID', message: formatActionError('Copy ID', err, 'Clipboard unavailable') });
        }
    };

    const handleCleanupFailed = async () => {
        if (isCleaningFailed) return;
        if (backendHealth === 'down') {
            setActionNotice({ type: 'error', title: 'Cleanup Failed', message: 'API is currently down. Please retry after backend recovers.' });
            return;
        }
        const hours = Number(normalizeCleanupHours(cleanupFailedHours));
        const deleteUploads = cleanupDeleteUploads;
        const cleanupQuery = `status=failed&older_than_hours=${encodeURIComponent(String(hours))}&limit=5000&delete_uploads=${deleteUploads ? 'true' : 'false'}`;
        setIsCleaningFailed(true);
        try {
            const previewRes = await fetch(
                `${API_HOST}/api/jobs/cleanup?${cleanupQuery}&dry_run=true`,
                { method: 'POST' }
            );
            const preview = await previewRes.json();
            if (!previewRes.ok) {
                throw new Error(preview?.detail || 'Cleanup preview failed');
            }

            const matched = Number(preview?.matched_count || 0);
            const targetDeleteCount = Number(preview?.target_delete_count ?? matched);
            const previewUploadDelete = Number(preview?.upload_deleted_count || 0);
            if (targetDeleteCount <= 0) {
                const message = `No failed jobs older than ${hours} hour(s).`;
                setLastCleanupResult({
                    kind: 'empty',
                    runAt: Date.now(),
                    hours,
                    matched: 0,
                    deleted: 0,
                    message,
                });
                setActionNotice({ type: 'info', title: 'Cleanup Failed', message });
                return;
            }

            const confirmed = await requestConfirmation({
                title: 'Cleanup Failed Jobs',
                message: deleteUploads
                    ? `Delete ${targetDeleteCount} failed job record(s) older than ${hours} hour(s) and remove about ${previewUploadDelete} original audio file(s)?${matched > targetDeleteCount ? ` (Total matched ${matched}, capped by limit 5000)` : ''}`
                    : `Delete ${targetDeleteCount} failed job record(s) older than ${hours} hour(s)?${matched > targetDeleteCount ? ` (Total matched ${matched}, capped by limit 5000)` : ''}`,
                confirmLabel: 'Delete Records',
                tone: 'danger',
            });
            if (!confirmed) {
                return;
            }

            const response = await fetch(
                `${API_HOST}/api/jobs/cleanup?${cleanupQuery}`,
                {
                method: 'POST',
                }
            );
            const payload = await response.json();
            if (!response.ok) {
                throw new Error(payload?.detail || 'Cleanup failed');
            }
            const deleted = Number(payload?.deleted_count || 0);
            const uploadDeleted = Number(payload?.upload_deleted_count || 0);
            const kind: CleanupFailedResult['kind'] = deleted > 0 ? 'success' : 'empty';
            const message = deleted > 0
                ? `Cleanup complete. Deleted ${deleted} failed job records.${deleteUploads ? ` Removed ${uploadDeleted} upload file(s).` : ''}`
                : `Cleanup finished. No failed job records were deleted.`;
            setLastCleanupResult({
                kind,
                runAt: Date.now(),
                hours,
                matched,
                deleted,
                message,
            });
            setActionNotice({ type: deleted > 0 ? 'success' : 'info', title: 'Cleanup Failed', message });
            await fetchJobs();
            await fetchUploadStorage();
        } catch (err: unknown) {
            console.error(err);
            const message = formatActionError('Cleanup Failed', err, 'Unknown error');
            setLastCleanupResult({
                kind: 'error',
                runAt: Date.now(),
                hours,
                matched: 0,
                deleted: 0,
                message,
            });
            setActionNotice({ type: 'error', title: 'Cleanup Failed', message });
        } finally {
            setIsCleaningFailed(false);
        }
    };

    const failedSubmissionIds = Array.from(
        new Set(
            failedJobs
                .map((job) => String(job.submission_id || job.id || '').trim())
                .filter(Boolean)
        )
    );

    const visibleFailedSubmissionIds = Array.from(
        new Set(
            filteredFailedJobs
                .map((row) => String(row.id || '').trim())
                .filter(Boolean)
        )
    );

    const retryTargetCount = isFailedOnly ? visibleFailedSubmissionIds.length : failedSubmissionIds.length;
    const retryProgressPercent = retryProgress.total > 0 ? Math.round((retryProgress.done / retryProgress.total) * 100) : 0;
    const backendHealthLabel = backendHealth === 'ok'
        ? 'API OK'
        : (backendHealth === 'down' ? 'API Down' : 'Checking API');
    const backendHealthBadgeClass = backendHealth === 'ok'
        ? 'border-emerald-400/30 bg-emerald-500/10 text-emerald-200'
        : (backendHealth === 'down'
            ? 'border-red-400/30 bg-red-500/10 text-red-200'
            : 'border-amber-400/30 bg-amber-500/10 text-amber-200');
    const headerPrimaryActionClass = 'h-10 min-w-[148px] px-4 rounded-lg text-sm font-bold inline-flex items-center justify-center gap-2 whitespace-nowrap transition-all disabled:opacity-60 disabled:cursor-not-allowed';
    const cleanupHourPresets = CLEANUP_HOUR_PRESETS;
    const isApiDown = backendHealth === 'down';
    const isStorageNearLimit = !!(uploadStorageStats && uploadStorageStats.warnGb > 0 && (
        uploadStorageStats.overWarn || uploadStorageStats.usageRatio >= STORAGE_NEAR_WARN_RATIO
    ));
    const confirmDialogButtonClass = confirmDialog?.tone === 'danger'
        ? 'bg-red-600 hover:bg-red-500 text-white'
        : 'bg-cyan-600 hover:bg-cyan-500 text-white';
    const actionNoticeClass = actionNotice?.type === 'success'
        ? 'border-emerald-400/25 bg-emerald-500/10 text-emerald-100'
        : actionNotice?.type === 'info'
            ? 'border-cyan-400/25 bg-cyan-500/10 text-cyan-100'
            : 'border-red-400/25 bg-red-500/10 text-red-100';
    const actionNoticeTitleClass = actionNotice?.type === 'success'
        ? 'text-emerald-200'
        : actionNotice?.type === 'info'
            ? 'text-cyan-200'
            : 'text-red-200';
    const cleanupCardClass = lastCleanupResult?.kind === 'success'
        ? 'border-emerald-400/25 bg-emerald-500/10'
        : (lastCleanupResult?.kind === 'error'
            ? 'border-red-400/25 bg-red-500/10'
            : 'border-amber-400/25 bg-amber-500/10');
    const cleanupCardTitleClass = lastCleanupResult?.kind === 'success'
        ? 'text-emerald-200'
        : (lastCleanupResult?.kind === 'error' ? 'text-red-200' : 'text-amber-200');
    const cleanupCardTextClass = lastCleanupResult?.kind === 'success'
        ? 'text-emerald-100/90'
        : (lastCleanupResult?.kind === 'error' ? 'text-red-100/90' : 'text-amber-100/90');
    const hasSearchApplied = !!searchDebounced;
    const searchResultCount = isJobOnlyView ? filteredReports.length : reportTotal;
    const searchResultExtra = !isJobOnlyView && statusFilter === 'all' && currentPage === 1 && activeJobsOnTop > 0
        ? ` + ${activeJobsOnTop} active`
        : '';

    const handleStopRetryBatch = () => {
        if (!isRetryingFailedBatch) return;
        retryStopRef.current = true;
        setIsRetryStopRequested(true);
    };

    const handleRetryFailedBatch = async () => {
        if (isRetryingFailedBatch) return;
        if (backendHealth === 'down') {
            setActionNotice({ type: 'error', title: 'Batch Retry', message: 'API is currently down. Please retry after backend recovers.' });
            return;
        }
        const targetIds = isFailedOnly ? visibleFailedSubmissionIds : failedSubmissionIds;
        if (targetIds.length === 0) {
            setActionNotice({ type: 'info', title: 'Batch Retry', message: 'No failed jobs to retry.' });
            return;
        }
        const confirmed = await requestConfirmation({
            title: 'Batch Retry',
            message: `Retry ${targetIds.length} failed job(s) now?`,
            confirmLabel: 'Start Retry',
            tone: 'primary',
        });
        if (!confirmed) {
            return;
        }

        setLastRetrySummary(null);
        setShowRetryDetails(false);
        setIsRetryingFailedBatch(true);
        setIsRetryStopRequested(false);
        retryStopRef.current = false;
        setRetryProgress({
            total: targetIds.length,
            done: 0,
            queued: 0,
            failed: 0,
        });

        const RETRY_BATCH_CONCURRENCY = 3;
        let nextIndex = 0;
        let queuedCount = 0;
        let failedCount = 0;
        let firstError = '';
        const successIds: string[] = [];
        const failedItems: BatchRetryFailureItem[] = [];

        try {
            const worker = async () => {
                while (true) {
                    if (retryStopRef.current) {
                        return;
                    }
                    const current = nextIndex;
                    if (current >= targetIds.length) {
                        return;
                    }
                    nextIndex += 1;
                    const submissionId = targetIds[current];

                    let queuedOk = false;
                    let errorMsg = '';

                    try {
                        const response = await fetch(`${API_HOST}/api/jobs/${encodeURIComponent(submissionId)}/rescore`, {
                            method: 'POST'
                        });
                        if (response.ok) {
                            queuedOk = true;
                        } else {
                            try {
                                const payload = await response.json();
                                errorMsg = String(payload?.detail || 'Unknown error');
                            } catch {
                                errorMsg = `HTTP ${response.status}`;
                            }
                        }
                    } catch (err: unknown) {
                        errorMsg = getErrorMessage(err, 'Unknown error');
                    }

                    if (queuedOk) {
                        queuedCount += 1;
                        successIds.push(submissionId);
                    } else {
                        failedCount += 1;
                        failedItems.push({
                            id: submissionId,
                            error: errorMsg || 'Unknown error',
                        });
                        if (errorMsg && !firstError) {
                            firstError = errorMsg;
                        }
                    }

                    setRetryProgress({
                        total: targetIds.length,
                        done: queuedCount + failedCount,
                        queued: queuedCount,
                        failed: failedCount,
                    });
                }
            };

            const workers = Array.from(
                { length: Math.min(RETRY_BATCH_CONCURRENCY, targetIds.length) },
                () => worker()
            );
            await Promise.all(workers);

            const processedCount = queuedCount + failedCount;
            const wasStopped = retryStopRef.current && processedCount < targetIds.length;
            const skippedCount = Math.max(0, targetIds.length - processedCount);
            setLastRetrySummary({
                runAt: Date.now(),
                targetCount: targetIds.length,
                processed: processedCount,
                queued: queuedCount,
                failed: failedCount,
                skipped: skippedCount,
                stopped: wasStopped,
                successIds,
                failedItems,
            });
            setShowRetryDetails(failedItems.length > 0);
            setRetryProgress({
                total: targetIds.length,
                done: processedCount,
                queued: queuedCount,
                failed: failedCount,
            });
            if (wasStopped) {
                setActionNotice({
                    type: 'info',
                    title: 'Batch Retry',
                    message: `Stopped. Processed ${processedCount}/${targetIds.length}; queued ${queuedCount}; failed ${failedCount}; skipped ${skippedCount}.${firstError ? ` First error: ${firstError}` : ''}`,
                });
            } else {
                setActionNotice({
                    type: failedCount > 0 ? 'info' : 'success',
                    title: 'Batch Retry',
                    message: `Completed. Queued ${queuedCount}; failed ${failedCount}.${firstError ? ` First error: ${firstError}` : ''}`,
                });
            }
            await fetchData({ background: true });
            startPolling();
        } finally {
            setIsRetryingFailedBatch(false);
            setIsRetryStopRequested(false);
            retryStopRef.current = false;
        }
    };

    return (
        <div className="pt-24 pb-20 px-8 max-w-7xl mx-auto min-h-screen">
            {/* Header Section */}
            <div className="flex flex-col md:flex-row md:items-center justify-between gap-6 mb-8">
                <div>
                    <h1 className="text-3xl font-black text-white flex items-center gap-3">
                        <History className="w-8 h-8 text-primary" />
                        My Recording List
                    </h1>
                    <p className="text-gray-500 text-sm mt-1 ml-11">
                        Manage and review your student assessments.
                        {lastSyncAt ? <span className="ml-2">Last sync: {new Date(lastSyncAt).toLocaleTimeString()}</span> : null}
                        <span className={`ml-2 inline-flex items-center rounded-md border px-2 py-0.5 text-[11px] font-bold ${backendHealthBadgeClass}`}>
                            {backendHealthLabel}
                        </span>
                        {backendHealthAt ? (
                            <span className="ml-1 text-[11px] text-gray-600">
                                {new Date(backendHealthAt).toLocaleTimeString()}
                            </span>
                        ) : null}
                        {uploadStorageStats ? (
                            <span className="ml-2 text-[11px] text-gray-500">
                                Backend audio: {uploadStorageStats.fileCount} files ({uploadStorageStats.usedGb.toFixed(2)}GB)
                            </span>
                        ) : null}
                    </p>
                </div>

                <div className="flex w-full flex-wrap items-start gap-3 md:w-auto md:items-center">
                    <button
                        onClick={handleManualRefresh}
                        disabled={isRefreshing || isRefreshCoolingDown}
                        className={`${headerPrimaryActionClass} bg-cyan-500/10 border border-cyan-400/20 text-cyan-200 hover:bg-cyan-500/20`}
                        title="Refresh reports and jobs now (2s cooldown)"
                    >
                        {isRefreshing ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
                        {isRefreshing ? 'Refreshing...' : (isRefreshCoolingDown ? `Refresh (${Math.max(1, refreshCooldownSeconds)}s)` : 'Refresh')}
                    </button>
                    {/* Mode Toggle & Batch Actions */}
                    {isSelectionMode ? (
                        <div className="flex items-center gap-2 animate-in fade-in slide-in-from-right-4 duration-300">
                            <button
                                onClick={() => {
                                    setIsSelectionMode(false);
                                    setSelectedIds(new Set());
                                }}
                                className={`${headerPrimaryActionClass} min-w-[120px] bg-white/5 border border-white/10 text-gray-300 hover:bg-white/10 hover:text-white`}
                            >
                                Cancel
                            </button>
                            {selectedIds.size > 0 && (
                                <button
                                    onClick={handleBatchDelete}
                                    className={`${headerPrimaryActionClass} bg-red-500 text-white hover:bg-red-600 shadow-lg shadow-red-500/20`}
                                >
                                    <Trash2 className="w-4 h-4" />
                                    Delete ({selectedIds.size})
                                </button>
                            )}
                        </div>
                    ) : (
                        <>
                            <button
                                onClick={handleToggleFailedView}
                                className={`${headerPrimaryActionClass} ${isFailedOnly ? 'bg-red-500 text-white hover:bg-red-600' : 'bg-red-500/10 border border-red-400/20 text-red-200 hover:bg-red-500/20'}`}
                                title="One-click filter failed jobs"
                            >
                                <AlertCircle className="w-4 h-4" />
                                {isFailedOnly ? 'Back to All' : `Failed (${jobStats.failed})`}
                            </button>
                            {failedSubmissionIds.length > 0 && (
                                <button
                                    onClick={handleRetryFailedBatch}
                                    disabled={isRetryingFailedBatch || retryTargetCount === 0 || isApiDown}
                                    className={`${headerPrimaryActionClass} bg-cyan-500/10 border border-cyan-400/20 text-cyan-200 hover:bg-cyan-500/20`}
                                    title={isApiDown ? 'API is down' : 'Retry failed jobs in batch'}
                                >
                                    {isRetryingFailedBatch ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
                                    {isRetryingFailedBatch ? `Retrying ${retryProgress.done}/${retryProgress.total}` : `Retry Failed (${retryTargetCount})`}
                                </button>
                            )}
                            {isRetryingFailedBatch && (
                                <button
                                    onClick={handleStopRetryBatch}
                                    disabled={isRetryStopRequested}
                                    className={`${headerPrimaryActionClass} bg-red-500/10 border border-red-400/20 text-red-200 hover:bg-red-500/20`}
                                    title="Stop pending retries"
                                >
                                    <AlertCircle className="w-4 h-4" />
                                    {isRetryStopRequested ? 'Stopping...' : 'Stop Retry'}
                                </button>
                            )}
                            <div className="inline-flex flex-col items-start gap-1">
                                <div className="inline-flex items-center gap-2">
                                    <span className="text-xs font-semibold text-red-200/90 whitespace-nowrap">Failed Older Than</span>
                                    <select
                                        value={cleanupFailedHours}
                                        onChange={(e) => setCleanupFailedHours(normalizeCleanupHours(e.target.value))}
                                        disabled={isCleaningFailed || isApiDown}
                                        className="h-10 min-w-[88px] rounded-lg border border-red-400/20 bg-red-500/5 px-2 text-sm font-semibold text-red-100 focus:outline-none focus:border-red-300/40 disabled:opacity-60"
                                        title={isApiDown ? 'API is down' : 'Hours threshold'}
                                    >
                                        {cleanupHourPresets.map((preset) => (
                                            <option key={preset} value={String(preset)} className="bg-[#1e1e24] text-gray-100">
                                                {preset}h
                                            </option>
                                        ))}
                                    </select>
                                    <button
                                        onClick={handleCleanupFailed}
                                        disabled={isCleaningFailed || isApiDown}
                                        className={`${headerPrimaryActionClass} bg-red-500/10 border border-red-400/20 text-red-200 hover:bg-red-500/20`}
                                        title={isApiDown ? 'API is down' : 'Remove stale failed jobs'}
                                    >
                                        {isCleaningFailed ? <Loader2 className="w-4 h-4 animate-spin" /> : <AlertCircle className="w-4 h-4" />}
                                        {isCleaningFailed ? 'Cleaning...' : 'Clean Old Failed'}
                                    </button>
                                </div>
                                <label className="inline-flex items-center gap-2 text-[11px] text-gray-400">
                                    <input
                                        type="checkbox"
                                        checked={cleanupDeleteUploads}
                                        onChange={(e) => setCleanupDeleteUploads(e.target.checked)}
                                        disabled={isCleaningFailed || isApiDown}
                                        className="h-3.5 w-3.5 rounded border-white/20 bg-white/5 text-red-400 focus:ring-red-400/50 disabled:opacity-60"
                                    />
                                    Also delete original audio
                                </label>
                            </div>
                            {!isJobOnlyView && (
                                <button
                                    onClick={() => setIsSelectionMode(true)}
                                    className={`${headerPrimaryActionClass} bg-white/5 border border-white/10 text-gray-300 hover:bg-white/10 hover:text-white`}
                                >
                                    <span className="w-4 h-4 border-2 border-current rounded-sm border-dashed"></span>
                                    Select
                                </button>
                            )}
                        </>
                    )}

                    <div className="relative group order-last basis-full md:order-none md:basis-auto md:w-[320px]">
                        <div className="absolute inset-y-0 left-3 flex items-center pointer-events-none">
                            <Search className="w-4 h-4 text-gray-500" />
                        </div>
                        <input
                            ref={searchInputRef}
                            type="text"
                            placeholder="Search records..."
                            value={searchTerm}
                            onChange={(e) => setSearchTerm(String(e.target.value || '').slice(0, MAX_SEARCH_TERM_LENGTH))}
                            onKeyDown={handleSearchKeyDown}
                            title="Press / or Ctrl/Cmd+K to focus, Enter to search, Esc to clear/reset"
                            className="w-full bg-[#1e1e24] border border-white/5 rounded-lg py-2.5 pl-9 pr-16 text-sm text-gray-300 focus:outline-none focus:border-primary/50 transition-all font-medium"
                        />
                        {(searchTerm || searchDebounced) ? (
                            <button
                                onClick={clearSearchNow}
                                className="absolute inset-y-0 right-2 my-1 px-2 rounded-md text-xs font-semibold text-gray-300 hover:text-white hover:bg-white/10 transition-colors"
                                title="Clear search"
                            >
                                Clear
                            </button>
                        ) : null}
                        <div className="mt-1 px-1 text-[11px] font-medium text-gray-500">
                            {hasSearchApplied ? 'Matches' : 'Records'}: <span className="text-gray-300">{searchResultCount}</span>{searchResultExtra ? <span className="text-cyan-300">{searchResultExtra}</span> : null}
                        </div>
                    </div>
                    {hasActiveViewOverrides ? (
                        <button
                            onClick={handleResetView}
                            className={`${headerPrimaryActionClass} order-last basis-full md:order-none md:basis-auto md:min-w-[120px] bg-white/5 border border-white/10 text-gray-300 hover:bg-white/10 hover:text-white`}
                            title="Clear search and reset status/date filters"
                        >
                            Reset View
                        </button>
                    ) : null}
                </div>
            </div>
            {backendHealth === 'down' && (
                <div className="mb-6 rounded-xl border border-red-400/25 bg-red-500/10 px-4 py-3 text-sm">
                    <div className="text-red-200 font-bold">Backend status abnormal</div>
                    <div className="text-red-200/80 text-xs mt-1">
                        Some actions may fail until API recovers.
                        {backendHealthError ? <span className="ml-1">({backendHealthError})</span> : null}
                    </div>
                </div>
            )}
            {isStorageNearLimit && uploadStorageStats && (
                <div className={`mb-6 rounded-xl border px-4 py-3 text-sm ${uploadStorageStats.overWarn ? 'border-red-400/30 bg-red-500/10' : 'border-amber-300/30 bg-amber-500/10'}`}>
                    <div className={`${uploadStorageStats.overWarn ? 'text-red-200' : 'text-amber-200'} font-bold`}>
                        Upload storage {uploadStorageStats.overWarn ? 'over limit' : 'near limit'}
                    </div>
                    <div className={`${uploadStorageStats.overWarn ? 'text-red-200/85' : 'text-amber-100/85'} text-xs mt-1`}>
                        Using {uploadStorageStats.usedGb.toFixed(2)}GB / {uploadStorageStats.warnGb.toFixed(2)}GB ({Math.round(uploadStorageStats.usageRatio * 100)}%).
                        Delete old failed items if needed.
                    </div>
                </div>
            )}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
                <button
                    onClick={() => switchStatusFilter('queued')}
                    className={`text-left rounded-xl border px-4 py-3 transition-all ${statusFilter === 'queued' ? 'border-slate-300/35 bg-slate-500/10' : 'border-white/10 bg-white/5 hover:bg-slate-500/10'}`}
                >
                    <div className="text-xs text-slate-300 uppercase tracking-wider">Queued</div>
                    <div className="text-xl font-black text-slate-100">{jobStats.queued}</div>
                </button>
                <button
                    onClick={() => switchStatusFilter('processing')}
                    className={`text-left rounded-xl border px-4 py-3 transition-all ${statusFilter === 'processing' ? 'border-cyan-400/35 bg-cyan-500/10' : 'border-white/10 bg-white/5 hover:bg-white/10'}`}
                >
                    <div className="text-xs text-cyan-300 uppercase tracking-wider">Processing</div>
                    <div className="text-xl font-black text-cyan-200">{jobStats.processing}</div>
                </button>
                <button
                    onClick={() => switchStatusFilter('failed')}
                    className={`text-left rounded-xl border px-4 py-3 transition-all ${isFailedOnly ? 'border-red-400/40 bg-red-500/15' : 'border-white/10 bg-white/5 hover:bg-red-500/10'}`}
                >
                    <div className="text-xs text-red-300 uppercase tracking-wider">Failed</div>
                    <div className="text-xl font-black text-red-200">{jobStats.failed}</div>
                </button>
                <button
                    onClick={() => switchStatusFilter('completed')}
                    className={`text-left rounded-xl border px-4 py-3 transition-all ${statusFilter === 'completed' ? 'border-green-400/20 bg-green-500/10' : 'border-white/10 bg-white/5 hover:bg-white/10'}`}
                >
                    <div className="text-xs text-green-300 uppercase tracking-wider">Completed</div>
                    <div className="text-xl font-black text-green-200">{jobStats.completed}</div>
                </button>
            </div>
            <div className="mb-6 flex flex-wrap items-center gap-2">
                <span className="text-xs font-bold uppercase tracking-wider text-gray-500">Date</span>
                <button
                    onClick={() => {
                        setDateRangeFilter('all');
                        setCurrentPage(1);
                    }}
                    className={`h-8 px-3 rounded-lg border text-xs font-bold transition-colors ${dateRangeFilter === 'all' ? 'border-cyan-300/40 bg-cyan-500/20 text-cyan-100' : 'border-white/10 bg-white/5 text-gray-300 hover:bg-white/10 hover:text-white'}`}
                >
                    All
                </button>
                <button
                    onClick={() => {
                        setDateRangeFilter('today');
                        setCurrentPage(1);
                    }}
                    className={`h-8 px-3 rounded-lg border text-xs font-bold transition-colors ${dateRangeFilter === 'today' ? 'border-cyan-300/40 bg-cyan-500/20 text-cyan-100' : 'border-white/10 bg-white/5 text-gray-300 hover:bg-white/10 hover:text-white'}`}
                >
                    Today
                </button>
                <button
                    onClick={() => {
                        setDateRangeFilter('7d');
                        setCurrentPage(1);
                    }}
                    className={`h-8 px-3 rounded-lg border text-xs font-bold transition-colors ${dateRangeFilter === '7d' ? 'border-cyan-300/40 bg-cyan-500/20 text-cyan-100' : 'border-white/10 bg-white/5 text-gray-300 hover:bg-white/10 hover:text-white'}`}
                >
                    Last 7 days
                </button>
            </div>
            {isRetryingFailedBatch && retryProgress.total > 0 && (
                <div className="mb-6 rounded-xl border border-cyan-400/20 bg-cyan-500/10 px-4 py-3">
                    <div className="flex items-center justify-between text-xs mb-2">
                        <span className="text-cyan-200 font-bold">
                            {isRetryStopRequested ? 'Stopping retry queue...' : `Retrying failed jobs: ${retryProgress.done}/${retryProgress.total}`}
                        </span>
                        <span className="text-cyan-100/80">
                            queued {retryProgress.queued} | failed {retryProgress.failed}
                        </span>
                    </div>
                    <div className="h-2 bg-white/10 rounded-full overflow-hidden">
                        <div
                            className="h-full bg-cyan-400 transition-all duration-300"
                            style={{ width: `${Math.max(0, Math.min(100, retryProgressPercent))}%` }}
                        />
                    </div>
                </div>
            )}
            {actionNotice && (
                <div className={`mb-6 rounded-xl border px-4 py-3 ${actionNoticeClass}`}>
                    <div className="flex items-center justify-between gap-2">
                        <div className={`text-sm font-bold ${actionNoticeTitleClass}`}>{actionNotice.title}</div>
                        <button
                            onClick={() => setActionNotice(null)}
                            className="text-xs text-gray-300/80 hover:text-white transition-colors"
                        >
                            Dismiss
                        </button>
                    </div>
                    <div className="mt-2 text-xs">{actionNotice.message}</div>
                </div>
            )}
            {lastCleanupResult && (
                <div className={`mb-6 rounded-xl border px-4 py-3 ${cleanupCardClass}`}>
                    <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className={`text-sm font-bold ${cleanupCardTitleClass}`}>
                            Last cleanup {lastCleanupResult.kind === 'error' ? '(failed)' : '(completed)'}
                            <span className="ml-2 text-xs font-medium text-gray-400">
                                {new Date(lastCleanupResult.runAt).toLocaleTimeString()}
                            </span>
                        </div>
                        <button
                            onClick={() => setLastCleanupResult(null)}
                            className="text-xs text-gray-300/80 hover:text-white transition-colors"
                        >
                            Dismiss
                        </button>
                    </div>
                    <div className={`mt-2 text-xs ${cleanupCardTextClass}`}>{lastCleanupResult.message}</div>
                    <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
                        <span className="px-2 py-1 rounded-md bg-white/10 text-gray-100">Threshold {lastCleanupResult.hours}h</span>
                        {lastCleanupResult.kind !== 'error' ? (
                            <>
                                <span className="px-2 py-1 rounded-md bg-white/10 text-gray-100">Matched {lastCleanupResult.matched}</span>
                                <span className="px-2 py-1 rounded-md bg-white/10 text-gray-100">Deleted {lastCleanupResult.deleted}</span>
                            </>
                        ) : null}
                    </div>
                </div>
            )}
            {lastRetrySummary && (
                <div className="mb-6 rounded-xl border border-white/10 bg-white/5 px-4 py-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="text-sm font-bold text-white">
                            Last batch retry {lastRetrySummary.stopped ? '(stopped)' : '(completed)'}
                            <span className="ml-2 text-xs font-medium text-gray-400">
                                {new Date(lastRetrySummary.runAt).toLocaleTimeString()}
                            </span>
                        </div>
                        <button
                            onClick={() => setLastRetrySummary(null)}
                            className="text-xs text-gray-400 hover:text-white transition-colors"
                        >
                            Dismiss
                        </button>
                    </div>
                    <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
                        <span className="px-2 py-1 rounded-md bg-white/10 text-gray-200">Target {lastRetrySummary.targetCount}</span>
                        <span className="px-2 py-1 rounded-md bg-white/10 text-gray-200">Processed {lastRetrySummary.processed}</span>
                        <span className="px-2 py-1 rounded-md bg-emerald-500/20 text-emerald-200">Queued {lastRetrySummary.queued}</span>
                        <span className="px-2 py-1 rounded-md bg-red-500/20 text-red-200">Failed {lastRetrySummary.failed}</span>
                        {lastRetrySummary.skipped > 0 ? (
                            <span className="px-2 py-1 rounded-md bg-amber-500/20 text-amber-200">Skipped {lastRetrySummary.skipped}</span>
                        ) : null}
                    </div>
                    {lastRetrySummary.failedItems.length > 0 ? (
                        <div className="mt-3">
                            <button
                                onClick={() => setShowRetryDetails((v) => !v)}
                                className="text-xs font-bold text-red-200 hover:text-red-100 transition-colors"
                            >
                                {showRetryDetails ? 'Hide failed details' : `Show failed details (${lastRetrySummary.failedItems.length})`}
                            </button>
                            {showRetryDetails && (
                                <div className="mt-2 rounded-lg border border-red-400/20 bg-red-500/5 p-3 max-h-56 overflow-auto space-y-2">
                                    {lastRetrySummary.failedItems.slice(0, 30).map((item) => (
                                        <div key={`${item.id}-${item.error}`} className="text-xs">
                                            <div className="text-red-200 font-mono">{item.id}</div>
                                            <div className="text-red-200/80">{item.error}</div>
                                        </div>
                                    ))}
                                    {lastRetrySummary.failedItems.length > 30 ? (
                                        <div className="text-xs text-red-200/70">
                                            ...and {lastRetrySummary.failedItems.length - 30} more
                                        </div>
                                    ) : null}
                                </div>
                            )}
                        </div>
                    ) : (
                        <div className="mt-3 text-xs text-emerald-200">All retries were queued successfully.</div>
                    )}
                </div>
            )}

            {/* List Content */}
            {isLoading ? (
                <div className="flex flex-col items-center justify-center py-32 space-y-4">
                    <Loader2 className="w-10 h-10 text-primary animate-spin opacity-50" />
                    <p className="text-gray-500 text-sm font-medium tracking-wide">LOADING RECORDS...</p>
                </div>
            ) : error ? (
                <div className="bg-red-500/10 border border-red-500/20 rounded-2xl p-8 flex flex-col items-center gap-4 text-center">
                    <AlertCircle className="w-10 h-10 text-red-400" />
                    <div>
                        <h3 className="text-lg font-bold text-white">Failed to load history</h3>
                        <p className="text-red-400/80 text-sm mt-1">{error}</p>
                    </div>
                    <button
                        onClick={fetchReports}
                        className="bg-red-500 text-white px-4 py-2 rounded-lg text-sm font-bold hover:bg-red-600 transition-colors"
                    >
                        Retry
                    </button>
                </div>
            ) : paginatedReports.length === 0 ? (
                <div className="bg-[#1e1e24] border border-white/5 border-dashed rounded-2xl py-20 text-center">
                    <div className="bg-white/5 w-16 h-16 rounded-full flex items-center justify-center mx-auto mb-4">
                        <History className="w-8 h-8 text-gray-600" />
                    </div>
                    <h3 className="text-xl font-bold text-gray-300">
                        {statusFilter === 'failed'
                            ? 'No failed jobs found'
                            : statusFilter === 'queued'
                                ? 'No queued jobs found'
                                : statusFilter === 'processing'
                                    ? 'No processing jobs found'
                                    : statusFilter === 'completed'
                                        ? 'No completed reports found'
                                        : 'No recordings found'}
                    </h3>
                    <p className="text-gray-500 text-sm mt-2">
                        {isJobOnlyView
                            ? 'Current status has no records.'
                            : (statusFilter === 'completed' ? 'No completed reports yet.' : 'Upload a new file to get started.')}
                    </p>
                </div>
            ) : (
                <div className="bg-[#1e1e24] border border-white/5 rounded-2xl overflow-hidden shadow-2xl">
                    {/* Toolbar */}
                    {isSelectionMode && !isJobOnlyView && (
                        <div className="px-6 py-4 border-b border-white/5 flex items-center gap-4 bg-white/5 animate-in fade-in slide-in-from-top-2 duration-200">
                            <div className="flex items-center gap-3">
                                <input
                                    type="checkbox"
                                    checked={paginatedReports.length > 0 && selectedIds.size === paginatedReports.length}
                                    onChange={handleSelectAll}
                                    className="w-5 h-5 rounded border-gray-600 bg-gray-700 text-primary focus:ring-primary focus:ring-offset-gray-900 cursor-pointer"
                                />
                                <span className="text-sm font-bold text-gray-400">Select All</span>
                            </div>
                            <div className="text-xs text-gray-500 border-l border-white/10 pl-4">
                                {selectedIds.size} selected
                            </div>
                        </div>
                    )}

                    <div className="divide-y divide-white/5">
                        {paginatedReports.map((report) => {
                            const { label, color } = getLevelInfo(report.score, report.status);
                            const isSelected = selectedIds.has(report.id);
                            const canOpenReport = !report.status;
                            const isFailedRow = report.status === 'failed';
                            const isRead = canOpenReport && readReportIds.has(report.id);
                            const isRescorePending = rescorePendingIds.has(report.id);

                            return (
                                <div
                                    key={report.id}
                                    className={`p-6 flex items-center gap-6 transition-colors group ${isSelected ? 'bg-primary/5' : 'hover:bg-white/[0.02]'} ${isSelectionMode ? 'cursor-pointer' : ''}`}
                                    onClick={() => isSelectionMode && handleToggleSelect(report.id)}
                                >
                                    {/* Checkbox (Conditional) */}
                                    {isSelectionMode && (
                                        <div className="shrink-0 animate-in fade-in zoom-in duration-200">
                                            <input
                                                type="checkbox"
                                                checked={isSelected}
                                                onChange={() => handleToggleSelect(report.id)}
                                                className="w-5 h-5 rounded border-gray-600 bg-gray-700 text-primary focus:ring-primary focus:ring-offset-gray-900 cursor-pointer pointer-events-none" // pointer-events-none to let parent click handle it
                                            />
                                        </div>
                                    )}

                                    {/* Status Dot (Unread Indicator) */}
                                    <div className="w-2.5 h-2.5 shrink-0 flex items-center justify-center">
                                        {canOpenReport && !isRead && (
                                            <div className="w-2.5 h-2.5 rounded-full bg-green-500 shadow-[0_0_10px_rgba(34,197,94,0.5)] animate-pulse" title="Unread" />
                                        )}
                                    </div>

                                    {/* Main Info */}
                                    <div className="flex-1 min-w-0">
                                        <div className="flex items-center gap-2 mb-1">
                                            <h3 className={`text-lg transition-colors ${canOpenReport ? 'cursor-pointer' : 'cursor-default'} ${isRead ? 'text-gray-400 font-medium' : 'text-white font-bold'} ${canOpenReport ? 'group-hover:text-primary' : ''}`} onClick={() => {
                                                if (!isSelectionMode && canOpenReport) {
                                                    markAsRead(report.id);
                                                    window.open(`${API_HOST}${report.url}`, '_blank');
                                                }
                                            }}>
                                                {report.display_name || report.student_name}
                                            </h3>
                                            {!isSelectionMode && canOpenReport && (
                                                <button className="text-gray-600 hover:text-gray-400 opacity-0 group-hover:opacity-100 transition-opacity">
                                                    <ExternalLink className="w-3.5 h-3.5" />
                                                </button>
                                            )}
                                        </div>
                                        <div className="flex items-center gap-4 text-xs text-gray-500 font-mono">
                                            <span className="flex items-center gap-1.5">
                                                <Calendar className="w-3.5 h-3.5" />
                                                {formatTimestamp(report.timestamp)}
                                            </span>
                                            <span className="w-1 h-1 bg-gray-700 rounded-full" />
                                            <div className="flex items-center gap-1.5">
                                                <span>ID: {report.id.substring(0, 8)}...</span>
                                                <button
                                                    type="button"
                                                    onClick={(e) => {
                                                        e.stopPropagation();
                                                        void copyReportId(report.id);
                                                    }}
                                                    className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 transition-colors ${
                                                        copiedReportId === report.id
                                                            ? 'text-emerald-300 bg-emerald-500/15'
                                                            : 'text-gray-400 hover:text-white hover:bg-white/10'
                                                    }`}
                                                    title={copiedReportId === report.id ? 'Copied full ID' : 'Copy full ID'}
                                                >
                                                    <Copy className="w-3 h-3" />
                                                    {copiedReportId === report.id ? 'Copied' : 'Copy ID'}
                                                </button>
                                            </div>
                                        </div>
                                        {isFailedRow && (
                                            <div className="text-xs text-red-300 mt-2 line-clamp-1">
                                                {report.error || 'Job failed without detailed error message.'}
                                            </div>
                                        )}
                                    </div>

                                    {/* Score & Level */}
                                    <div className="flex items-center gap-4 w-48 shrink-0 justify-end">
                                        <div className="text-right hidden sm:block">
                                            <div className={`text-sm font-bold ${color}`}>{label}</div>
                                            <div className="text-[10px] text-gray-600 uppercase tracking-wider font-bold">Proficiency</div>
                                        </div>
                                        <CircularScore score={report.score} status={report.status} />
                                    </div>

                                    {/* Actions (Only visible when NOT in selection mode) */}
                                    {!isSelectionMode && (canOpenReport || isFailedRow) && (
                                        <div className="flex items-center gap-2 pl-4 border-l border-white/5 ml-2" onClick={(e) => e.stopPropagation()}>
                                            {canOpenReport && (
                                                <button
                                                    onClick={() => {
                                                        markAsRead(report.id);
                                                        window.open(`${API_HOST}${report.url}`, '_blank');
                                                    }}
                                                    className="p-2 text-gray-400 hover:text-white hover:bg-white/10 rounded-lg transition-all"
                                                    title="View Report"
                                                >
                                                    <TrendingUp className="w-4 h-4" />
                                                </button>
                                            )}

                                            {/* Rescore (Clone & Re-run) */}
                                            <button
                                                onClick={(e) => {
                                                    e.stopPropagation();
                                                    void handleRescore(report.id);
                                                }}
                                                disabled={isRescorePending || isApiDown}
                                                className="p-2 text-gray-600 hover:text-cyan-300 hover:bg-cyan-500/10 rounded-lg transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                                                title={isApiDown ? 'API is down' : (isRescorePending ? 'Rescore request in progress' : 'Rescore (Clone & Rerun)')}
                                            >
                                                <RefreshCw className={`w-4 h-4 ${isRescorePending ? 'animate-spin text-cyan-300' : ''}`} />
                                            </button>

                                            {/* Print / Export PDF */}
                                            {canOpenReport && (
                                                <button
                                                    onClick={() => {
                                                        markAsRead(report.id);
                                                        window.open(`${API_HOST}${report.url}`, '_blank');
                                                    }}
                                                    className="p-2 text-gray-600 hover:text-primary hover:bg-primary/10 rounded-lg transition-all"
                                                    title="Print / Export PDF"
                                                >
                                                    {/* Use Printer Icon */}
                                                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="lucide lucide-printer"><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2" /><path d="M6 9V3a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v6" /><rect x="6" y="14" width="12" height="8" rx="1" /></svg>
                                                </button>
                                            )}
                                        </div>
                                    )}
                                </div>
                            );
                        })}
                    </div>

                    {/* Pagination Footer */}
                    <div className="px-6 py-4 border-t border-white/5 flex items-center justify-between bg-black/20">

                        <div className="text-xs text-gray-500 font-medium">
                            {isJobOnlyView ? (
                                <>
                                    Showing <span className="text-red-300">{paginatedReports.length}</span> {statusFilter} job record(s)
                                </>
                            ) : (
                                <>
                                    Showing <span className="text-gray-300">{reportTotal === 0 ? 0 : Math.min(reportTotal, (currentPage - 1) * itemsPerPage + 1)}</span> to <span className="text-gray-300">{Math.min(reportTotal, currentPage * itemsPerPage)}</span> of <span className="text-gray-300">{reportTotal}</span> entries
                                    {statusFilter === 'all' && currentPage === 1 && activeJobsOnTop > 0 ? <span className="text-cyan-300"> + {activeJobsOnTop} active</span> : null}
                                </>
                            )}
                        </div>
                        {usesReportPagination && (
                            <div className="flex items-center gap-2">
                            <button
                                onClick={() => setCurrentPage(p => Math.max(1, p - 1))}
                                disabled={currentPage === 1}
                                className="px-3 py-1.5 text-xs font-bold text-gray-400 bg-white/5 rounded-lg hover:bg-white/10 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                            >
                                Previous
                            </button>
                            <span className="text-xs font-mono text-gray-500 px-2">
                                Page {currentPage} / {totalPages || 1}
                            </span>
                            <button
                                onClick={() => setCurrentPage(p => Math.min(totalPages, p + 1))}
                                disabled={currentPage === totalPages}
                                className="px-3 py-1.5 text-xs font-bold text-gray-400 bg-white/5 rounded-lg hover:bg-white/10 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                            >
                                Next
                            </button>
                            </div>
                        )}
                    </div>
                </div>
            )}
            {confirmDialog && (
                <div
                    className="fixed inset-0 z-[90] flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
                    onClick={() => resolveConfirmDialog(false)}
                >
                    <div
                        className="w-full max-w-md rounded-2xl border border-white/10 bg-[#17171f] p-5 shadow-2xl"
                        onClick={(event) => event.stopPropagation()}
                    >
                        <h3 className="text-lg font-bold text-white">{confirmDialog.title}</h3>
                        <p className="mt-2 text-sm text-gray-300">{confirmDialog.message}</p>
                        <p className="mt-1 text-[11px] text-gray-500">Enter: confirm | Esc: cancel</p>
                        <div className="mt-5 flex items-center justify-end gap-2">
                            <button
                                type="button"
                                onClick={() => resolveConfirmDialog(false)}
                                className="h-9 rounded-lg border border-white/15 bg-white/5 px-3 text-sm font-medium text-gray-200 hover:bg-white/10"
                            >
                                Cancel
                            </button>
                            <button
                                type="button"
                                onClick={() => resolveConfirmDialog(true)}
                                className={`h-9 rounded-lg px-3 text-sm font-semibold transition-colors ${confirmDialogButtonClass}`}
                            >
                                {confirmDialog.confirmLabel}
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}

