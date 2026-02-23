import { useCallback, useEffect, useMemo, useState } from 'react';
import { BookOpen, Download, ExternalLink, Loader2, Search, SlidersHorizontal, Subtitles, Upload, Video } from 'lucide-react';
import { API_HOST } from '../config/api';

type WordClipStatus = 'queued' | 'processing' | 'completed' | 'failed';

interface WordClipJob {
    id: string;
    status: WordClipStatus;
    word: string;
    source?: string;
    include_cambridge?: boolean;
    cambridge_clips?: number;
    domain: string;
    domains?: string[];
    video_count: number;
    clip_seconds: number;
    progress: number;
    message: string;
    error: string;
    clips_generated: number;
    videos_scanned: number;
    video_download_url?: string;
    audio_download_url?: string;
}

interface FocusWord {
    word: string;
    count: number;
    student_count?: number;
    last_seen_ts?: number;
}

interface FocusWordsResponse {
    words?: FocusWord[];
}

interface OnlineClipItem {
    video_id: string;
    title: string;
    source_url: string;
    embed_url: string;
    start_seconds: number;
    source_type: string;
}

interface OnlineClipResponse {
    word: string;
    source: string;
    count: number;
    warning?: string;
    items: OnlineClipItem[];
}

interface YouGlishNearbyWord {
    word: string;
    url: string;
}

interface YouGlishSnapshotResponse {
    word: string;
    available: boolean;
    source: string;
    source_url: string;
    count: number;
    example_sentence: string;
    nearby_words: YouGlishNearbyWord[];
    warning?: string;
}

type ApiPayload = { detail?: string; [key: string]: unknown };

const QUICK_FOCUS_WORDS = ['sausages', 'weather', 'lanterns', 'vacation', 'harbin'];

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

export default function WordClipStudio() {
    const [word, setWord] = useState('');
    const [videoCount, setVideoCount] = useState(4);
    const [includeCambridge, setIncludeCambridge] = useState(true);
    const [jobId, setJobId] = useState<string>('');
    const [job, setJob] = useState<WordClipJob | null>(null);
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState('');
    const [harFile, setHarFile] = useState<File | null>(null);
    const [poTokenKey, setPoTokenKey] = useState('web.gvs');
    const [extractingPoToken, setExtractingPoToken] = useState(false);
    const [poTokenStatus, setPoTokenStatus] = useState('');
    const [checkingPoToken, setCheckingPoToken] = useState(false);
    const [poTokenHealth, setPoTokenHealth] = useState('');
    const [focusWords, setFocusWords] = useState<FocusWord[]>([]);
    const [focusWordsLoading, setFocusWordsLoading] = useState(false);
    const [focusWordsError, setFocusWordsError] = useState('');
    const [focusMinCount, setFocusMinCount] = useState(2);
    const [focusStudentFilter, setFocusStudentFilter] = useState('');
    const [ygStatus, setYgStatus] = useState('Ready to search YouGlish clips in-page.');
    const [ygAccent, setYgAccent] = useState<'all' | 'us' | 'uk' | 'aus' | 'ca'>('all');
    const [allowFallbackPlayer, setAllowFallbackPlayer] = useState(true);
    const [ygBusy, setYgBusy] = useState(false);
    const [ygNeedsVerify, setYgNeedsVerify] = useState(false);
    const [onlineClips, setOnlineClips] = useState<OnlineClipItem[]>([]);
    const [onlineSource, setOnlineSource] = useState('');
    const [onlineIndex, setOnlineIndex] = useState(0);
    const [playerResetNonce, setPlayerResetNonce] = useState(0);
    const [ygSnapshotBusy, setYgSnapshotBusy] = useState(false);
    const [ygSnapshot, setYgSnapshot] = useState<YouGlishSnapshotResponse | null>(null);

    const jobSourceLabel = useMemo(() => {
        const source = String(job?.source || '').toLowerCase();
        if (source === 'youglish') return 'YouGlish';
        if (source === 'youglish_fallback') return 'YouGlish (fallback search)';
        if (source === 'hybrid') return 'YouGlish + YouTube';
        return 'YouGlish';
    }, [job?.source]);
    const onlineSourceLabel = useMemo(() => {
        const source = String(onlineSource || '').toLowerCase();
        if (source === 'youglish') return 'YouGlish';
        if (source === 'fallback_youtube') return 'YouTube fallback';
        if (source === 'fallback_youtube_api') return 'YouTube API fallback';
        return source || 'Online';
    }, [onlineSource]);

    const readApiJson = useCallback(async <T = ApiPayload>(response: Response): Promise<T> => {
        const raw = await response.text();
        if (!raw) return {} as T;
        try {
            return JSON.parse(raw) as T;
        } catch {
            const compact = raw.replace(/\s+/g, ' ').trim();
            throw new Error(`Server returned non-JSON response (${response.status}): ${compact.slice(0, 220)}`);
        }
    }, []);

    useEffect(() => {
        if (!jobId) return;
        if (job?.status === 'completed' || job?.status === 'failed') return;

        let stopped = false;

        const poll = async () => {
            try {
                const response = await fetch(`${API_HOST}/api/word-clips/jobs/${jobId}`);
                const data = await readApiJson<WordClipJob & { detail?: string }>(response);
                if (!response.ok) {
                    throw new Error(data?.detail || 'Failed to load job status');
                }
                if (!stopped) {
                    setJob(data);
                }
            } catch (err) {
                if (!stopped) {
                    setError(formatActionError('Job status check', err, 'Unknown error'));
                }
            }
        };

        poll();
        const timer = window.setInterval(poll, 2000);
        return () => {
            stopped = true;
            window.clearInterval(timer);
        };
    }, [job?.status, jobId, readApiJson]);

    const loadYouGlishSnapshot = useCallback(
        async (queryWord: string) => {
            const token = String(queryWord || '').trim();
            if (!token) {
                setYgSnapshot(null);
                return;
            }
            setYgSnapshotBusy(true);
            try {
                const params = new URLSearchParams({
                    word: token,
                    limit: '12',
                });
                const response = await fetch(`${API_HOST}/api/word-clips/youglish-snapshot?${params.toString()}`);
                const data = await readApiJson<YouGlishSnapshotResponse & { detail?: string }>(response);
                if (!response.ok) {
                    throw new Error(data?.detail || 'Failed to load YouGlish snapshot');
                }
                setYgSnapshot(data);
            } catch (err) {
                const msg = err instanceof Error ? err.message : 'Failed to load YouGlish snapshot';
                setYgSnapshot({
                    word: token,
                    available: false,
                    source: 'youglish_snapshot',
                    source_url: `https://youglish.com/pronounce/${encodeURIComponent(token)}/english`,
                    count: 0,
                    example_sentence: '',
                    nearby_words: [],
                    warning: msg,
                });
            } finally {
                setYgSnapshotBusy(false);
            }
        },
        [readApiJson],
    );

    const loadFocusWords = useCallback(async () => {
        setFocusWordsLoading(true);
        setFocusWordsError('');
        try {
            const params = new URLSearchParams({
                min_count: String(focusMinCount),
                limit: '12',
                recent_reports: '500',
            });
            const studentFilter = focusStudentFilter.trim();
            if (studentFilter) {
                params.set('student', studentFilter);
            }
            const response = await fetch(`${API_HOST}/api/pronunciation/focus-words?${params.toString()}`);
            const data = await readApiJson<FocusWordsResponse & { detail?: string }>(response);
            if (!response.ok) {
                throw new Error(data?.detail || 'Failed to load focus words');
            }
            const rows = Array.isArray(data?.words) ? data.words : [];
            setFocusWords(rows);
        } catch (err) {
            setFocusWords([]);
            setFocusWordsError(formatActionError('Focus words load', err, 'Unknown error'));
        } finally {
            setFocusWordsLoading(false);
        }
    }, [focusMinCount, focusStudentFilter, readApiJson]);

    useEffect(() => {
        void loadFocusWords();
    }, [loadFocusWords]);

    useEffect(() => {
        const timer = window.setInterval(() => {
            void loadFocusWords();
        }, 30000);
        return () => window.clearInterval(timer);
    }, [loadFocusWords]);

    const canSubmit = word.trim().length > 0 && !submitting && (job?.status !== 'queued' && job?.status !== 'processing');

    const submitJob = async () => {
        if (!canSubmit) return;
        setSubmitting(true);
        setError('');
        setJob(null);
        setJobId('');
        try {
            const response = await fetch(`${API_HOST}/api/word-clips/jobs`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    word: word.trim(),
                    video_count: videoCount,
                    clip_seconds: 5.0,
                    source: 'youglish',
                    include_cambridge: includeCambridge,
                }),
            });
            const data = await readApiJson(response);
            if (!response.ok) {
                throw new Error(data?.detail || 'Failed to create job');
            }
            setJobId(data.job_id as string);
            setJob({
                id: data.job_id as string,
                status: 'queued',
                word: word.trim(),
                source: 'youglish',
                include_cambridge: includeCambridge,
                cambridge_clips: includeCambridge ? 1 : 0,
                domain: 'youglish',
                domains: ['youglish'],
                video_count: videoCount,
                clip_seconds: 5,
                progress: 0,
                message: 'Queued',
                error: '',
                clips_generated: 0,
                videos_scanned: 0,
            });
        } catch (err) {
            setError(formatActionError('Create job', err, 'Unknown error'));
        } finally {
            setSubmitting(false);
        }
    };

    const extractPoTokenFromHar = async () => {
        if (!harFile || extractingPoToken) return;
        setExtractingPoToken(true);
        setPoTokenStatus('');
        try {
            const form = new FormData();
            form.append('har_file', harFile);
            form.append('token_key', poTokenKey);
            form.append('merge_existing', 'true');
            const response = await fetch(`${API_HOST}/api/word-clips/po-token/extract-har`, {
                method: 'POST',
                body: form,
            });
            const data = await readApiJson(response);
            if (!response.ok) {
                throw new Error(data?.detail || 'Failed to extract PO token');
            }
            const msg = `Saved ${data.token_key} (${data.selected_token_preview}) to ${data.saved_path}`;
            setPoTokenStatus(msg);
        } catch (err) {
            setPoTokenStatus(formatActionError('PO token extract', err, 'Unknown error'));
        } finally {
            setExtractingPoToken(false);
        }
    };

    const checkPoTokenHealth = async () => {
        if (checkingPoToken) return;
        setCheckingPoToken(true);
        setPoTokenHealth('');
        try {
            const response = await fetch(
                `${API_HOST}/api/word-clips/po-token/health-check?token_key=${encodeURIComponent(poTokenKey)}`,
            );
            const data = await readApiJson(response);
            if (!response.ok) {
                throw new Error(data?.detail || 'Failed to check token health');
            }
            if (data.status === 'ok') {
                setPoTokenHealth(`OK: playable formats ${data.playable_formats}/${data.total_formats}`);
            } else if (data.status === 'degraded') {
                setPoTokenHealth(`Degraded: ${data.hint || 'No playable format found'}`);
            } else {
                setPoTokenHealth(`Failed: ${data.hint || data.error || 'Health check failed'}`);
            }
        } catch (err) {
            setPoTokenHealth(formatActionError('PO token health check', err, 'Unknown error'));
        } finally {
            setCheckingPoToken(false);
        }
    };

    const fetchYouGlishWord = useCallback(async () => {
        const fallbackWord =
            focusWords
                .map((item) => String(item.word || '').trim().toLowerCase())
                .find(Boolean) || QUICK_FOCUS_WORDS[0];
        const q = (word.trim() || fallbackWord || '').trim();
        if (!q) return;
        void loadYouGlishSnapshot(q);
        try {
            setYgBusy(true);
            setYgNeedsVerify(false);
            setYgStatus(`Searching in-page clips for "${q}"...`);
            const params = new URLSearchParams({
                word: q,
                limit: String(Math.max(4, Math.min(20, videoCount))),
                accent: ygAccent,
            });
            const response = await fetch(`${API_HOST}/api/word-clips/online-sources?${params.toString()}`);
            const data = await readApiJson<OnlineClipResponse & { detail?: string }>(response);
            if (!response.ok) {
                throw new Error(data?.detail || 'Failed to load online clip sources');
            }
            const allItems = Array.isArray(data?.items) ? data.items : [];
            const youglishItems = allItems.filter((item) => {
                const sourceType = String(item?.source_type || data?.source || '').toLowerCase();
                return sourceType.includes('youglish');
            });
            const hasYouGlish = youglishItems.length > 0;
            const canUseFallback = !hasYouGlish && allowFallbackPlayer && allItems.length > 0;
            const items = hasYouGlish ? youglishItems : canUseFallback ? allItems : [];
            setOnlineClips(items);
            setOnlineIndex(0);
            setOnlineSource(
                hasYouGlish
                    ? 'youglish'
                    : canUseFallback
                        ? String(data?.source || 'fallback_youtube')
                        : String(data?.source || ''),
            );
            if (hasYouGlish) {
                setYgNeedsVerify(false);
                setYgStatus(`Loaded ${items.length} YouGlish clip(s) in player.`);
            } else if (canUseFallback) {
                setYgNeedsVerify(true);
                const fallbackSource = String(data?.source || 'fallback_youtube');
                const warningText = String(data?.warning || '').trim();
                const reason = warningText ? ` ${warningText}` : '';
                setYgStatus(
                    `Loaded ${items.length} fallback clip(s) from ${fallbackSource}.${reason}`,
                );
            } else {
                setYgNeedsVerify(true);
                const fallbackSource = String(data?.source || 'unknown');
                const fallbackCount = allItems.length;
                const warningText = String(data?.warning || '').trim();
                const reason = warningText ? ` ${warningText}` : '';
                setYgStatus(
                    `No YouGlish clips available. API returned ${fallbackSource} (${fallbackCount} item(s)); in YouGlish-only mode player stays empty.${reason}`,
                );
            }
        } catch (err) {
            const msg = formatActionError('YouGlish clip fetch', err, 'Unknown error');
            setOnlineClips([]);
            setYgNeedsVerify(true);
            setYgStatus(msg);
        } finally {
            setYgBusy(false);
        }
    }, [allowFallbackPlayer, focusWords, loadYouGlishSnapshot, readApiJson, videoCount, word, ygAccent]);

    const controlYouGlish = useCallback((action: 'prev' | 'next' | 'replay') => {
        if (!onlineClips.length) return;
        if (action === 'replay') {
            setPlayerResetNonce((prev) => prev + 1);
            return;
        }
        if (action === 'prev') {
            setOnlineIndex((prev) => (prev - 1 + onlineClips.length) % onlineClips.length);
            return;
        }
        if (action === 'next') {
            setOnlineIndex((prev) => (prev + 1) % onlineClips.length);
        }
    }, [onlineClips.length]);

    const buildYoutubeExternalUrl = useCallback((videoId: string, startSeconds?: number) => {
        const cleanId = String(videoId || '').trim().slice(0, 11);
        if (!cleanId) return '';
        const start = Math.max(0, Math.round(Number(startSeconds || 0)));
        return `https://www.youtube.com/watch?v=${encodeURIComponent(cleanId)}${start > 0 ? `&t=${start}s` : ''}`;
    }, []);

    const buildMirrorWatchUrl = useCallback((videoId: string, startSeconds?: number) => {
        const cleanId = String(videoId || '').trim().slice(0, 11);
        if (!cleanId) return '';
        const start = Math.max(0, Math.round(Number(startSeconds || 0)));
        return `https://piped.video/watch?v=${encodeURIComponent(cleanId)}${start > 0 ? `&t=${start}s` : ''}`;
    }, []);

    const buildYoutubeSearchUrl = useCallback((term: string) => {
        const q = String(term || '').trim();
        return `https://www.youtube.com/results?search_query=${encodeURIComponent(`${q} pronunciation in sentence`)}`;
    }, []);

    const openYouGlishTab = useCallback(() => {
        const fallbackWord =
            focusWords
                .map((item) => String(item.word || '').trim().toLowerCase())
                .find(Boolean) || QUICK_FOCUS_WORDS[0];
        const q = (word.trim() || fallbackWord || 'sausages').trim();
        const activeClip = onlineClips[onlineIndex] || onlineClips[0] || null;
        if (activeClip?.video_id) {
            const url = buildYoutubeExternalUrl(activeClip.video_id, activeClip.start_seconds);
            if (url) {
                window.open(url, '_blank', 'noopener,noreferrer');
                const stamp = Math.max(0, Math.round(Number(activeClip.start_seconds || 0)));
                setYgStatus(`Opened current keyword clip on YouTube watch page at ${stamp}s.`);
                return;
            }
        }
        const youtubeSearchUrl = buildYoutubeSearchUrl(q);
        window.open(youtubeSearchUrl, '_blank', 'noopener,noreferrer');
        setYgStatus(`No clip loaded yet. Opened YouTube verification results for "${q}".`);
    }, [buildYoutubeExternalUrl, buildYoutubeSearchUrl, focusWords, onlineClips, onlineIndex, word]);

    const inProgress = job?.status === 'queued' || job?.status === 'processing';
    const done = job?.status === 'completed';
    const autoFocusWords = useMemo(() => {
        const tokens = focusWords
            .map((item) => String(item.word || '').trim().toLowerCase())
            .filter(Boolean);
        return tokens.length > 0 ? tokens : QUICK_FOCUS_WORDS;
    }, [focusWords]);
    const coachWord = word.trim() || autoFocusWords[0] || 'sausages';
    const coachLinks = useMemo(() => {
        const token = encodeURIComponent(coachWord);
        return {
            cambridge: `https://dictionary.cambridge.org/pronunciation/english/${token}`,
            youglish: `https://www.youtube.com/results?search_query=${encodeURIComponent(`${coachWord} pronunciation in sentence`)}`,
            soundsOfSpeech: 'https://soundsofspeech.uiowa.edu/english',
            videoMouth: `https://www.youtube.com/results?search_query=${encodeURIComponent(`${coachWord} pronunciation mouth position`)}`,
            videoSentence: `https://www.youtube.com/results?search_query=${encodeURIComponent(`${coachWord} in sentence pronunciation`)}`,
            videoCoach: `https://www.youtube.com/results?search_query=${encodeURIComponent(`${coachWord} pronunciation lesson`)}`,
        };
    }, [coachWord]);
    const currentOnlineClip = onlineClips[onlineIndex] || null;
    const currentClipStart = Math.max(0, Math.round(Number(currentOnlineClip?.start_seconds || 0)));
    const verificationUrl = useMemo(() => {
        if (currentOnlineClip?.video_id) {
            return buildYoutubeExternalUrl(currentOnlineClip.video_id, currentOnlineClip.start_seconds);
        }
        const fallbackWord =
            focusWords
                .map((item) => String(item.word || '').trim().toLowerCase())
                .find(Boolean) || QUICK_FOCUS_WORDS[0];
        const q = (word.trim() || fallbackWord || 'sausages').trim();
        return buildYoutubeSearchUrl(q);
    }, [buildYoutubeExternalUrl, buildYoutubeSearchUrl, currentOnlineClip, focusWords, word]);
    const mirrorVerificationUrl = useMemo(() => {
        if (!currentOnlineClip?.video_id) return '';
        return buildMirrorWatchUrl(currentOnlineClip.video_id, currentOnlineClip.start_seconds);
    }, [buildMirrorWatchUrl, currentOnlineClip]);
    const currentEmbedUrl = useMemo(() => {
        if (!currentOnlineClip) return '';
        const raw = String(currentOnlineClip.embed_url || '').trim();
        if (!raw) return '';
        const normalizedRaw = raw.replace('http://www.youtube.com/embed/', 'https://www.youtube.com/embed/');
        const hasStart = /(?:\?|&)start=/.test(normalizedRaw);
        const joiner = normalizedRaw.includes('?') ? '&' : '?';
        const withStart = hasStart ? normalizedRaw : `${normalizedRaw}${joiner}start=${currentClipStart}`;
        return `${withStart}&autoplay=1`;
    }, [currentClipStart, currentOnlineClip]);

    return (
        <div className="relative min-h-screen overflow-x-hidden pt-28 pb-16 px-4 sm:px-6 lg:px-10">
            <div className="pointer-events-none absolute inset-0 -z-10">
                <div className="absolute -top-20 left-10 h-72 w-72 rounded-full bg-cyan-400/20 blur-3xl" />
                <div className="absolute top-1/3 right-12 h-72 w-72 rounded-full bg-blue-500/20 blur-3xl" />
                <div className="absolute bottom-0 left-1/3 h-72 w-72 rounded-full bg-emerald-400/20 blur-3xl" />
            </div>

            <div className="mx-auto max-w-6xl space-y-6">
                <div className="grid gap-6">
                    <section className="rounded-3xl border border-white/10 bg-slate-950/70 p-6 sm:p-8">
                        <div className="space-y-6">
                            <div className="sticky top-12 z-10 rounded-2xl border border-cyan-300/45 bg-gradient-to-br from-cyan-500/35 via-blue-500/26 to-emerald-400/20 p-4 sm:p-5 shadow-[0_0_0_1px_rgba(34,211,238,0.18),0_14px_30px_rgba(6,182,212,0.14)] backdrop-blur-md">
                                <div className="flex flex-wrap items-center justify-between gap-2">
                                    <p className="inline-flex items-center gap-2 rounded-full border border-cyan-200/40 bg-cyan-300/10 px-3 py-1 text-[11px] font-bold tracking-[0.08em] text-cyan-100 uppercase">
                                        <Search className="h-3.5 w-3.5 text-cyan-200" />
                                        Target Word
                                    </p>
                                    <span className="text-xs text-cyan-100/80">Input one word and run extraction</span>
                                </div>
                                <div className="mt-3 grid gap-3 sm:grid-cols-[1fr_auto]">
                                    <input
                                        value={word}
                                        onChange={(event) => setWord(event.target.value)}
                                        placeholder="e.g. fascinating"
                                        className="h-12 w-full rounded-2xl border border-cyan-200/35 bg-black/35 px-4 text-lg font-semibold tracking-wide text-white outline-none transition placeholder:text-cyan-100/45 focus:border-cyan-200/75 focus:ring-2 focus:ring-cyan-300/35"
                                    />
                                    <button
                                        type="button"
                                        disabled={ygBusy}
                                        onClick={fetchYouGlishWord}
                                        className="inline-flex h-12 items-center justify-center gap-2 rounded-2xl border border-cyan-200/45 bg-cyan-300/20 px-5 text-sm font-bold text-cyan-50 transition hover:bg-cyan-300/30 disabled:cursor-not-allowed disabled:opacity-60"
                                    >
                                        {ygBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Video className="h-4 w-4" />}
                                        {ygBusy ? 'Searching...' : 'Load in player'}
                                    </button>
                                </div>
                            </div>

                            <div className="rounded-2xl border border-cyan-300/20 bg-cyan-500/[0.08] p-3">
                                <p className="text-[11px] font-semibold tracking-[0.08em] text-cyan-100/80 uppercase">Quick picks</p>
                                <div className="mt-2 flex flex-wrap gap-2">
                                    {autoFocusWords.slice(0, 6).map((item) => (
                                        <button
                                            key={`spot-${item}`}
                                            type="button"
                                            onClick={() => setWord(item)}
                                            className={`rounded-full border px-3 py-1 text-xs font-semibold transition ${
                                                coachWord.toLowerCase() === item.toLowerCase()
                                                    ? 'border-cyan-200/70 bg-cyan-300/25 text-cyan-50'
                                                    : 'border-cyan-200/25 bg-black/20 text-cyan-100/90 hover:border-cyan-200/45'
                                            }`}
                                        >
                                            {item}
                                        </button>
                                    ))}
                                </div>
                            </div>

                            <div className="rounded-2xl border border-violet-400/30 bg-violet-500/10 p-4">
                                <div className="flex flex-wrap items-center justify-between gap-3">
                                    <div>
                                        <p className="text-xs uppercase tracking-wide text-violet-200">YouGlish Online Player</p>
                                        <p className="mt-1 text-base font-bold text-white">In-page player (no local download)</p>
                                    </div>
                                    <div className="flex items-center gap-2">
                                        <select
                                            value={ygAccent}
                                            onChange={(event) => setYgAccent(event.target.value as 'all' | 'us' | 'uk' | 'aus' | 'ca')}
                                            className="rounded-xl border border-white/15 bg-black/40 px-3 py-2 text-sm text-slate-100"
                                        >
                                            <option value="all">Accent: all</option>
                                            <option value="us">Accent: US</option>
                                            <option value="uk">Accent: UK</option>
                                            <option value="aus">Accent: AUS</option>
                                            <option value="ca">Accent: CA</option>
                                        </select>
                                        <button
                                            type="button"
                                            onClick={() => setAllowFallbackPlayer((prev) => !prev)}
                                            className={`rounded-xl border px-3 py-2 text-xs font-semibold transition ${
                                                allowFallbackPlayer
                                                    ? 'border-emerald-300/40 bg-emerald-400/15 text-emerald-100'
                                                    : 'border-white/20 bg-white/10 text-slate-200'
                                            }`}
                                        >
                                            Fallback {allowFallbackPlayer ? 'On' : 'Off'}
                                        </button>
                                        <button
                                            type="button"
                                            onClick={openYouGlishTab}
                                            className="inline-flex items-center gap-2 rounded-xl border border-white/20 bg-white/10 px-4 py-2 text-sm font-semibold text-white transition hover:border-white/35"
                                        >
                                            {onlineClips.length > 0 ? 'Open current clip' : 'Open YouTube results'}
                                            <ExternalLink className="h-3.5 w-3.5" />
                                        </button>
                                    </div>
                                </div>
                                <div className="mt-3 flex flex-wrap gap-2">
                                    <button
                                        type="button"
                                        disabled={!onlineClips.length}
                                        onClick={() => controlYouGlish('prev')}
                                        className="rounded-lg border border-white/15 bg-black/30 px-3 py-1.5 text-xs font-semibold text-slate-200 transition hover:border-white/35 disabled:cursor-not-allowed disabled:opacity-60"
                                    >
                                        Previous
                                    </button>
                                    <button
                                        type="button"
                                        disabled={!onlineClips.length}
                                        onClick={() => controlYouGlish('replay')}
                                        className="rounded-lg border border-white/15 bg-black/30 px-3 py-1.5 text-xs font-semibold text-slate-200 transition hover:border-white/35 disabled:cursor-not-allowed disabled:opacity-60"
                                    >
                                        Replay
                                    </button>
                                    <button
                                        type="button"
                                        disabled={!onlineClips.length}
                                        onClick={() => controlYouGlish('next')}
                                        className="rounded-lg border border-white/15 bg-black/30 px-3 py-1.5 text-xs font-semibold text-slate-200 transition hover:border-white/35 disabled:cursor-not-allowed disabled:opacity-60"
                                    >
                                        Next
                                    </button>
                                    {onlineClips.length > 0 && (
                                        <span className="inline-flex items-center rounded-lg border border-violet-300/30 bg-violet-500/10 px-2.5 py-1 text-xs font-semibold text-violet-200">
                                            {onlineIndex + 1}/{onlineClips.length} • {onlineSourceLabel}
                                        </span>
                                    )}
                                </div>
                                <div className="mt-3 rounded-xl border border-white/10 bg-black/25 p-2">
                                    {currentOnlineClip ? (
                                        <div className="mx-auto flex w-full justify-center">
                                            <iframe
                                                key={`${currentOnlineClip.video_id}:${onlineIndex}:${currentClipStart}:${playerResetNonce}`}
                                                title={currentOnlineClip.title}
                                                src={currentEmbedUrl}
                                                className="aspect-video w-[92%] max-w-[920px] rounded-lg"
                                                allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
                                                allowFullScreen
                                            />
                                        </div>
                                    ) : (
                                        <div className="mx-auto flex min-h-[220px] w-[92%] max-w-[920px] items-center justify-center rounded-lg bg-black/20 px-4 text-center text-sm text-slate-400">
                                            Click "Load in player" to fetch in-page clips (YouGlish-first, fallback if blocked).
                                        </div>
                                    )}
                                </div>
                                <p className={`mt-2 text-xs ${ygNeedsVerify ? 'text-amber-200' : 'text-violet-100/85'}`}>{ygStatus}</p>
                                {currentOnlineClip && (
                                    <p className="text-xs text-violet-200/90">
                                        {currentOnlineClip.title} | keyword at {currentClipStart}s
                                    </p>
                                )}
                                <div className="mt-2 flex flex-wrap gap-2">
                                    <a
                                        href={verificationUrl}
                                        target="_blank"
                                        rel="noreferrer"
                                        className="inline-flex items-center gap-2 rounded-lg border border-violet-300/40 bg-violet-400/20 px-3 py-1.5 text-xs font-semibold text-violet-100 hover:bg-violet-400/30"
                                    >
                                        Open YouTube for verification
                                        <ExternalLink className="h-3.5 w-3.5" />
                                    </a>
                                    {mirrorVerificationUrl && (
                                        <a
                                            href={mirrorVerificationUrl}
                                            target="_blank"
                                            rel="noreferrer"
                                            className="inline-flex items-center gap-2 rounded-lg border border-cyan-300/40 bg-cyan-400/20 px-3 py-1.5 text-xs font-semibold text-cyan-100 hover:bg-cyan-400/30"
                                        >
                                            Open no-login mirror
                                            <ExternalLink className="h-3.5 w-3.5" />
                                        </a>
                                    )}
                                    <button
                                        type="button"
                                        onClick={openYouGlishTab}
                                        className="rounded-lg border border-violet-300/40 bg-violet-400/20 px-3 py-1.5 text-xs font-semibold text-violet-100 hover:bg-violet-400/30"
                                    >
                                        {onlineClips.length > 0 ? 'Open current clip' : 'Open YouTube results'}
                                    </button>
                                </div>
                                <div className="mt-3 rounded-xl border border-emerald-300/35 bg-emerald-500/10 p-3">
                                    <div className="flex flex-wrap items-center justify-between gap-2">
                                        <p className="text-xs uppercase tracking-wide text-emerald-200">Direct YouGlish Results</p>
                                        <a
                                            href={ygSnapshot?.source_url || `https://youglish.com/pronounce/${encodeURIComponent(word.trim() || 'sausages')}/english`}
                                            target="_blank"
                                            rel="noreferrer"
                                            className="inline-flex items-center gap-1 rounded-md border border-emerald-300/40 bg-emerald-400/15 px-2 py-1 text-[11px] font-semibold text-emerald-100 hover:bg-emerald-400/25"
                                        >
                                            Open on YouGlish
                                            <ExternalLink className="h-3 w-3" />
                                        </a>
                                    </div>
                                    {ygSnapshotBusy ? (
                                        <p className="mt-2 text-xs text-emerald-100/85">Loading YouGlish snapshot...</p>
                                    ) : ygSnapshot ? (
                                        <>
                                            <p className="mt-2 text-xs text-emerald-100/90">
                                                {ygSnapshot.available
                                                    ? `${ygSnapshot.count} pronunciation result(s) found on YouGlish.`
                                                    : 'YouGlish snapshot currently unavailable.'}
                                            </p>
                                            {ygSnapshot.example_sentence && (
                                                <p className="mt-1 text-xs text-emerald-100/85">Example: {ygSnapshot.example_sentence}</p>
                                            )}
                                            {ygSnapshot.nearby_words.length > 0 && (
                                                <div className="mt-2 flex flex-wrap gap-1.5">
                                                    {ygSnapshot.nearby_words.slice(0, 10).map((item) => (
                                                        <a
                                                            key={`${item.word}-${item.url}`}
                                                            href={item.url}
                                                            target="_blank"
                                                            rel="noreferrer"
                                                            className="rounded-full border border-emerald-300/35 bg-emerald-400/10 px-2 py-0.5 text-[11px] font-medium text-emerald-100 hover:bg-emerald-400/20"
                                                        >
                                                            {item.word}
                                                        </a>
                                                    ))}
                                                </div>
                                            )}
                                            {ygSnapshot.warning && <p className="mt-2 text-xs text-amber-200">{ygSnapshot.warning}</p>}
                                        </>
                                    ) : (
                                        <p className="mt-2 text-xs text-emerald-100/80">Click "Load in player" to load YouGlish snapshot data.</p>
                                    )}
                                </div>
                            </div>

                            <div className="rounded-2xl border border-cyan-400/30 bg-cyan-500/10 p-4">
                                <p className="text-xs uppercase tracking-wide text-cyan-200">Source</p>
                                <p className="mt-1 text-base font-bold text-white">YouGlish keyword clips</p>
                                <p className="mt-1 text-xs text-cyan-100/80">
                                    Pipeline is now focused on YouGlish. Slider controls the number of YouGlish clips.
                                </p>
                            </div>

                            <div>
                                <div className="mb-2 flex items-center justify-between text-sm font-semibold text-slate-200">
                                    <span className="inline-flex items-center gap-2">
                                        <SlidersHorizontal className="h-4 w-4 text-cyan-300" />
                                        YouGlish clip count
                                    </span>
                                    <span className="text-cyan-300">{videoCount}</span>
                                </div>
                                <input
                                    type="range"
                                    min={2}
                                    max={20}
                                    step={1}
                                    value={videoCount}
                                    onChange={(event) => setVideoCount(Number(event.target.value))}
                                    className="w-full accent-cyan-400"
                                />
                                <p className="mt-2 text-xs text-slate-400">
                                    Higher values increase coverage but can take longer.
                                </p>
                            </div>

                            <label className="flex items-center justify-between gap-3 rounded-2xl border border-white/10 bg-black/25 px-4 py-3">
                                <div>
                                    <p className="text-sm font-semibold text-slate-100">Include Cambridge demo</p>
                                    <p className="text-xs text-slate-400">Add 1 pronunciation demo clip from Cambridge dictionary when available.</p>
                                </div>
                                <button
                                    type="button"
                                    onClick={() => setIncludeCambridge((prev) => !prev)}
                                    className={`relative inline-flex h-7 w-12 items-center rounded-full transition ${includeCambridge ? 'bg-emerald-500' : 'bg-slate-600'}`}
                                    aria-pressed={includeCambridge}
                                >
                                    <span className={`inline-block h-5 w-5 transform rounded-full bg-white transition ${includeCambridge ? 'translate-x-6' : 'translate-x-1'}`} />
                                </button>
                            </label>

                            <div className="rounded-2xl border border-white/10 bg-black/25 p-4 space-y-3">
                                <p className="text-sm font-semibold text-slate-200">PO Token helper (HAR)</p>
                                <div className="grid gap-3 sm:grid-cols-[1fr_auto]">
                                    <input
                                        type="file"
                                        accept=".har,.json"
                                        onChange={(event) => setHarFile(event.target.files?.[0] || null)}
                                        className="rounded-xl border border-white/15 bg-black/30 px-3 py-2 text-sm text-slate-200"
                                    />
                                    <select
                                        value={poTokenKey}
                                        onChange={(event) => setPoTokenKey(event.target.value)}
                                        className="rounded-xl border border-white/15 bg-black/30 px-3 py-2 text-sm text-slate-200"
                                    >
                                        <option value="web.gvs">web.gvs</option>
                                        <option value="android.gvs">android.gvs</option>
                                        <option value="mweb.gvs">mweb.gvs</option>
                                    </select>
                                </div>
                                <button
                                    type="button"
                                    disabled={!harFile || extractingPoToken}
                                    onClick={extractPoTokenFromHar}
                                    className="inline-flex items-center gap-2 rounded-xl border border-cyan-400/40 bg-cyan-500/10 px-4 py-2 text-sm font-semibold text-cyan-200 transition hover:bg-cyan-500/20 disabled:cursor-not-allowed disabled:opacity-50"
                                >
                                    {extractingPoToken ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
                                    {extractingPoToken ? 'Extracting...' : 'Extract PO Token from HAR'}
                                </button>
                                <button
                                    type="button"
                                    disabled={checkingPoToken}
                                    onClick={checkPoTokenHealth}
                                    className="inline-flex items-center gap-2 rounded-xl border border-emerald-400/40 bg-emerald-500/10 px-4 py-2 text-sm font-semibold text-emerald-200 transition hover:bg-emerald-500/20 disabled:cursor-not-allowed disabled:opacity-50"
                                >
                                    {checkingPoToken ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
                                    {checkingPoToken ? 'Checking...' : 'Check token health'}
                                </button>
                                {poTokenStatus && (
                                    <p className={`text-xs ${poTokenStatus.toLowerCase().includes('failed') || poTokenStatus.toLowerCase().includes('no ') ? 'text-red-300' : 'text-emerald-300'}`}>
                                        {poTokenStatus}
                                    </p>
                                )}
                                {poTokenHealth && (
                                    <p className={`text-xs ${poTokenHealth.toLowerCase().startsWith('ok:') ? 'text-emerald-300' : poTokenHealth.toLowerCase().startsWith('degraded:') ? 'text-amber-300' : 'text-red-300'}`}>
                                        {poTokenHealth}
                                    </p>
                                )}
                            </div>

                            <button
                                type="button"
                                disabled={!canSubmit}
                                onClick={submitJob}
                                className="inline-flex items-center gap-2 rounded-2xl bg-cyan-400 px-5 py-3 text-sm font-bold text-slate-950 transition hover:bg-cyan-300 disabled:cursor-not-allowed disabled:opacity-50"
                            >
                                {submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Video className="h-4 w-4" />}
                                {submitting ? 'Creating job...' : 'Generate clips'}
                            </button>

                            {error && (
                                <div className="rounded-2xl border border-red-400/40 bg-red-500/10 px-4 py-3 text-sm text-red-200">
                                    {error}
                                </div>
                            )}
                        </div>
                    </section>

                    <aside className="rounded-3xl border border-white/10 bg-black/35 p-6 sm:p-8 space-y-4">
                        <h2 className="text-xl font-bold text-white">Job status</h2>
                        {!job && (
                            <p className="text-sm text-slate-400">
                                Submit a word to start extraction. Progress and download buttons appear here.
                            </p>
                        )}

                        {job && (
                            <>
                                <div className="rounded-2xl border border-white/10 bg-black/30 p-4 space-y-3">
                                    <div className="flex items-center justify-between text-sm">
                                        <span className="text-slate-300">Word</span>
                                        <span className="font-semibold text-cyan-200">{job.word}</span>
                                    </div>
                                    <div className="flex items-center justify-between text-sm">
                                        <span className="text-slate-300">Source</span>
                                        <span className="font-semibold text-cyan-200">{jobSourceLabel}</span>
                                    </div>
                                    <div className="flex items-center justify-between text-sm">
                                        <span className="text-slate-300">Cambridge demo</span>
                                        <span className="font-semibold text-cyan-200">
                                            {job.include_cambridge ? `On${job.cambridge_clips ? ` (+${job.cambridge_clips})` : ''}` : 'Off'}
                                        </span>
                                    </div>
                                    <div className="flex items-center justify-between text-sm">
                                        <span className="text-slate-300">Status</span>
                                        <span className="font-semibold text-white capitalize">{job.status}</span>
                                    </div>
                                    <div className="h-2 rounded-full bg-white/10 overflow-hidden">
                                        <div
                                            className="h-full bg-gradient-to-r from-cyan-400 to-blue-500 transition-all duration-500"
                                            style={{ width: `${Math.max(0, Math.min(100, job.progress || 0))}%` }}
                                        />
                                    </div>
                                    <p className="text-xs text-slate-400">{job.message || 'Waiting...'}</p>
                                </div>

                                <div className="grid grid-cols-2 gap-3 text-center">
                                    <div className="rounded-xl border border-white/10 bg-white/[0.03] p-3">
                                        <p className="text-[11px] uppercase tracking-wide text-slate-400">Scanned</p>
                                        <p className="text-lg font-bold text-white">{job.videos_scanned ?? 0}</p>
                                    </div>
                                    <div className="rounded-xl border border-white/10 bg-white/[0.03] p-3">
                                        <p className="text-[11px] uppercase tracking-wide text-slate-400">Clips</p>
                                        <p className="text-lg font-bold text-white">{job.clips_generated ?? 0}</p>
                                    </div>
                                </div>

                                {inProgress && (
                                    <div className="inline-flex items-center gap-2 rounded-xl border border-cyan-400/30 bg-cyan-500/10 px-3 py-2 text-sm text-cyan-200">
                                        <Loader2 className="h-4 w-4 animate-spin" />
                                        Processing in background...
                                    </div>
                                )}

                                {done && (
                                    <div className="space-y-3">
                                        <a
                                            href={`${API_HOST}${job.video_download_url || ''}`}
                                            className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-emerald-400 px-4 py-2.5 text-sm font-bold text-emerald-950 transition hover:bg-emerald-300"
                                        >
                                            <Download className="h-4 w-4" />
                                            Download merged video
                                        </a>
                                        <a
                                            href={`${API_HOST}${job.audio_download_url || ''}`}
                                            className="inline-flex w-full items-center justify-center gap-2 rounded-xl border border-white/20 bg-white/5 px-4 py-2.5 text-sm font-semibold text-white transition hover:border-cyan-400/40"
                                        >
                                            <Subtitles className="h-4 w-4" />
                                            Download merged audio
                                        </a>
                                    </div>
                                )}

                                {job.status === 'failed' && (
                                    <div className="rounded-2xl border border-red-400/40 bg-red-500/10 px-4 py-3 text-sm text-red-200">
                                        {job.error || 'Processing failed'}
                                    </div>
                                )}
                            </>
                        )}
                    </aside>
                </div>

                <section className="rounded-3xl border border-white/10 bg-slate-950/70 p-6 sm:p-8">
                    <div className="flex flex-wrap items-start justify-between gap-4">
                        <div>
                            <p className="inline-flex items-center gap-2 rounded-full border border-amber-400/40 bg-amber-400/10 px-3 py-1 text-xs font-semibold tracking-wider text-amber-200 uppercase">
                                <BookOpen className="h-3.5 w-3.5" />
                                Pronunciation Coach
                            </p>
                            <h2 className="mt-3 text-2xl font-black tracking-tight text-white">
                                AI focus word: {coachWord}
                            </h2>
                            <p className="mt-2 text-sm text-slate-300">
                                Focus words are auto-generated from cumulative pronunciation errors in recent uploads.
                            </p>
                        </div>

                        <div className="w-full space-y-3 lg:w-[520px]">
                            <div className="grid gap-2 sm:grid-cols-[1fr_auto_auto]">
                                <input
                                    value={focusStudentFilter}
                                    onChange={(event) => setFocusStudentFilter(event.target.value)}
                                    onKeyDown={(event) => {
                                        if (event.key === 'Enter') {
                                            loadFocusWords();
                                        }
                                    }}
                                    placeholder="Student name filter (optional)"
                                    className="rounded-xl border border-white/15 bg-black/30 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500"
                                />
                                <select
                                    value={focusMinCount}
                                    onChange={(event) => setFocusMinCount(Number(event.target.value) || 2)}
                                    className="rounded-xl border border-white/15 bg-black/30 px-3 py-2 text-sm text-slate-100"
                                >
                                    <option value={2}>Threshold {'>='} 2</option>
                                    <option value={3}>Threshold {'>='} 3</option>
                                    <option value={4}>Threshold {'>='} 4</option>
                                    <option value={5}>Threshold {'>='} 5</option>
                                </select>
                                <button
                                    type="button"
                                    onClick={loadFocusWords}
                                    disabled={focusWordsLoading}
                                    className="inline-flex items-center justify-center gap-2 rounded-xl border border-amber-300/40 bg-amber-400/10 px-4 py-2 text-sm font-semibold text-amber-100 transition hover:bg-amber-400/20 disabled:cursor-not-allowed disabled:opacity-60"
                                >
                                    {focusWordsLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
                                    Refresh
                                </button>
                            </div>

                            <div className="flex flex-wrap gap-2">
                                {autoFocusWords.map((item) => {
                                    const row = focusWords.find((entry) => entry.word.toLowerCase() === item.toLowerCase());
                                    return (
                                        <button
                                            key={item}
                                            type="button"
                                            onClick={() => setWord(item)}
                                            className={`rounded-full border px-3 py-1.5 text-xs font-semibold transition ${coachWord.toLowerCase() === item.toLowerCase()
                                                ? 'border-amber-300/70 bg-amber-300/20 text-amber-100'
                                                : 'border-white/15 bg-white/5 text-slate-300 hover:border-white/35'
                                                }`}
                                        >
                                            {item}{row ? ` (${row.count})` : ''}
                                        </button>
                                    );
                                })}
                            </div>

                            {focusWordsError && <p className="text-xs text-rose-300">{focusWordsError}</p>}
                            {!focusWordsError && <p className="text-xs text-slate-400">Auto-refresh: every 30 seconds</p>}
                            {!focusWordsLoading && focusWords.length === 0 && (
                                <p className="text-xs text-slate-400">No aggregated focus words yet. Upload more reading reports first.</p>
                            )}
                        </div>
                    </div>

                    <div className="mt-5 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                        <a
                            href={coachLinks.soundsOfSpeech}
                            target="_blank"
                            rel="noreferrer"
                            className="group rounded-2xl border border-cyan-400/30 bg-cyan-500/10 p-4 transition hover:bg-cyan-500/20"
                        >
                            <p className="text-xs font-semibold tracking-wide text-cyan-200 uppercase">Mouth Animation</p>
                            <p className="mt-2 text-lg font-bold text-white">Sounds of Speech (Direct)</p>
                            <p className="mt-1 text-sm text-cyan-100/80">Open UIowa mouth-position demos directly for phoneme articulation practice.</p>
                            <p className="mt-1 text-xs text-cyan-100/70">Need word-level examples for "{coachWord}"? Use Video Mouth Search below.</p>
                            <span className="mt-3 inline-flex items-center gap-1 text-sm font-semibold text-cyan-200">
                                Open
                                <ExternalLink className="h-3.5 w-3.5" />
                            </span>
                        </a>

                        <a
                            href={coachLinks.cambridge}
                            target="_blank"
                            rel="noreferrer"
                            className="group rounded-2xl border border-emerald-400/30 bg-emerald-500/10 p-4 transition hover:bg-emerald-500/20"
                        >
                            <p className="text-xs font-semibold tracking-wide text-emerald-200 uppercase">Dictionary Audio</p>
                            <p className="mt-2 text-lg font-bold text-white">Cambridge Pronunciation</p>
                            <p className="mt-1 text-sm text-emerald-100/80">Compare UK/US audio and IPA to verify your target sound.</p>
                            <span className="mt-3 inline-flex items-center gap-1 text-sm font-semibold text-emerald-200">
                                Open
                                <ExternalLink className="h-3.5 w-3.5" />
                            </span>
                        </a>

                        <a
                            href={coachLinks.youglish}
                            target="_blank"
                            rel="noreferrer"
                            className="group rounded-2xl border border-violet-400/30 bg-violet-500/10 p-4 transition hover:bg-violet-500/20"
                        >
                            <p className="text-xs font-semibold tracking-wide text-violet-200 uppercase">Real Context</p>
                            <p className="mt-2 text-lg font-bold text-white">YouTube Usage Clips</p>
                            <p className="mt-1 text-sm text-violet-100/80">Open sentence-level pronunciation examples directly (no YouGlish login wall).</p>
                            <span className="mt-3 inline-flex items-center gap-1 text-sm font-semibold text-violet-200">
                                Open
                                <ExternalLink className="h-3.5 w-3.5" />
                            </span>
                        </a>

                        <a
                            href={coachLinks.videoMouth}
                            target="_blank"
                            rel="noreferrer"
                            className="rounded-2xl border border-white/15 bg-white/[0.04] p-4 transition hover:border-white/30"
                        >
                            <p className="text-xs font-semibold tracking-wide text-slate-300 uppercase">Video 1</p>
                            <p className="mt-2 text-base font-bold text-white">Mouth position demo</p>
                            <p className="mt-1 text-sm text-slate-300">Search: {coachWord} pronunciation mouth position</p>
                        </a>

                        <a
                            href={coachLinks.videoSentence}
                            target="_blank"
                            rel="noreferrer"
                            className="rounded-2xl border border-white/15 bg-white/[0.04] p-4 transition hover:border-white/30"
                        >
                            <p className="text-xs font-semibold tracking-wide text-slate-300 uppercase">Video 2</p>
                            <p className="mt-2 text-base font-bold text-white">Sentence pronunciation demo</p>
                            <p className="mt-1 text-sm text-slate-300">Search: {coachWord} in sentence pronunciation</p>
                        </a>

                        <a
                            href={coachLinks.videoCoach}
                            target="_blank"
                            rel="noreferrer"
                            className="rounded-2xl border border-white/15 bg-white/[0.04] p-4 transition hover:border-white/30"
                        >
                            <p className="text-xs font-semibold tracking-wide text-slate-300 uppercase">Video 3</p>
                            <p className="mt-2 text-base font-bold text-white">Teacher explanation video</p>
                            <p className="mt-1 text-sm text-slate-300">Search: {coachWord} pronunciation lesson</p>
                        </a>
                    </div>
                </section>
            </div>
        </div>
    );
}
