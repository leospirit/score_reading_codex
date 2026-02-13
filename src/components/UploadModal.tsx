import { useState, useRef, useEffect } from 'react';
import { X, Upload, FileAudio, AlertCircle, Loader2, Check, Trash2, History as HistoryIcon, Zap, Code } from 'lucide-react';

interface UploadModalProps {
    isOpen: boolean;
    onClose: () => void;
}

interface QueueItem {
    file: File | { name: string };
    status: 'idle' | 'uploading' | 'queued' | 'processing' | 'done' | 'error';
    jobId?: string;
    resultUrl?: string;
    error?: string;
}

export default function UploadModal({ isOpen, onClose }: UploadModalProps) {
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
    const [stats, setStats] = useState({ completed: 0, total: 0 });
    const fileInputRef = useRef<HTMLInputElement>(null);
    const preheatTextareaRef = useRef<HTMLTextAreaElement>(null);

    const [existingNames, setExistingNames] = useState<Set<string>>(new Set());
    const API_HOST = "";

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

    // Initial load from server
    useEffect(() => {
        if (isOpen) {
            fetchRecentReports();
        }
    }, [isOpen]);

    const fetchRecentReports = async () => {
        try {
            const response = await fetch(`${API_HOST}/api/reports`);
            if (response.ok) {
                const data = await response.json();

                // 1. Populate Set of existing names for duplicate checking
                // Assuming report name format usually matches uploaded filename stem
                const names = new Set<string>();
                const toStem = (value: string) => {
                    const dot = value.lastIndexOf('.');
                    return dot > 0 ? value.substring(0, dot) : value;
                };
                data.forEach((r: any) => {
                    if (r.student_name) names.add(String(r.student_name));
                    if (r.display_name) names.add(String(r.display_name));
                    if (r.original_filename) names.add(toStem(String(r.original_filename)));
                });
                setExistingNames(names);

                // 2. Queue population (just 5 for display)
                if (queue.length === 0) {
                    const recent = data.slice(0, 5).map((r: any) => ({
                        file: { name: r.student_name },
                        status: 'done',
                        resultUrl: `${API_HOST}${r.url}`,
                        jobId: r.id // Important: Store ID for deletion
                    }));
                    if (recent.length > 0) {
                        setQueue(recent);
                    }
                }
            }
        } catch (e) {
            console.error("Failed to fetch recent reports", e);
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
        setQueue(prev => {
            const updatedQueue = [...prev];
            const newItems: QueueItem[] = [];

            for (const f of newFiles) {
                const uniqueName = getUniqueFileName(f.name, [...updatedQueue, ...newItems]);
                const renamedFile = new File([f], uniqueName, { type: f.type });
                newItems.push({
                    file: renamedFile,
                    status: 'idle'
                });
            }
            return [...prev, ...newItems];
        });
        setShouldAutoRun(true);
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
                const ok = await prebuildAnnotatedReference(normalizedText, { silent: true });
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
            const response = await fetch(`${API_HOST}/api/upload`, {
                method: 'POST',
                body: formData,
            });

            if (!response.ok) {
                const errData = await parseResponseSafely(response);
                throw new Error(errData.detail || 'Upload Failed');
            }

            const data = await parseResponseSafely(response);
            const jobId = data.job_id;

            // Update to Queued state
            setQueue(prev => {
                const copy = [...prev];
                if (copy[index]) copy[index] = { ...copy[index], status: 'queued', jobId: jobId };
                return copy;
            });

            // 2. Polling Phase (Detached)
            let transientFailureCount = 0;
            const MAX_TRANSIENT_FAILURES = 30; // ~60s with 2s interval
            const isTransientMessage = (message: string) =>
                /failed to fetch|networkerror|bad gateway|temporarily unavailable|unexpected token|transient:/i.test(message);

            const poll = async () => {
                try {
                    const statusRes = await fetch(`${API_HOST}/api/jobs/${jobId}`);
                    const statusData = await parseResponseSafely(statusRes);
                    if (!statusRes.ok) {
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
                        setQueue(prev => {
                            const copy = [...prev];
                            if (copy[index]) {
                                copy[index] = {
                                    ...copy[index],
                                    status: 'done',
                                    resultUrl: `${API_HOST}${jobData.result_url}`
                                };
                            }
                            return copy;
                        });
                        return; // Stop polling
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
                                };
                            }
                            return copy;
                        });

                        // Keep polling with delay (Dynamic backoff could be added here)
                        setTimeout(poll, 2000);
                    }
                } catch (err: any) {
                    const message = String(err?.message || err || 'Polling failed');
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

        } catch (err: any) {
            setQueue(prev => {
                const copy = [...prev];
                if (copy[index]) {
                    copy[index] = {
                        ...copy[index],
                        status: 'error',
                        error: err.message
                    };
                }
                return copy;
            });
            throw err; // Re-throw upload errors to stop batch if needed
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

    // Public trigger
    const runBatch = () => {
        runBatchLogic();
    };

    // Auto-Run Effect
    useEffect(() => {
        if (shouldAutoRun && !isGlobalLoading) {
            // Debounce
            const timer = setTimeout(() => {
                runBatchLogic();
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

    const removeFile = async (index: number) => {
        const item = queue[index];
        if (!item) return;

        if (window.confirm(`Remove "${item.file.name.replace(/\.[^/.]+$/, "")}"?\n(This will permanently delete the server record.)`)) {
            // Optimistic UI update
            setQueue(prev => prev.filter((_, i) => i !== index));

            // If it's a server record (has jobId or mapped ID), delete from server
            const targetId = item.jobId;
            if (targetId) {
                try {
                    await fetch(`${API_HOST}/api/reports/${targetId}`, { method: 'DELETE' });
                } catch (e) {
                    console.error("Failed to delete from server", e);
                    // Optionally revert UI or toast error
                }
            }
        }
    };

    const openReport = (url?: string) => {
        if (url) window.open(url, '_blank');
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

    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-[100] flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm animate-fade-in">
            <div className="bg-[#0A0A0B] border border-white/10 rounded-2xl w-full max-w-2xl shadow-2xl p-6 relative overflow-hidden flex flex-col max-h-[90vh]">
                <div className="absolute top-0 right-0 w-64 h-64 bg-primary/10 rounded-full blur-[100px] -z-10"></div>

                {/* Header */}
                <div className="flex justify-between items-center mb-6 shrink-0">
                    <div className="flex items-center gap-4">
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
                            <button
                                onClick={async () => {
                                    if (window.confirm('Clear the current list permanently?\\n(Server-side history records in this batch will also be deleted.)')) {
                                        // Collect IDs to delete
                                        const idsToDelete = queue
                                            .map(item => item.jobId)
                                            .filter(id => id !== undefined) as string[];

                                        if (idsToDelete.length > 0) {
                                            try {
                                                await fetch(`${API_HOST}/api/reports/batch-delete`, {
                                                    method: 'POST',
                                                    headers: { 'Content-Type': 'application/json' },
                                                    body: JSON.stringify({ ids: idsToDelete })
                                                });
                                            } catch (e) {
                                                console.error("Batch delete failed", e);
                                            }
                                        }
                                        setQueue([]);
                                    }
                                }}
                                className="text-xs text-gray-500 hover:text-red-400 transition-colors font-bold uppercase tracking-wider underline underline-offset-4"
                            >
                                Clear All (Delete)
                            </button>
                        )}
                    </div>
                    <div className="flex items-center gap-4">
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

                <div className="flex-1 overflow-y-auto min-h-0 space-y-6 pr-2">
                    {/* Step 1: Engine Mode Selection (Moved to Top) */}
                    <div className="shrink-0 space-y-2">
                        <div className="flex items-center gap-2">
                            <span className="bg-primary text-background text-xs font-bold px-2 py-0.5 rounded">STEP 1</span>
                            <label className="text-sm font-bold text-gray-300">Select Engine Mode</label>
                        </div>
                        <div className="flex gap-4">
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
                                    {engineMode === 'pro' ? "WAV, MP3 supported" : "Auto-dictation enabled"}
                                </p>
                            </div>
                        </div>
                    </div>

                    {/* File List */}
                    {queue.length > 0 && (
                        <div className="space-y-3">
                            <label className="block text-xs font-bold text-gray-400 uppercase tracking-wider px-1">Batch Results ({queue.length})</label>
                            <div className="space-y-2">
                                {queue.map((item, idx) => {
                                    const isDone = item.status === 'done';
                                    const isProcessing = item.status === 'processing';
                                    const isQueued = item.status === 'queued';
                                    const isError = item.status === 'error';

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
                                                        {isProcessing && <span className="text-primary animate-pulse flex items-center gap-2 font-bold"><span className="w-1.5 h-1.5 bg-primary rounded-full animate-ping"></span>Grading in progress...</span>}
                                                        {isQueued && <span className="text-green-400 font-medium">Upload Success! Queued...</span>}
                                                        {isDone && <span className="text-gray-500 font-medium">Click to view report</span>}
                                                        {isError && <span className="text-red-400 font-bold">{item.error}</span>}
                                                        {item.status === 'idle' && <span className="text-gray-600">Ready to upload</span>}
                                                    </div>
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
                                                            removeFile(idx);
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


