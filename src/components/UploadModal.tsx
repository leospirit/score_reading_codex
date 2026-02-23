import { useState, useRef, useEffect, useCallback } from 'react';
import { X, Upload, FileAudio, AlertCircle, Loader2, Check, Trash2, History as HistoryIcon, Zap, Code } from 'lucide-react';
import { useViewportProfile } from '../hooks/useViewportProfile';
import { API_HOST } from '../config/api';

interface UploadModalProps {
    isOpen: boolean;
    onClose: () => void;
}

interface QueueItem {
    file: File | { name: string };
    status: 'idle' | 'uploading' | 'queued' | 'processing' | 'done' | 'error';
    jobId?: string;
    submissionId?: string;
    startedAt?: number;
    resultUrl?: string;
    error?: string;
}

interface ReportListItemLike {
    id?: unknown;
    student_name?: unknown;
    display_name?: unknown;
    original_filename?: unknown;
    url?: unknown;
}

interface JobStatusLike {
    status?: unknown;
    result_url?: unknown;
    error?: unknown;
}

interface UploadStorageUsageResponse {
    status?: unknown;
    uploads_bytes?: unknown;
    uploads_gb?: unknown;
    uploads_file_count?: unknown;
    linked_audio_file_count?: unknown;
    orphan_audio_file_count?: unknown;
    warn_gb?: unknown;
    over_warn?: unknown;
}

interface UploadStorageStats {
    uploadsGb: number;
    uploadsFileCount: number;
    linkedAudioFileCount: number;
    orphanAudioFileCount: number;
    warnGb: number;
    overWarn: boolean;
}

type FailedKeywordFilter = 'all' | 'timeout' | 'network' | 'server' | 'unknown';
type ConfirmActionState =
    | { kind: 'remove_one'; targetKey: string; targetName: string }
    | { kind: 'clear_all' };

export default function UploadModal({ isOpen, onClose }: UploadModalProps) {
    const LONG_WAIT_MS = 2 * 60 * 1000;
    const STUCK_WAIT_MS = 5 * 60 * 1000;
    const DELETE_REQUEST_TIMEOUT_MS = 15000;
    const UPLOAD_REQUEST_TIMEOUT_MS = 60000;
    const JOB_POLL_REQUEST_TIMEOUT_MS = 15000;
    const [maxUploadSizeMB, setMaxUploadSizeMB] = useState(30);
    const maxUploadSizeBytes = maxUploadSizeMB * 1024 * 1024;
    const ALLOWED_AUDIO_EXTENSIONS = new Set([
        '.mp3',
        '.wav',
        '.m4a',
        '.aac',
        '.flac',
        '.ogg',
        '.opus',
        '.oga',
        '.mpga',
        '.mpeg',
        '.webm',
        '.wma',
        '.mp4',
    ]);
    const [queue, setQueue] = useState<QueueItem[]>([]);
    const [text, setText] = useState(() => localStorage.getItem("reference_text") || "");
    const [preheatText, setPreheatText] = useState(() => localStorage.getItem("reference_preheat_text") || "");
    const [engineMode, setEngineMode] = useState<'auto' | 'pro'>('auto');
    const [isEditingPreheat, setIsEditingPreheat] = useState(false);
    const [isPreheating, setIsPreheating] = useState(false);
    const [preheatLastUpdated, setPreheatLastUpdated] = useState("");
    const [isPreheatReady, setIsPreheatReady] = useState(() => {
        const savedText = localStorage.getItem("reference_preheat_text") || "";
        const savedReady = localStorage.getItem("reference_preheat_ready") === "1";
        return savedReady && savedText.trim().length > 0;
    });
    const [preheatStatus, setPreheatStatus] = useState<{ type: 'idle' | 'success' | 'error'; message: string }>({
        type: 'idle',
        message: '',
    });
    const [isGlobalLoading, setIsGlobalLoading] = useState(false);
    const [showOnlyUnfinished, setShowOnlyUnfinished] = useState(false);
    const [showOnlyFailed, setShowOnlyFailed] = useState(false);
    const [failedKeywordFilter, setFailedKeywordFilter] = useState<FailedKeywordFilter>('all');
    const [stats, setStats] = useState({ completed: 0, total: 0 });
    const [tickNow, setTickNow] = useState(() => Date.now());
    const [isRefreshingStuck, setIsRefreshingStuck] = useState(false);
    const [lastStuckRefreshAt, setLastStuckRefreshAt] = useState<number | null>(null);
    const [isCopyingFailedSummary, setIsCopyingFailedSummary] = useState(false);
    const [failedSummaryCopiedAt, setFailedSummaryCopiedAt] = useState<number | null>(null);
    const [isCopyingFailedNames, setIsCopyingFailedNames] = useState(false);
    const [failedNamesCopiedAt, setFailedNamesCopiedAt] = useState<number | null>(null);
    const [confirmAction, setConfirmAction] = useState<ConfirmActionState | null>(null);
    const [isConfirmingAction, setIsConfirmingAction] = useState(false);
    const [deleteStatus, setDeleteStatus] = useState<{ type: 'idle' | 'error'; message: string }>({
        type: 'idle',
        message: '',
    });
    const [uploadStorageWarning, setUploadStorageWarning] = useState('');
    const [uploadStorageStats, setUploadStorageStats] = useState<UploadStorageStats | null>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);
    const preheatTextareaRef = useRef<HTMLTextAreaElement>(null);

    const [existingNames, setExistingNames] = useState<Set<string>>(new Set());
    const { isMobile } = useViewportProfile();
    const getQueueItemKey = (item?: QueueItem | null): string =>
        String(item?.submissionId || item?.jobId || item?.file?.name || '').trim();
    const getQueueItemDisplayName = (item?: QueueItem | null): string =>
        String(item?.file?.name || '').replace(/\.[^/.]+$/, '');

    const parseResponseSafely = async (response: Response) => {
        const contentType = response.headers.get('content-type') || '';
        if (contentType.includes('application/json')) {
            return await response.json();
        }
        const text = await response.text();
        try {
            return JSON.parse(text);
        } catch {
            const cleaned = text.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
            return { detail: cleaned || `${response.status} ${response.statusText}` };
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

    const formatUploadError = (reason: string): string => {
        const message = String(reason || '').trim();
        if (!message) return 'Upload failed: unknown error';
        if (/^upload failed:/i.test(message)) return message;
        return `Upload failed: ${message}`;
    };

    const formatFileSizeMB = (bytes: number): string => {
        const mb = bytes / (1024 * 1024);
        return mb >= 10 ? mb.toFixed(1) : mb.toFixed(2);
    };

    const isSupportedAudioFile = (file: File): boolean => {
        const name = String(file.name || '').toLowerCase();
        const dot = name.lastIndexOf('.');
        const ext = dot >= 0 ? name.slice(dot) : '';
        const type = String(file.type || '').toLowerCase();
        return type.startsWith('audio/') || ALLOWED_AUDIO_EXTENSIONS.has(ext);
    };

    const fetchWithTimeout = async (
        input: RequestInfo | URL,
        init?: RequestInit,
        timeoutMs: number = DELETE_REQUEST_TIMEOUT_MS
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
    };

    const refreshUploadStorageStats = useCallback(async () => {
        try {
            const response = await fetch(`${API_HOST}/api/storage/uploads-usage`);
            if (!response.ok) return;
            const contentType = response.headers.get('content-type') || '';
            let payload: UploadStorageUsageResponse | null = null;
            if (contentType.includes('application/json')) {
                payload = (await response.json()) as UploadStorageUsageResponse;
            } else {
                const text = await response.text();
                payload = JSON.parse(text) as UploadStorageUsageResponse;
            }
            if (!payload || typeof payload !== 'object') return;
            const usedGb = Number(payload.uploads_gb || 0);
            const warnGb = Number(payload.warn_gb || 0);
            const fileCount = Number(payload.uploads_file_count || 0);
            const linkedCount = Number(payload.linked_audio_file_count || 0);
            const orphanCount = Number(payload.orphan_audio_file_count || 0);
            const overWarn = Boolean(payload.over_warn);
            if (!Number.isFinite(usedGb) || !Number.isFinite(warnGb) || !Number.isFinite(fileCount) || !Number.isFinite(linkedCount) || !Number.isFinite(orphanCount)) return;
            setUploadStorageStats({
                uploadsGb: usedGb,
                uploadsFileCount: fileCount,
                linkedAudioFileCount: linkedCount,
                orphanAudioFileCount: orphanCount,
                warnGb,
                overWarn,
            });
            if (overWarn) {
                setUploadStorageWarning(
                    `Storage warning: uploads are ${usedGb.toFixed(2)}GB (${fileCount} files), over ${warnGb.toFixed(0)}GB threshold.`
                );
            } else {
                setUploadStorageWarning('');
            }
        } catch {
            // Keep local default states
        }
    }, []);

    // Initial load from server
    useEffect(() => {
        if (isOpen) {
            fetchRecentReports();
            void (async () => {
                try {
                    const response = await fetch(`${API_HOST}/api/config`);
                    if (!response.ok) return;
                    const payload = await parseResponseSafely(response);
                    const rawValue = Number(
                        (payload as { upload?: { max_mb?: unknown } })?.upload?.max_mb
                    );
                    if (Number.isFinite(rawValue) && rawValue >= 1 && rawValue <= 500) {
                        setMaxUploadSizeMB(rawValue);
                    }
                } catch {
                    // Keep local default
                }
            })();
            void refreshUploadStorageStats();
        }
    }, [isOpen, refreshUploadStorageStats]);

    useEffect(() => {
        if (!isOpen) return;
        const timer = window.setInterval(() => setTickNow(Date.now()), 1000);
        return () => window.clearInterval(timer);
    }, [isOpen]);

    useEffect(() => {
        if (deleteStatus.type === 'idle') return;
        const timer = window.setTimeout(() => {
            setDeleteStatus({ type: 'idle', message: '' });
        }, 5000);
        return () => window.clearTimeout(timer);
    }, [deleteStatus.type, deleteStatus.message]);

    const fetchRecentReports = async () => {
        try {
            const response = await fetch(`${API_HOST}/api/reports`);
            if (response.ok) {
                const payload = await response.json();
                const rows = Array.isArray(payload)
                    ? payload
                    : (Array.isArray(payload?.items) ? payload.items : []);

                // 1. Populate Set of existing names for duplicate checking
                // Assuming report name format usually matches uploaded filename stem
                const names = new Set<string>();
                const toStem = (value: string) => {
                    const dot = value.lastIndexOf('.');
                    return dot > 0 ? value.substring(0, dot) : value;
                };
                const listRows = rows as ReportListItemLike[];
                listRows.forEach((r) => {
                    if (r.student_name) names.add(String(r.student_name));
                    if (r.display_name) names.add(String(r.display_name));
                    if (r.original_filename) names.add(toStem(String(r.original_filename)));
                });
                setExistingNames(names);

                // 2. Queue population (just 5 for display)
                const recent: QueueItem[] = listRows.slice(0, 5).map((r) => ({
                    file: {
                        name: String(r?.student_name || r?.display_name || r?.original_filename || r?.id || 'unknown'),
                    },
                    status: 'done' as const,
                    resultUrl: r?.url ? `${API_HOST}${String(r.url)}` : undefined,
                    jobId: String(r?.id || ''), // Important: Store ID for deletion
                }));
                if (recent.length > 0) {
                    setQueue((prev) => (prev.length === 0 ? recent : prev));
                }
            }
        } catch (e) {
            console.error("Failed to fetch recent reports", e);
        }
    };

    const formatElapsed = (ms: number) => {
        const sec = Math.max(0, Math.floor(ms / 1000));
        const m = Math.floor(sec / 60);
        const s = sec % 60;
        return m > 0 ? `${m}m ${String(s).padStart(2, '0')}s` : `${s}s`;
    };

    const resolveReportUrlBySubmission = async (submissionId?: string): Promise<string | null> => {
        const sid = String(submissionId || "").trim();
        if (!sid) return null;
        try {
            const response = await fetch(`${API_HOST}/api/reports?page=1&page_size=8&search=${encodeURIComponent(sid)}`);
            if (!response.ok) return null;
            const data = await parseResponseSafely(response);
            const rows = Array.isArray(data) ? data : (Array.isArray(data?.items) ? data.items : []);
            const hit = (rows as ReportListItemLike[]).find((row) => String(row?.id || "") === sid && row?.url);
            if (!hit?.url) return null;
            return `${API_HOST}${String(hit.url)}`;
        } catch {
            return null;
        }
    };

    const markItemDone = (index: number, resultUrl?: string | null) => {
        setQueue(prev => {
            const copy = [...prev];
            if (copy[index]) {
                copy[index] = {
                    ...copy[index],
                    status: 'done',
                    resultUrl: resultUrl || copy[index].resultUrl,
                    error: undefined,
                };
            }
            return copy;
        });
    };

    const tryRecoverCompletedJob = async (index: number, submissionId?: string): Promise<boolean> => {
        const reportUrl = await resolveReportUrlBySubmission(submissionId);
        if (!reportUrl) return false;
        markItemDone(index, reportUrl);
        return true;
    };

    const refreshItemStatus = async (index: number) => {
        const item = queueRef.current[index];
        if (!item) return;
        const submissionId = String(item.submissionId || "").trim();
        const jobId = String(item.jobId || "").trim();

        const setInlineError = (message: string) => {
            setQueue(prev => {
                const copy = [...prev];
                if (copy[index]) copy[index] = { ...copy[index], error: message };
                return copy;
            });
        };

        try {
            if (jobId) {
                const response = await fetch(`${API_HOST}/api/jobs/${jobId}`);
                const data = await parseResponseSafely(response);
                if (response.ok && data && typeof data === 'object' && 'status' in data) {
                    const jobData = data as JobStatusLike;
                    const jobStatus = String(jobData.status || '');
                    if (jobStatus === 'completed') {
                        const directResultUrl = jobData.result_url ? `${API_HOST}${String(jobData.result_url)}` : null;
                        if (directResultUrl) {
                            markItemDone(index, directResultUrl);
                            return;
                        }
                        if (await tryRecoverCompletedJob(index, submissionId)) return;
                        setInlineError('Finalizing report file, please refresh again shortly.');
                        return;
                    }
                    if (jobStatus === 'failed') {
                        setQueue(prev => {
                            const copy = [...prev];
                            if (copy[index]) {
                                copy[index] = {
                                    ...copy[index],
                                    status: 'error',
                                    error: String(jobData.error || 'Job failed on server'),
                                };
                            }
                            return copy;
                        });
                        return;
                    }
                    if (jobStatus === 'queued' || jobStatus === 'processing') {
                        setQueue(prev => {
                            const copy = [...prev];
                            if (copy[index]) {
                                copy[index] = {
                                    ...copy[index],
                                    status: jobStatus,
                                    startedAt: copy[index].startedAt || Date.now(),
                                    error: undefined,
                                };
                            }
                            return copy;
                        });
                        return;
                    }
                }
            }

            if (await tryRecoverCompletedJob(index, submissionId)) return;
            setInlineError('No new status yet. Try again in a few seconds.');
        } catch (err: unknown) {
            if (await tryRecoverCompletedJob(index, submissionId)) return;
            setInlineError(`Refresh failed: ${getErrorMessage(err, 'unknown error')}`);
        }
    };

    const retryItem = async (index: number) => {
        const item = queueRef.current[index];
        if (!item) return;
        if (!(item.file instanceof File)) {
            setQueue(prev => {
                const copy = [...prev];
                if (copy[index]) {
                    copy[index] = { ...copy[index], error: 'Retry unavailable for this record. Please upload again.' };
                }
                return copy;
            });
            return;
        }
        try {
            await processItem(index, item.file);
        } catch {
            // processItem updates queue error state itself
        }
    };

    // Auto-trigger flag
    const [shouldAutoRun, setShouldAutoRun] = useState(false);

    // Refs for async access to latest state
    const queueRef = useRef(queue);
    const textRef = useRef(text);
    const preheatTextRef = useRef(preheatText);
    const isGlobalLoadingRef = useRef(isGlobalLoading);
    const prebuildTimerRef = useRef<number | null>(null);
    const lastPrebuiltTextRef = useRef("");
    const prebuildAnnotatedReferenceRef = useRef<(sourceText: string, options?: { silent?: boolean }) => Promise<boolean>>(
        async () => false,
    );
    const runBatchLogicRef = useRef<() => Promise<void>>(async () => undefined);

    // Update refs and persistence on render
    useEffect(() => {
        queueRef.current = queue;
        textRef.current = text;
        preheatTextRef.current = preheatText;
        isGlobalLoadingRef.current = isGlobalLoading;
        localStorage.setItem("reference_text", text);
        localStorage.setItem("reference_preheat_text", preheatText);
        localStorage.setItem("reference_preheat_ready", isPreheatReady ? "1" : "0");
    }, [queue, text, preheatText, isGlobalLoading, isPreheatReady]);


    const prebuildAnnotatedReference = async (
        sourceText: string,
        options?: { silent?: boolean }
    ) => {
        const normalizedText = sourceText.replace(/\s+/g, ' ').trim();
        if (normalizedText.length < 8) {
            if (!options?.silent) {
                setIsEditingPreheat(false);
                setPreheatStatus({ type: 'error', message: 'Reference text is too short. Please enter at least 8 characters.' });
            }
            return false;
        }

        const requestReference = async (wait: boolean, timeoutSec: number) => {
            const response = await fetch(`${API_HOST}/api/script-reference`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    text: normalizedText,
                    wait,
                    timeout_sec: timeoutSec,
                }),
            });
            const data = await parseResponseSafely(response);
            return { response, data };
        };

        if (!options?.silent) {
            setIsPreheating(true);
            setIsEditingPreheat(false);
            setPreheatStatus({ type: 'idle', message: '' });
            setIsPreheatReady(false);
        }

        try {
            const first = await requestReference(!options?.silent, !options?.silent ? 20 : 0);
            if (!first.response.ok) {
                const message = first.data?.detail || first.response.statusText || 'Preheat failed';
                if (!options?.silent) {
                    setPreheatStatus({ type: 'error', message });
                }
                return false;
            }
            if (!first.data || typeof first.data !== 'object' || (!('ready' in first.data) && !('status' in first.data))) {
                if (!options?.silent) {
                    setPreheatStatus({ type: 'error', message: 'Preheat API returned an invalid JSON payload.' });
                }
                return false;
            }

            let finalData = first.data;
            if (finalData?.preheat_text) {
                setPreheatText(String(finalData.preheat_text));
                setIsPreheatReady(true);
                setPreheatLastUpdated(new Date().toLocaleTimeString());
            }

            if (!options?.silent && !finalData?.ready) {
                setPreheatStatus({
                    type: 'success',
                    message: 'Preheat task submitted. Once ready, the button switches to Edit.',
                });
                const second = await requestReference(true, 55);
                if (!second.response.ok) {
                    const message = second.data?.detail || second.response.statusText || 'Preheat failed';
                    setPreheatStatus({ type: 'error', message });
                    return false;
                }
                if (!second.data || typeof second.data !== 'object' || (!('ready' in second.data) && !('status' in second.data))) {
                    setPreheatStatus({ type: 'error', message: 'Preheat API returned an invalid JSON payload.' });
                    return false;
                }
                finalData = second.data;
                if (finalData?.preheat_text) {
                    setPreheatText(String(finalData.preheat_text));
                    setIsPreheatReady(true);
                    setPreheatLastUpdated(new Date().toLocaleTimeString());
                }
            }

            lastPrebuiltTextRef.current = normalizedText;

            if (!options?.silent) {
                if (finalData?.ready) {
                    setPreheatStatus({
                        type: 'success',
                        message: 'AI preheat ready.',
                    });
                } else {
                    setPreheatStatus({
                        type: 'success',
                        message: 'AI preheat task submitted and processing in background. Please retry shortly.',
                    });
                }
            }
            return true;
        } catch (error) {
            if (!options?.silent) {
                setPreheatStatus({ type: 'error', message: 'AI preheat failed due to network/server error.' });
            }
            console.debug('Script reference prebuild request failed', error);
            return false;
        } finally {
            if (!options?.silent) {
                setIsPreheating(false);
            }
        }
    };
    prebuildAnnotatedReferenceRef.current = prebuildAnnotatedReference;

    const getUniqueFileName = (fileName: string, currentQueue: QueueItem[]) => {
        const dotIndex = fileName.lastIndexOf('.');
        const name = dotIndex !== -1 ? fileName.substring(0, dotIndex) : fileName;
        const ext = dotIndex !== -1 ? fileName.substring(dotIndex) : '';

        let newName = fileName;
        let newBaseName = name;
        let counter = 1;

        // Check against Current Queue OR Server History
        const isDuplicate = (nameToCheck: string, baseNameToCheck: string) => {
            const inQueue = currentQueue.some(item => item.file.name === nameToCheck);
            // Check if base name starts with any existing 'student_name' (loose check) or exact match
            // Simple exact match on base name vs student_name
            const inHistory = existingNames.has(baseNameToCheck);
            return inQueue || inHistory;
        };

        while (isDuplicate(newName, newBaseName)) {
            // User requested "01" style suffix
            const suffix = String(counter).padStart(2, '0');
            newBaseName = `${name}_${suffix}`;
            newName = `${newBaseName}${ext}`;
            counter++;
        }
        return newName;
    };

    const addFiles = (newFiles: File[]) => {
        const hasUploadableFile = newFiles.some((f) => {
            return isSupportedAudioFile(f) && f.size > 0 && f.size <= maxUploadSizeBytes;
        });

        setQueue(prev => {
            const updatedQueue = [...prev];
            const newItems: QueueItem[] = [];

            for (const f of newFiles) {
                if (!isSupportedAudioFile(f)) {
                    newItems.push({
                        file: { name: f.name },
                        status: 'error',
                        error: formatUploadError('unsupported file type. Please use audio files.'),
                    });
                    continue;
                }
                if (f.size <= 0) {
                    newItems.push({
                        file: { name: f.name },
                        status: 'error',
                        error: formatUploadError('empty file is not allowed.'),
                    });
                    continue;
                }
                if (f.size > maxUploadSizeBytes) {
                    newItems.push({
                        file: { name: f.name },
                        status: 'error',
                        error: formatUploadError(`file is ${formatFileSizeMB(f.size)}MB, max is ${maxUploadSizeMB}MB.`),
                    });
                    continue;
                }
                const uniqueName = getUniqueFileName(f.name, [...updatedQueue, ...newItems]);
                const renamedFile = new File([f], uniqueName, { type: f.type });
                newItems.push({
                    file: renamedFile,
                    status: 'idle'
                });
            }
            return [...prev, ...newItems];
        });
        setShouldAutoRun(hasUploadableFile);
    };

    useEffect(() => {
        if (engineMode !== 'pro') return;

        if (prebuildTimerRef.current) {
            window.clearTimeout(prebuildTimerRef.current);
        }

        prebuildTimerRef.current = window.setTimeout(() => {
            const normalizedText = textRef.current.replace(/\s+/g, ' ').trim();
            if (normalizedText.length < 8) return;
            if (normalizedText === lastPrebuiltTextRef.current) return;

            void (async () => {
                const ok = await prebuildAnnotatedReferenceRef.current(normalizedText, { silent: true });
                if (!ok) console.debug("Script reference prebuild skipped");
            })();
        }, 900);

        return () => {
            if (prebuildTimerRef.current) {
                window.clearTimeout(prebuildTimerRef.current);
                prebuildTimerRef.current = null;
            }
        };
    }, [text, engineMode]);

    useEffect(() => {
        if (engineMode !== 'pro') return;
        const normalizedText = text.replace(/\s+/g, ' ').trim();
        if (!normalizedText || normalizedText !== lastPrebuiltTextRef.current) {
            setIsPreheatReady(false);
        }
    }, [text, engineMode]);

    useEffect(() => {
        if (engineMode !== 'pro') {
            setIsEditingPreheat(false);
            setPreheatStatus({ type: 'idle', message: '' });
        }
    }, [engineMode]);

    useEffect(() => {
        if (!isEditingPreheat) return;
        const timer = window.setTimeout(() => {
            preheatTextareaRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' });
            preheatTextareaRef.current?.focus();
        }, 80);
        return () => window.clearTimeout(timer);
    }, [isEditingPreheat]);

    /**
     * Process a single item by index.
     * Async Flow: Upload -> Get Job ID -> Poll Status -> Done
     */
    const processItem = async (index: number, file: File) => {
        // 1. Upload Phase
        setQueue(prev => {
            const copy = [...prev];
            if (copy[index]) copy[index] = { ...copy[index], status: 'uploading' }; // Distinct uploading state
            return copy;
        });

        try {
            const formData = new FormData();
            formData.append('file', file);
            // Only send text in Pro mode. In Auto mode (Free Speaking), text must be empty.
            formData.append('text', engineMode === 'pro' ? textRef.current : "");
            formData.append('mode', engineMode);

            // POST to /api/upload - Returns Job ID immediately
            const response = await fetchWithTimeout(
                `${API_HOST}/api/upload`,
                {
                    method: 'POST',
                    body: formData,
                },
                UPLOAD_REQUEST_TIMEOUT_MS
            );

            if (!response.ok) {
                const errData = await parseResponseSafely(response);
                const detail = String(errData?.detail || response.statusText || `HTTP ${response.status}`).trim();
                throw new Error(formatUploadError(detail || 'unknown error'));
            }

            const data = await parseResponseSafely(response);
            const jobId = data.job_id;
            const submissionId = data.submission_id;
            const startedAt = Date.now();

            // Update to Queued state
            setQueue(prev => {
                const copy = [...prev];
                if (copy[index]) {
                    copy[index] = {
                        ...copy[index],
                        status: 'queued',
                        jobId: jobId,
                        submissionId,
                        startedAt,
                        error: undefined,
                    };
                }
                return copy;
            });
            void refreshUploadStorageStats();

            // 2. Polling Phase (Detached)
            let transientFailureCount = 0;
            const MAX_TRANSIENT_FAILURES = 30; // ~60s with 2s interval
            const isTransientMessage = (message: string) =>
                /failed to fetch|networkerror|bad gateway|temporarily unavailable|unexpected token|transient:/i.test(message);

            const poll = async () => {
                try {
                    const statusRes = await fetchWithTimeout(
                        `${API_HOST}/api/jobs/${jobId}`,
                        undefined,
                        JOB_POLL_REQUEST_TIMEOUT_MS
                    );
                    const statusData = await parseResponseSafely(statusRes);
                    if (!statusRes.ok) {
                        if (await tryRecoverCompletedJob(index, submissionId)) {
                            return;
                        }
                        const detail = statusData?.detail || statusRes.statusText || '';
                        const isTransientHttp = [502, 503, 504].includes(statusRes.status);
                        if (isTransientHttp) {
                            throw new Error(`TRANSIENT:${statusRes.status}:${detail}`);
                        }
                        throw new Error(`Server Error ${statusRes.status}: ${detail}`);
                    }

                    const jobData = statusData;
                    if (!jobData || typeof jobData !== 'object' || !('status' in jobData)) {
                        throw new Error('TRANSIENT:invalid job response payload');
                    }
                    transientFailureCount = 0;

                    // Update UI state based on job status
                    if (jobData.status === 'completed') {
                        const directResultUrl = jobData.result_url ? `${API_HOST}${jobData.result_url}` : null;
                        if (directResultUrl) {
                            markItemDone(index, directResultUrl);
                            return; // Stop polling
                        }
                        if (await tryRecoverCompletedJob(index, submissionId)) {
                            return;
                        }
                        // Rare race: completed but report file hasn't shown up yet.
                        setTimeout(poll, 1200);
                        return;
                    } else if (jobData.status === 'failed') {
                        throw new Error(jobData.error || "Job failed on server");
                    } else {
                        // Still queued or processing
                        setQueue(prev => {
                            const copy = [...prev];
                            if (copy[index]) {
                                copy[index] = {
                                    ...copy[index],
                                    status: jobData.status, // "queued" or "processing"
                                    error: undefined,
                                };
                            }
                            return copy;
                        });

                        // Keep polling with delay (Dynamic backoff could be added here)
                        setTimeout(poll, 2000);
                    }
                } catch (err: unknown) {
                    if (await tryRecoverCompletedJob(index, submissionId)) {
                        return;
                    }
                    const message = getErrorMessage(err, 'Polling failed');
                    if (isTransientMessage(message) && transientFailureCount < MAX_TRANSIENT_FAILURES) {
                        transientFailureCount += 1;
                        setQueue(prev => {
                            const copy = [...prev];
                            if (copy[index]) {
                                const keepStatus = copy[index].status === 'processing' ? 'processing' : 'queued';
                                copy[index] = { ...copy[index], status: keepStatus };
                            }
                            return copy;
                        });
                        setTimeout(poll, 2000);
                        return;
                    }
                    setQueue(prev => {
                        const copy = [...prev];
                        if (copy[index]) {
                            copy[index] = { ...copy[index], status: 'error', error: message };
                        }
                        return copy;
                    });
                }
            };

            // Start polling in background (do not await)
            setTimeout(poll, 1000);

            // Resolve immediately after upload to allow next upload to start ("Fast Upload")
            return;

        } catch (err: unknown) {
            const message = formatUploadError(getErrorMessage(err, 'unknown error'));
            setQueue(prev => {
                const copy = [...prev];
                if (copy[index]) {
                    copy[index] = {
                        ...copy[index],
                        status: 'error',
                        error: message,
                    };
                }
                return copy;
            });
            throw new Error(message); // Re-throw upload errors to stop batch if needed
        }
    };

    /**
     * Core batch processor using REFS to avoid stale closures
     */
    const runBatchLogic = async () => {
        if (isGlobalLoadingRef.current) return;
        setIsGlobalLoading(true);

        const currentQ = queueRef.current;
        // Identify indices that are idle
        const pendingIndices = currentQ
            .map((item, index) => ({ status: item.status, index }))
            .filter(x => x.status === 'idle')
            .map(x => x.index);

        if (pendingIndices.length === 0) {
            setIsGlobalLoading(false);
            return;
        }

        setStats({ completed: 0, total: pendingIndices.length });

        const CONCURRENCY_LIMIT = 4;
        let activeCount = 0;
        let nextPendingRefIndex = 0; // index in the pendingIndices array
        let completedCount = 0;

        return new Promise<void>((resolve) => {
            const processNext = () => {
                // Check if all tasks launched and active ones finished
                if (nextPendingRefIndex >= pendingIndices.length && activeCount === 0) {
                    setIsGlobalLoading(false);
                    resolve();
                    return;
                }

                // Launch new tasks up to limit
                while (activeCount < CONCURRENCY_LIMIT && nextPendingRefIndex < pendingIndices.length) {
                    const realQueueIndex = pendingIndices[nextPendingRefIndex];

                    // Safety check: ensure file exists in current queue state (in case of clears)
                    const item = queueRef.current[realQueueIndex];
                    if (!item || !(item.file instanceof File)) {
                        nextPendingRefIndex++;
                        continue;
                    }

                    nextPendingRefIndex++;
                    activeCount++;

                    // Pass file explicitly to avoid lookup issues later
                    processItem(realQueueIndex, item.file).finally(() => {
                        activeCount--;
                        completedCount++;
                        setStats(prev => ({ ...prev, completed: completedCount }));
                        // Recursive tick
                        processNext();
                    });
                }
            };

            // Start loop
            processNext();
        });
    };
    runBatchLogicRef.current = runBatchLogic;

    // Public trigger
    const runBatch = () => {
        runBatchLogic();
    };

    // Auto-Run Effect
    useEffect(() => {
        if (shouldAutoRun && !isGlobalLoading) {
            // Debounce
            const timer = setTimeout(() => {
                void runBatchLogicRef.current();
                setShouldAutoRun(false);
            }, 500);
            return () => clearTimeout(timer);
        }
    }, [shouldAutoRun, isGlobalLoading]);

    const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
        if (e.target.files && e.target.files.length > 0) {
            addFiles(Array.from(e.target.files));
        }
        // Allow selecting the same file again so change event still fires.
        e.target.value = '';
    };

    const handleDrop = (e: React.DragEvent) => {
        e.preventDefault();
        e.stopPropagation();
        if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
            addFiles(Array.from(e.dataTransfer.files));
        }
    };

    const removeFile = async (targetKey: string) => {
        const snapshot = queueRef.current;
        const index = snapshot.findIndex((row) => getQueueItemKey(row) === targetKey);
        const item = index >= 0 ? snapshot[index] : null;
        if (!item) return;

        // Optimistic UI update
        setQueue((prev) => {
            const hit = prev.findIndex((row) => getQueueItemKey(row) === targetKey);
            if (hit < 0) return prev;
            return prev.filter((_, i) => i !== hit);
        });
        setDeleteStatus({ type: 'idle', message: '' });

        // Prefer submissionId for report deletion; fallback to jobId for legacy rows.
        const targetId = String(item.submissionId || item.jobId || '').trim();
        if (targetId) {
            try {
                const response = await fetchWithTimeout(
                    `${API_HOST}/api/reports/${targetId}`,
                    { method: 'DELETE' }
                );
                if (!response.ok) {
                    const data = await parseResponseSafely(response);
                    const detail = String(data?.detail || response.statusText || `HTTP ${response.status}`).trim();
                    throw new Error(detail || 'Delete request failed');
                }
                void refreshUploadStorageStats();
            } catch (e) {
                console.error("Failed to delete from server", e);
                setDeleteStatus({ type: 'error', message: formatActionError('Delete', e, 'Unknown error') });
                // Roll back optimistic removal when server deletion fails.
                setQueue((prev) => {
                    const key = getQueueItemKey(item);
                    const alreadyExists = prev.some((row) => getQueueItemKey(row) === key);
                    if (alreadyExists) return prev;
                    const copy = [...prev];
                    const restoreIndex = Math.max(0, Math.min(index, copy.length));
                    copy.splice(restoreIndex, 0, item);
                    return copy;
                });
            }
        }
    };
    const clearAllAndDelete = async () => {
        setDeleteStatus({ type: 'idle', message: '' });
        let canClearLocalList = true;
        // Collect IDs to delete
        const idsToDelete = Array.from(
            new Set(
                queue
                    .map((item) => String(item.submissionId || item.jobId || '').trim())
                    .filter(Boolean)
            )
        );

        if (idsToDelete.length > 0) {
            try {
                const response = await fetchWithTimeout(
                    `${API_HOST}/api/reports/batch-delete`,
                    {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ ids: idsToDelete }),
                    }
                );
                if (!response.ok) {
                    const data = await parseResponseSafely(response);
                    const detail = String(data?.detail || response.statusText || `HTTP ${response.status}`).trim();
                    throw new Error(detail || 'Batch delete request failed');
                }
                const data = await parseResponseSafely(response);
                const errorCount = Array.isArray(data?.errors) ? data.errors.length : 0;
                if (errorCount > 0) {
                    canClearLocalList = false;
                    setDeleteStatus({
                        type: 'error',
                        message: `Batch delete partially failed: ${errorCount} item(s) were not deleted on server.`,
                    });
                }
            } catch (e) {
                console.error("Batch delete failed", e);
                setDeleteStatus({ type: 'error', message: formatActionError('Batch delete', e, 'Unknown error') });
                canClearLocalList = false;
            }
        }
        if (canClearLocalList) {
            setQueue([]);
        }
        void refreshUploadStorageStats();
    };

    const handleConfirmAction = async () => {
        if (!confirmAction || isConfirmingAction) return;
        setIsConfirmingAction(true);
        try {
            if (confirmAction.kind === 'remove_one') {
                await removeFile(confirmAction.targetKey);
            } else {
                await clearAllAndDelete();
            }
        } finally {
            setIsConfirmingAction(false);
            setConfirmAction(null);
        }
    };

    const closeConfirmAction = () => {
        if (isConfirmingAction) return;
        setConfirmAction(null);
    };

    const openReport = (url?: string) => {
        if (url) window.open(url, '_blank');
    };

    const retryItemsByIndices = (indices: number[]) => {
        if (!indices.length) return;
        const targetSet = new Set(indices);
        setQueue(prev => prev.map((item, index) => {
            if (!targetSet.has(index)) return item;
            return {
                ...item,
                status: 'idle',
                error: undefined,
                startedAt: undefined,
            };
        }));
        setShouldAutoRun(true);
    };

    const retryFailedItems = (limit?: number) => {
        const current = queueRef.current;
        const retryableIndices = current
            .map((item, index) => ({ item, index }))
            .filter(({ item }) => item.status === 'error' && item.file instanceof File)
            .map(({ index }) => index);
        const targetIndices = typeof limit === 'number'
            ? retryableIndices.slice(0, Math.max(0, limit))
            : retryableIndices;
        retryItemsByIndices(targetIndices);
    };

    const retryVisibleFailedItems = () => {
        const visibleFailedIndices = visibleQueueRows
            .filter(({ item }) => item.status === 'error' && item.file instanceof File)
            .map(({ idx }) => idx);
        retryItemsByIndices(visibleFailedIndices);
    };

    const clearFailedFromList = () => {
        if (failedCount <= 0) return;
        setQueue(prev => prev.filter(item => item.status !== 'error'));
        setShowOnlyFailed(false);
        setFailedKeywordFilter('all');
    };

    const refreshActiveItems = async () => {
        const current = queueRef.current;
        const activeIndices = current
            .map((item, index) => ({ item, index }))
            .filter(({ item }) => item.status === 'queued' || item.status === 'processing')
            .map(({ index }) => index);
        await Promise.all(activeIndices.map(index => refreshItemStatus(index)));
    };

    const refreshStuckItems = async () => {
        if (isRefreshingStuck) return;
        const now = Date.now();
        const current = queueRef.current;
        const stuckIndices = current
            .map((item, index) => ({ item, index }))
            .filter(({ item }) => {
                const isActive = item.status === 'queued' || item.status === 'processing';
                if (!isActive || typeof item.startedAt !== 'number') return false;
                return (now - item.startedAt) >= LONG_WAIT_MS;
            })
            .map(({ index }) => index);
        if (stuckIndices.length === 0) return;
        setIsRefreshingStuck(true);
        try {
            await Promise.all(stuckIndices.map(index => refreshItemStatus(index)));
            setLastStuckRefreshAt(Date.now());
        } finally {
            setIsRefreshingStuck(false);
        }
    };

    const handlePreheatAction = async () => {
        if (isPreheatReady) {
            setIsEditingPreheat(true);
            return;
        }
        await prebuildAnnotatedReference(textRef.current);
    };

    const preheatActionLabel = isPreheating
        ? 'Preheating...'
        : (isPreheatReady ? '编辑' : '预热文本');

    const failedCount = queue.filter(item => item.status === 'error').length;
    const activeCount = queue.filter(item => item.status === 'queued' || item.status === 'processing').length;
    const longWaitingCount = queue.filter(item => {
        const isActive = item.status === 'queued' || item.status === 'processing';
        return isActive && typeof item.startedAt === 'number' && (tickNow - item.startedAt) >= LONG_WAIT_MS;
    }).length;
    const stuckCount = queue.filter(item => {
        const isActive = item.status === 'queued' || item.status === 'processing';
        return isActive && typeof item.startedAt === 'number' && (tickNow - item.startedAt) >= STUCK_WAIT_MS;
    }).length;
    const retryableFailedCount = queue.filter(item => item.status === 'error' && item.file instanceof File).length;
    const failedKeywordOptions: Array<{ key: FailedKeywordFilter; label: string }> = [
        { key: 'all', label: 'All' },
        { key: 'timeout', label: 'Timeout' },
        { key: 'network', label: 'Network' },
        { key: 'server', label: 'Server' },
        { key: 'unknown', label: 'Unknown' },
    ];
    const matchesFailedKeyword = (message: string): boolean => {
        const text = String(message || '').toLowerCase();
        if (failedKeywordFilter === 'all') return true;
        if (failedKeywordFilter === 'timeout') {
            return text.includes('timeout') || text.includes('timed out') || text.includes('abort');
        }
        if (failedKeywordFilter === 'network') {
            return text.includes('network') || text.includes('failed to fetch') || text.includes('connection');
        }
        if (failedKeywordFilter === 'server') {
            return text.includes('500') || text.includes('502') || text.includes('503') || text.includes('504') || text.includes('server');
        }
        return text.includes('unknown') || text.trim().length === 0;
    };

    const toFailedSummaryKey = (message: string): string => {
        const text = String(message || '').toLowerCase().replace(/\s+/g, ' ').trim();
        if (!text) return 'unknown error';
        if (text.includes('unsupported file type')) return 'unsupported file type';
        if (text.includes('empty file')) return 'empty file';
        if (text.includes('max is') && text.includes('mb')) return 'file too large';
        if (text.includes('timeout') || text.includes('timed out') || text.includes('abort')) return 'request timeout';
        if (text.includes('failed to fetch') || text.includes('network') || text.includes('connection')) return 'network error';
        if (text.includes('job failed on server') || text.includes('job failed')) return 'analysis failed on server';

        const statusCodeMatch = text.match(/\b([45]\d{2})\b/);
        if (statusCodeMatch?.[1]) return `server error ${statusCodeMatch[1]}`;
        if (text.includes('server error')) return 'server error';
        if (text.includes('unknown error')) return 'unknown error';
        return text.length > 80 ? `${text.slice(0, 80)}...` : text;
    };

    const failedSummaryEntries = Object.entries(
        queue.reduce((acc, item) => {
            if (item.status !== 'error') return acc;
            const groupedKey = toFailedSummaryKey(String(item.error || ''));
            acc[groupedKey] = (acc[groupedKey] || 0) + 1;
            return acc;
        }, {} as Record<string, number>)
    ).sort((a, b) => b[1] - a[1]);
    const failedSummaryTop = failedSummaryEntries.slice(0, 4);
    const failedSummaryText = failedSummaryEntries
        .map(([message, count]) => `${count}x ${message}`)
        .join('\n');
    const failedNamesText = queue
        .filter((item) => item.status === 'error')
        .map((item) => String(item.file?.name || '').replace(/\.[^/.]+$/, '').trim())
        .filter(Boolean)
        .join('\n');
    const visibleQueueRows = queue
        .map((item, idx) => ({ item, idx }))
        .filter(({ item }) => {
            if (showOnlyFailed) {
                if (item.status !== 'error') return false;
                return matchesFailedKeyword(String(item.error || ''));
            }
            if (showOnlyUnfinished) return item.status !== 'done';
            return true;
        });
    const hiddenCount = queue.length - visibleQueueRows.length;
    const hiddenLabel = showOnlyFailed ? 'Filtered out' : (showOnlyUnfinished ? 'Hidden done' : 'Hidden');
    const visibleRetryableFailedCount = visibleQueueRows.filter(({ item }) => item.status === 'error' && item.file instanceof File).length;
    const visibleFailedIndices = visibleQueueRows.filter(({ item }) => item.status === 'error').map(({ idx }) => idx);
    const visibleFailedCount = visibleFailedIndices.length;

    const clearVisibleFailedFromList = () => {
        if (visibleFailedCount <= 0) return;
        const targetSet = new Set(visibleFailedIndices);
        setQueue(prev => prev.filter((_, index) => !targetSet.has(index)));
    };

    const copyFailedSummary = async () => {
        if (!failedSummaryText || isCopyingFailedSummary) return;
        setIsCopyingFailedSummary(true);
        try {
            await navigator.clipboard.writeText(failedSummaryText);
            setFailedSummaryCopiedAt(Date.now());
        } catch (err) {
            console.error('Failed to copy failed summary', err);
        } finally {
            setIsCopyingFailedSummary(false);
        }
    };

    const copyFailedNames = async () => {
        if (!failedNamesText || isCopyingFailedNames) return;
        setIsCopyingFailedNames(true);
        try {
            await navigator.clipboard.writeText(failedNamesText);
            setFailedNamesCopiedAt(Date.now());
        } catch (err) {
            console.error('Failed to copy failed names', err);
        } finally {
            setIsCopyingFailedNames(false);
        }
    };

    if (!isOpen) return null;

    return (
        <div className={`fixed inset-0 z-[100] flex ${isMobile ? 'items-stretch justify-stretch p-0' : 'items-center justify-center p-4'} bg-black/80 backdrop-blur-sm animate-fade-in`}>
            <div className={`bg-[#0A0A0B] border border-white/10 ${isMobile ? 'rounded-none border-x-0 border-y-0 h-[100dvh] w-full p-4' : 'rounded-2xl w-full max-w-[min(96vw,1200px)] p-6 max-h-[92vh]'} shadow-2xl relative overflow-hidden flex flex-col`}>
                <div className="absolute top-0 right-0 w-64 h-64 bg-primary/10 rounded-full blur-[100px] -z-10"></div>

                {/* Header */}
                <div className={`flex ${isMobile ? 'flex-col items-start gap-3' : 'justify-between items-center'} mb-6 shrink-0`}>
                    <div className={`flex ${isMobile ? 'flex-col items-start gap-2' : 'items-center gap-4'}`}>
                        <h2 className="text-xl font-bold text-white flex items-center gap-2">
                            <Upload className="w-5 h-5 text-primary" />
                            Batch Upload
                        </h2>
                        {isGlobalLoading && (
                            <div className="text-xs font-bold text-primary bg-primary/10 px-3 py-1 rounded-full animate-pulse border border-primary/20">
                                COMPLETED {stats.completed} / {stats.total}
                            </div>
                        )}
                        {!isGlobalLoading && queue.length > 0 && (
                            <>
                                {longWaitingCount > 0 && (
                                    <button
                                        onClick={() => void refreshStuckItems()}
                                        disabled={isRefreshingStuck}
                                        className="text-xs text-amber-300 hover:text-amber-200 transition-colors font-bold uppercase tracking-wider underline underline-offset-4 disabled:opacity-60 disabled:cursor-not-allowed"
                                        title={`Re-check ${longWaitingCount} long-wait item(s)`}
                                    >
                                        {isRefreshingStuck ? 'Re-checking...' : `Re-check Stuck (${longWaitingCount})`}
                                    </button>
                                )}
                                {activeCount > 0 && (
                                    <button
                                        onClick={() => void refreshActiveItems()}
                                        className="text-xs text-cyan-300 hover:text-cyan-200 transition-colors font-bold uppercase tracking-wider underline underline-offset-4"
                                        title={`Refresh ${activeCount} active item(s)`}
                                    >
                                        Refresh Active ({activeCount})
                                    </button>
                                )}
                                {retryableFailedCount > 0 && (
                                    <button
                                        onClick={() => retryFailedItems()}
                                        className="text-xs text-rose-300 hover:text-rose-200 transition-colors font-bold uppercase tracking-wider underline underline-offset-4"
                                        title={`Retry ${retryableFailedCount} failed item(s)`}
                                    >
                                        Retry Failed ({retryableFailedCount})
                                    </button>
                                )}
                                {showOnlyFailed && visibleRetryableFailedCount > 0 && (
                                    <button
                                        onClick={retryVisibleFailedItems}
                                        className="text-xs text-rose-200/90 hover:text-rose-100 transition-colors font-bold uppercase tracking-wider underline underline-offset-4"
                                        title={`Retry currently visible ${visibleRetryableFailedCount} failed item(s)`}
                                    >
                                        Retry Visible ({visibleRetryableFailedCount})
                                    </button>
                                )}
                                {retryableFailedCount > 3 && (
                                    <button
                                        onClick={() => retryFailedItems(3)}
                                        className="text-xs text-rose-200/90 hover:text-rose-100 transition-colors font-bold uppercase tracking-wider underline underline-offset-4"
                                        title="Retry only first 3 failed item(s)"
                                    >
                                        Retry First 3
                                    </button>
                                )}
                                {retryableFailedCount > 10 && (
                                    <button
                                        onClick={() => retryFailedItems(10)}
                                        className="text-xs text-rose-200/90 hover:text-rose-100 transition-colors font-bold uppercase tracking-wider underline underline-offset-4"
                                        title="Retry only first 10 failed item(s)"
                                    >
                                        Retry First 10
                                    </button>
                                )}
                                {failedCount > 0 && (
                                    <button
                                        onClick={clearFailedFromList}
                                        className="text-xs text-gray-400 hover:text-gray-200 transition-colors font-bold uppercase tracking-wider underline underline-offset-4"
                                        title="Remove failed rows from current list"
                                    >
                                        Clear Failed ({failedCount})
                                    </button>
                                )}
                                {showOnlyFailed && visibleFailedCount > 0 && (
                                    <button
                                        onClick={clearVisibleFailedFromList}
                                        className="text-xs text-gray-400 hover:text-gray-200 transition-colors font-bold uppercase tracking-wider underline underline-offset-4"
                                        title="Remove only currently visible failed rows"
                                    >
                                        Clear Visible Failed ({visibleFailedCount})
                                    </button>
                                )}
                                <button
                                    onClick={() => setConfirmAction({ kind: 'clear_all' })}
                                    className="text-xs text-gray-500 hover:text-red-400 transition-colors font-bold uppercase tracking-wider underline underline-offset-4"
                                >
                                    Clear All (Delete)
                                </button>
                            </>
                        )}
                    </div>
                    <div className={`flex items-center gap-3 ${isMobile ? 'self-end' : ''}`}>
                        <button
                            onClick={() => { onClose(); window.location.href = '/history'; }}
                            className="text-xs font-bold text-primary hover:text-white transition-colors flex items-center gap-1.5 bg-primary/10 px-3 py-1.5 rounded-lg border border-primary/20"
                        >
                            <HistoryIcon className="w-4 h-4" />
                            VIEW HISTORY
                        </button>
                        <button onClick={onClose} className="text-gray-400 hover:text-white transition-colors">
                            <X className="w-5 h-5" />
                        </button>
                    </div>
                </div>

                {longWaitingCount > 0 && (
                    <div className={`mb-3 shrink-0 rounded-xl border border-amber-300/30 bg-amber-500/10 px-3 py-2 ${isMobile ? 'space-y-2' : 'flex items-center justify-between gap-3'}`}>
                        <div className="text-xs text-amber-100">
                            {longWaitingCount} active item(s) have been running for over 2 minutes.
                            {stuckCount > 0 ? ` ${stuckCount} item(s) are over 5 minutes and may be stuck.` : ''}
                            {lastStuckRefreshAt ? ` Last re-check: ${new Date(lastStuckRefreshAt).toLocaleTimeString()}.` : ''}
                        </div>
                        <div className="flex items-center gap-3 text-xs">
                            <button
                                type="button"
                                onClick={() => void refreshStuckItems()}
                                disabled={isRefreshingStuck}
                                className="underline underline-offset-2 text-amber-200 hover:text-amber-100 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
                            >
                                {isRefreshingStuck ? 'Re-checking...' : `Re-check Stuck (${longWaitingCount})`}
                            </button>
                            <button
                                type="button"
                                onClick={() => void refreshActiveItems()}
                                className="underline underline-offset-2 text-cyan-200 hover:text-cyan-100 transition-colors"
                            >
                                Refresh All Active
                            </button>
                            <button
                                type="button"
                                onClick={() => { onClose(); window.location.href = '/history'; }}
                                className="underline underline-offset-2 text-amber-200 hover:text-amber-100 transition-colors"
                            >
                                Check History
                            </button>
                        </div>
                    </div>
                )}

                {deleteStatus.type === 'error' && deleteStatus.message ? (
                    <div className="mb-3 shrink-0 rounded-xl border border-red-300/30 bg-red-500/10 px-3 py-2 text-xs text-red-100">
                        {deleteStatus.message}
                    </div>
                ) : null}

                {uploadStorageWarning ? (
                    <div className="mb-3 shrink-0 rounded-xl border border-amber-300/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-100">
                        {uploadStorageWarning}
                    </div>
                ) : null}

                {failedCount > 0 && (
                    <div className={`mb-3 shrink-0 rounded-xl border border-rose-300/30 bg-rose-500/10 px-3 py-2 ${isMobile ? 'space-y-2' : 'flex items-start justify-between gap-3'}`}>
                        <div className="min-w-0">
                            <div className="text-xs font-bold text-rose-200">
                                Failed summary ({failedCount})
                            </div>
                            <div className="mt-1 flex flex-wrap gap-1.5">
                                {failedSummaryTop.map(([message, count]) => (
                                    <span key={`${count}-${message}`} className="max-w-full truncate rounded-md border border-rose-300/20 bg-black/20 px-2 py-1 text-[11px] text-rose-100">
                                        {count}x {message}
                                    </span>
                                ))}
                            </div>
                        </div>
                        <div className="flex items-center gap-3 text-xs shrink-0">
                            {failedSummaryCopiedAt ? (
                                <span className="text-rose-200/80">
                                    Summary copied {new Date(failedSummaryCopiedAt).toLocaleTimeString()}
                                </span>
                            ) : null}
                            {failedNamesCopiedAt ? (
                                <span className="text-rose-200/80">
                                    Names copied {new Date(failedNamesCopiedAt).toLocaleTimeString()}
                                </span>
                            ) : null}
                            <button
                                type="button"
                                onClick={() => void copyFailedSummary()}
                                disabled={isCopyingFailedSummary}
                                className="underline underline-offset-2 text-rose-200 hover:text-rose-100 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
                            >
                                {isCopyingFailedSummary ? 'Copying...' : 'Copy failed summary'}
                            </button>
                            <button
                                type="button"
                                onClick={() => void copyFailedNames()}
                                disabled={isCopyingFailedNames}
                                className="underline underline-offset-2 text-rose-200 hover:text-rose-100 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
                            >
                                {isCopyingFailedNames ? 'Copying...' : 'Copy failed names'}
                            </button>
                        </div>
                    </div>
                )}

                <div className={`flex-1 overflow-y-auto min-h-0 space-y-6 ${isMobile ? 'pr-0' : 'pr-2'}`}>
                    {/* Step 1: Engine Mode Selection (Moved to Top) */}
                    <div className="shrink-0 space-y-2">
                        <div className="flex items-center gap-2">
                            <span className="bg-primary text-background text-xs font-bold px-2 py-0.5 rounded">STEP 1</span>
                            <label className="text-sm font-bold text-gray-300">Select Engine Mode</label>
                        </div>
                        <div className={`${isMobile ? 'grid grid-cols-1 gap-3' : 'flex gap-4'}`}>
                            <button
                                onClick={() => setEngineMode('auto')}
                                className={`flex-1 p-4 rounded-xl border transition-all duration-300 text-left group
                                    ${engineMode === 'auto' ? 'bg-primary/10 border-primary shadow-lg shadow-primary/5' : 'bg-white/5 border-white/10 hover:border-white/20'}`}
                            >
                                <div className="flex items-center justify-between mb-2">
                                    <div className={`p-2 rounded-lg ${engineMode === 'auto' ? 'bg-primary text-black' : 'bg-white/5 text-gray-400'}`}>
                                        <Zap className="w-5 h-5" />
                                    </div>
                                    {engineMode === 'auto' && <div className="w-2 h-2 bg-primary rounded-full animate-pulse"></div>}
                                </div>
                                <h4 className="font-bold text-white">Free Talk Mode</h4>
                                <p className="text-xs text-gray-500 mt-1">No script required. AI transcribes speech and evaluates fluency and pronunciation.</p>
                            </button>

                            <button
                                onClick={() => setEngineMode('pro')}
                                className={`flex-1 p-4 rounded-xl border transition-all duration-300 text-left group
                                    ${engineMode === 'pro' ? 'bg-purple-500/10 border-purple-500 shadow-lg shadow-purple-500/5' : 'bg-white/5 border-white/10 hover:border-white/20'}`}
                            >
                                <div className="flex items-center justify-between mb-2">
                                    <div className={`p-2 rounded-lg ${engineMode === 'pro' ? 'bg-purple-500 text-white' : 'bg-white/5 text-gray-400'}`}>
                                        <Code className="w-5 h-5" />
                                    </div>
                                    {engineMode === 'pro' && <div className="w-2 h-2 bg-purple-500 rounded-full animate-pulse"></div>}
                                </div>
                                <h4 className="font-bold text-white">Reference Mode</h4>
                                <p className="text-xs text-gray-500 mt-1">Script required. AI provides word-level correction for recitation and reading tasks.</p>
                            </button>
                        </div>
                    </div>

                    {/* Step 2: Reference Text (Conditional) */}
                    {engineMode === 'pro' && (
                        <div className="shrink-0 space-y-2 animate-fade-in-up">
                            <div className="flex items-center gap-2">
                                <span className="bg-primary text-background text-xs font-bold px-2 py-0.5 rounded">STEP 2</span>
                                <label className="text-sm font-bold text-gray-300">Set Reference Text</label>
                                <span className="text-xs text-primary font-bold">(Used for scoring alignment)</span>
                                <div className="ml-auto flex items-center gap-2">
                                    <button
                                        type="button"
                                        onClick={() => void handlePreheatAction()}
                                        disabled={isPreheating}
                                        className={`text-xs px-2.5 py-1 rounded border disabled:opacity-60 disabled:cursor-not-allowed transition-colors flex items-center gap-1 ${
                                            isPreheatReady
                                                ? 'border-amber-300/50 bg-amber-500/15 text-amber-100 hover:bg-amber-500/25'
                                                : 'border-cyan-300/40 bg-cyan-500/10 text-cyan-100 hover:bg-cyan-500/20'
                                        }`}
                                    >
                                        {isPreheating ? <Loader2 className="w-3 h-3 animate-spin" /> : null}
                                        {preheatActionLabel}
                                    </button>
                                </div>
                            </div>
                            <textarea
                                value={text}
                                onChange={(e) => setText(e.target.value)}
                                className="w-full border rounded-xl p-4 text-gray-300 h-32 resize-none placeholder-gray-500 transition-all font-mono text-sm leading-relaxed bg-white/5 border-white/10 focus:outline-none focus:border-primary/50 focus:ring-1 focus:ring-primary/50"
                                placeholder="Paste script here. Example: Climate change is a long-term shift..."
                            />
                            {preheatStatus.type !== 'idle' && (
                                <p className={`text-[11px] px-1 ${preheatStatus.type === 'success' ? 'text-cyan-300' : 'text-rose-300'}`}>
                                    {preheatStatus.message}
                                </p>
                            )}
                            {preheatLastUpdated && (
                                <p className="text-[11px] text-cyan-300 px-1">
                                    Preheat last updated: {preheatLastUpdated}
                                </p>
                            )}
                            <p className="text-[11px] text-gray-500 px-1">
                                Input text is preprocessed first, so later audio scoring is faster and more stable.
                            </p>
                        </div>
                    )}
                    {/* Step 3: Upload Area */}
                    <div className="space-y-2 shrink-0">
                        <div className="flex items-center gap-2">
                            <span className="text-xs font-bold px-2 py-0.5 rounded transition-colors bg-primary text-background">
                                STEP {engineMode === 'pro' ? '3' : '2'}
                            </span>
                            <label className="text-sm font-bold text-gray-300">Upload Audio Files</label>
                        </div>

                        <div
                            className={`border-2 border-dashed rounded-xl p-8 text-center transition-all duration-300 cursor-pointer flex flex-col items-center justify-center gap-4 relative overflow-hidden
                                ${queue.length > 0 ? 'border-primary/30 bg-primary/5 py-4' : 'border-white/10 hover:border-primary/50 hover:bg-white/5 py-8'}`}
                            onDragOver={(e) => {
                                e.preventDefault();
                                e.stopPropagation();
                            }}
                            onDrop={handleDrop}
                            onClick={() => {
                                if (fileInputRef.current) fileInputRef.current.value = '';
                                fileInputRef.current?.click();
                            }}
                        >
                            <input
                                type="file"
                                ref={fileInputRef}
                                onChange={handleFileChange}
                                accept="audio/*"
                                multiple
                                className="hidden"
                            />
                            <div className="p-4 bg-white/5 rounded-full ring-1 ring-white/10 shadow-lg">
                                <Upload className="w-8 h-8 text-gray-400" />
                            </div>
                            <div className="space-y-1">
                                <p className="text-lg font-medium text-white">
                                    Drop files here to <span className="text-primary font-bold">Auto-Start</span>
                                </p>
                                <p className="text-sm text-gray-500">
                                    {engineMode === 'pro'
                                        ? `WAV/MP3, max ${maxUploadSizeMB}MB each`
                                        : `Auto-dictation enabled, max ${maxUploadSizeMB}MB each`}
                                </p>
                                {uploadStorageStats ? (
                                    <p className={`text-xs ${uploadStorageStats.overWarn ? 'text-amber-300' : 'text-gray-500'}`}>
                                        Backend audio storage: {uploadStorageStats.uploadsFileCount} files / {uploadStorageStats.uploadsGb.toFixed(2)}GB
                                        {uploadStorageStats.overWarn ? ` (warning threshold ${uploadStorageStats.warnGb.toFixed(0)}GB)` : ''}
                                    </p>
                                ) : null}
                            </div>
                        </div>
                    </div>

                    {/* File List */}
                    {queue.length > 0 && (
                        <div className="space-y-3">
                            <div className="flex items-center justify-between gap-3 px-1">
                                <label className="block text-xs font-bold text-gray-400 uppercase tracking-wider">
                                    Batch Results ({visibleQueueRows.length})
                                    {failedCount > 0 ? <span className="text-rose-300"> | Failed {failedCount}</span> : null}
                                    {hiddenCount > 0 ? <span className="text-gray-500"> | {hiddenLabel} {hiddenCount}</span> : null}
                                </label>
                                <div className="flex items-center gap-2">
                                    <button
                                        type="button"
                                        onClick={() => {
                                            setShowOnlyFailed(prev => {
                                                const next = !prev;
                                                if (!next) {
                                                    setFailedKeywordFilter('all');
                                                }
                                                return next;
                                            });
                                        }}
                                        className={`text-[11px] font-semibold px-2 py-1 rounded border transition-colors ${
                                            showOnlyFailed
                                                ? 'border-rose-300/40 bg-rose-500/10 text-rose-200'
                                                : 'border-white/15 bg-white/5 text-gray-300 hover:text-white hover:border-white/30'
                                        }`}
                                    >
                                        {showOnlyFailed ? 'Show All' : 'Failed Only'}
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => setShowOnlyUnfinished(prev => !prev)}
                                        disabled={showOnlyFailed}
                                        className={`text-[11px] font-semibold px-2 py-1 rounded border transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${
                                            showOnlyUnfinished
                                                ? 'border-cyan-300/40 bg-cyan-500/10 text-cyan-200'
                                                : 'border-white/15 bg-white/5 text-gray-300 hover:text-white hover:border-white/30'
                                        }`}
                                    >
                                        {showOnlyUnfinished ? 'Show All' : 'Hide Completed'}
                                    </button>
                                </div>
                            </div>
                            {showOnlyFailed && (
                                <div className="flex flex-wrap items-center gap-2 px-1">
                                    <span className="text-[11px] font-semibold text-gray-500 uppercase tracking-wider">Error Type</span>
                                    {failedKeywordOptions.map((opt) => (
                                        <button
                                            key={opt.key}
                                            type="button"
                                            onClick={() => setFailedKeywordFilter(opt.key)}
                                            className={`text-[11px] font-semibold px-2 py-1 rounded border transition-colors ${
                                                failedKeywordFilter === opt.key
                                                    ? 'border-rose-300/40 bg-rose-500/10 text-rose-200'
                                                    : 'border-white/15 bg-white/5 text-gray-300 hover:text-white hover:border-white/30'
                                            }`}
                                        >
                                            {opt.label}
                                        </button>
                                    ))}
                                </div>
                            )}
                            <div className="space-y-2">
                                {visibleQueueRows.length === 0 && (
                                    <div className="border border-white/10 rounded-lg px-3 py-2 text-xs text-gray-400 bg-white/5">
                                        No pending items. Click "Show All" to view completed results.
                                    </div>
                                )}
                                {visibleQueueRows.map(({ item, idx }) => {
                                    const isDone = item.status === 'done';
                                    const isProcessing = item.status === 'processing';
                                    const isQueued = item.status === 'queued';
                                    const isError = item.status === 'error';
                                    const isActive = isProcessing || isQueued;
                                    const elapsedMs = isActive && item.startedAt ? Math.max(0, tickNow - item.startedAt) : 0;
                                    const isLongWait = isActive && elapsedMs >= LONG_WAIT_MS;

                                    return (
                                        <div
                                            key={idx}
                                            onClick={() => isDone && openReport(item.resultUrl)}
                                            className={`group relative border rounded-xl p-4 flex items-center justify-between transition-all duration-300
                                                ${isDone ? 'bg-white/5 border-white/10 hover:bg-white/10 hover:border-primary/50 cursor-pointer shadow-lg hover:shadow-primary/5' :
                                                    isProcessing ? 'bg-primary/5 border-primary/20 cursor-wait' :
                                                        isQueued ? 'bg-white/5 border-white/5 border-dashed cursor-wait opacity-80' :
                                                            isError ? 'bg-red-500/5 border-red-500/20 cursor-default' :
                                                                'bg-white/5 border-white/5 cursor-default opacity-60'}`}
                                        >
                                            <div className="flex items-center gap-4 overflow-hidden relative z-10">
                                                {/* Status Icon / Spinner */}
                                                <div className={`w-10 h-10 rounded-full flex items-center justify-center shrink-0 transition-all duration-300
                                                    ${isDone ? 'bg-green-500 text-black scale-100 group-hover:scale-110' :
                                                        isError ? 'bg-red-500 text-white' :
                                                            isProcessing ? 'bg-primary text-black' :
                                                                item.status === 'uploading' ? 'bg-blue-500 text-white' :
                                                                    isQueued ? 'bg-white/10 text-white animate-pulse' :
                                                                        'bg-gray-800 text-gray-500'}`}>
                                                    {item.status === 'uploading' ? <Upload className="w-5 h-5 animate-bounce" /> :
                                                        isProcessing ? <Loader2 className="w-6 h-6 animate-spin" /> :
                                                            isQueued ? <div className="text-xs font-bold">Q</div> :
                                                                isDone ? <Check className="w-6 h-6 stroke-[3px]" /> :
                                                                    isError ? <AlertCircle className="w-6 h-6" /> :
                                                                        <FileAudio className="w-5 h-5" />}
                                                </div>

                                                <div className="min-w-0">
                                                    <div className="flex items-center gap-2">
                                                        <p className={`text-sm font-bold truncate transition-colors ${isDone ? 'text-white' : 'text-gray-400'}`}>
                                                            {item.file.name.replace(/\.[^/.]+$/, "")}
                                                        </p>
                                                        {isDone && <span className="bg-green-500/10 text-green-500 text-[10px] font-black px-1.5 py-0.5 rounded border border-green-500/20">READY</span>}
                                                        {isQueued && <span className="bg-white/10 text-gray-400 text-[10px] font-black px-1.5 py-0.5 rounded border border-white/10">QUEUED</span>}
                                                    </div>
                                                    <div className="text-xs transition-colors duration-300 flex items-center gap-1.5 mt-0.5">
                                                        {item.status === 'uploading' && <span className="text-blue-400 font-bold flex items-center gap-2"><Loader2 className="w-3 h-3 animate-spin" /> Uploading...</span>}
                                                        {isProcessing && <span className="text-primary animate-pulse flex items-center gap-2 font-bold"><span className="w-1.5 h-1.5 bg-primary rounded-full animate-ping"></span>Grading in progress... ({formatElapsed(elapsedMs)})</span>}
                                                        {isQueued && <span className="text-green-400 font-medium">Upload Success! Queued... ({formatElapsed(elapsedMs)})</span>}
                                                        {isDone && <span className="text-gray-500 font-medium">Click to view report</span>}
                                                        {isError && <span className="text-red-400 font-bold">{item.error}</span>}
                                                        {item.status === 'idle' && <span className="text-gray-600">Ready to upload</span>}
                                                    </div>
                                                    {(isLongWait || isError) && (
                                                        <div className="mt-1 text-[11px] flex items-center gap-2">
                                                            {isLongWait ? <span className="text-amber-300">Still running. You can refresh status now.</span> : null}
                                                            <button
                                                                type="button"
                                                                onClick={(e) => {
                                                                    e.stopPropagation();
                                                                    void refreshItemStatus(idx);
                                                                }}
                                                                className="underline underline-offset-2 text-cyan-300 hover:text-cyan-200 transition-colors"
                                                            >
                                                                {isLongWait ? 'Re-check now' : 'Refresh Status'}
                                                            </button>
                                                            {isError ? (
                                                                <button
                                                                    type="button"
                                                                    onClick={(e) => {
                                                                        e.stopPropagation();
                                                                        void retryItem(idx);
                                                                    }}
                                                                    className="underline underline-offset-2 text-rose-300 hover:text-rose-200 transition-colors"
                                                                >
                                                                    Retry
                                                                </button>
                                                            ) : null}
                                                            {isLongWait ? (
                                                                <button
                                                                    type="button"
                                                                    onClick={(e) => {
                                                                        e.stopPropagation();
                                                                        onClose();
                                                                        window.location.href = '/history';
                                                                    }}
                                                                    className="underline underline-offset-2 text-amber-300 hover:text-amber-200 transition-colors"
                                                                >
                                                                    Check History
                                                                </button>
                                                            ) : null}
                                                        </div>
                                                    )}
                                                </div>
                                            </div>

                                            <div className="flex items-center gap-2 shrink-0 relative z-10">
                                                {isDone && (
                                                    <div className="text-primary opacity-0 group-hover:opacity-100 transition-all duration-300 transform translate-x-2 group-hover:translate-x-0 font-bold text-xs flex items-center gap-1 bg-primary/10 px-3 py-1.5 rounded-lg border border-primary/20">
                                                        OPEN REPORT
                                                    </div>
                                                )}
                                                {!isProcessing && (
                                                    <button
                                                        onClick={(e) => {
                                                            e.stopPropagation();
                                                            setConfirmAction({
                                                                kind: 'remove_one',
                                                                targetKey: getQueueItemKey(item),
                                                                targetName: getQueueItemDisplayName(item),
                                                            });
                                                        }}
                                                        className="p-2 text-gray-600 hover:text-red-400 hover:bg-red-500/10 rounded-full transition-all duration-200"
                                                        title="Remove from list"
                                                    >
                                                        <Trash2 className="w-4 h-4" />
                                                    </button>
                                                )}
                                            </div>
                                        </div>
                                    );
                                })}
                            </div>
                        </div>
                    )}
                </div>

                {isEditingPreheat && (
                    <div className="absolute inset-0 z-30 flex items-center justify-center bg-[#06080c]/75 backdrop-blur-sm p-4">
                        <div className="w-full max-w-3xl rounded-2xl border border-cyan-300/35 bg-[#0f1621] shadow-2xl shadow-cyan-500/10">
                            <div className="flex items-center justify-between px-5 py-4 border-b border-cyan-300/20">
                                <div>
                                    <h3 className="text-base font-bold text-cyan-100">Edit Preheat Content</h3>
                                    <p className="text-xs text-cyan-300/80 mt-1">You can manually refine preprocessed text. Scoring will prioritize Gemini results when conflicts appear.</p>
                                </div>
                                <button
                                    type="button"
                                    onClick={() => setIsEditingPreheat(false)}
                                    className="text-cyan-200/80 hover:text-cyan-100 transition-colors"
                                    aria-label="Close preheat editor"
                                >
                                    <X className="w-5 h-5" />
                                </button>
                            </div>
                            <div className="p-5">
                                <textarea
                                    ref={preheatTextareaRef}
                                    value={preheatText}
                                    onChange={(e) => setPreheatText(e.target.value)}
                                    className="w-full min-h-[360px] resize-y rounded-xl border border-cyan-300/25 bg-[#0b111a] p-4 text-cyan-50 placeholder-cyan-300/50 focus:outline-none focus:ring-1 focus:ring-cyan-300/40 font-mono text-xs leading-relaxed"
                                    placeholder="Paste or edit preheated annotated text here..."
                                />
                            </div>
                            <div className="px-5 pb-5 flex justify-end gap-2">
                                <button
                                    type="button"
                                    onClick={() => setIsEditingPreheat(false)}
                                    className="px-3 py-2 text-sm rounded-lg border border-white/20 text-gray-300 hover:text-white hover:border-white/40 transition-colors"
                                >
                                    Cancel
                                </button>
                                <button
                                    type="button"
                                    onClick={() => setIsEditingPreheat(false)}
                                    className="px-4 py-2 text-sm rounded-lg border border-amber-300/45 bg-amber-500/15 text-amber-100 hover:bg-amber-500/25 transition-colors font-semibold"
                                >
                                    Done
                                </button>
                            </div>
                        </div>
                    </div>
                )}

                {confirmAction && (
                    <div className="absolute inset-0 z-40 flex items-center justify-center bg-black/65 backdrop-blur-sm p-4">
                        <div className="w-full max-w-md rounded-2xl border border-red-300/25 bg-[#111217] shadow-2xl">
                            <div className="px-5 py-4 border-b border-white/10">
                                <h3 className="text-base font-bold text-white">
                                    {confirmAction.kind === 'remove_one' ? 'Remove Item' : 'Clear List'}
                                </h3>
                                <p className="text-xs text-gray-300 mt-1">
                                    {confirmAction.kind === 'remove_one'
                                        ? `Remove "${confirmAction.targetName}" and delete its server record?`
                                        : 'Clear the current list and delete corresponding server records?'}
                                </p>
                            </div>
                            <div className="px-5 py-4 flex justify-end gap-2">
                                <button
                                    type="button"
                                    onClick={closeConfirmAction}
                                    disabled={isConfirmingAction}
                                    className="px-3 py-2 text-sm rounded-lg border border-white/20 text-gray-300 hover:text-white hover:border-white/40 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
                                >
                                    Cancel
                                </button>
                                <button
                                    type="button"
                                    onClick={() => void handleConfirmAction()}
                                    disabled={isConfirmingAction}
                                    className="px-4 py-2 text-sm rounded-lg border border-red-400/35 bg-red-500/20 text-red-100 hover:bg-red-500/30 transition-colors font-semibold disabled:opacity-60 disabled:cursor-not-allowed"
                                >
                                    {isConfirmingAction ? 'Working...' : 'Confirm Delete'}
                                </button>
                            </div>
                        </div>
                    </div>
                )}

                {/* Footer Actions */}
                <div className="mt-6 flex justify-end gap-3 pt-4 border-t border-white/10 shrink-0">
                    <button
                        onClick={onClose}
                        className="px-4 py-2 text-gray-400 hover:text-white transition-colors text-sm font-medium"
                    >
                        Close
                    </button>
                    <button
                        onClick={runBatch}
                        disabled={isGlobalLoading || queue.filter(i => i.status === 'idle').length === 0}
                        className="bg-primary text-background px-6 py-2 rounded-lg font-bold hover:bg-white transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2 min-w-[140px] justify-center"
                    >
                        {isGlobalLoading ? (
                            <>
                                <Loader2 className="w-4 h-4 animate-spin" />
                                Processing...
                            </>
                        ) : (
                            `Process ${queue.filter(i => i.status === 'idle').length} Files`
                        )}
                    </button>
                </div>
            </div>
        </div>
    );
}


