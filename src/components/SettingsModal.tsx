import React, { useEffect, useRef, useState } from 'react';
import { useViewportProfile } from '../hooks/useViewportProfile';
import { API_HOST } from '../config/api';

interface SettingsModalProps {
    isOpen: boolean;
    onClose: () => void;
}

interface GeminiConfig {
    api_key: string;
    model: string;
    alignment_source: 'whisper' | 'gop';
    has_key: boolean;
}

interface AzureConfig {
    api_key: string;
    region: string;
    has_key: boolean;
}

interface ConfigState {
    provider: string;
    base_url: string;
    model: string;
    api_key: string;
    has_key: boolean;
    gemini: GeminiConfig;
    azure: AzureConfig;
}

interface ConfirmDialogState {
    title: string;
    message: string;
    confirmLabel: string;
    tone: 'danger' | 'primary';
}

interface ErrorWithStatus extends Error {
    status?: number;
}

const SettingsModal: React.FC<SettingsModalProps> = ({ isOpen, onClose }) => {
    const [loading, setLoading] = useState(false);
    const [switchingPath, setSwitchingPath] = useState(false);
    const [switchStatus, setSwitchStatus] = useState<{ type: 'idle' | 'progress' | 'success' | 'error'; message: string }>({
        type: 'idle',
        message: '',
    });
    const [saveStatus, setSaveStatus] = useState<{ type: 'idle' | 'success' | 'error'; message: string }>({
        type: 'idle',
        message: '',
    });

    const [config, setConfig] = useState<ConfigState>({
        provider: 'openai',
        base_url: '',
        model: 'gpt-4o',
        api_key: '',
        has_key: false,
        gemini: {
            api_key: '',
            model: 'gemini-3-flash-preview',
            alignment_source: 'whisper',
            has_key: false,
        },
        azure: {
            api_key: '',
            region: 'eastus',
            has_key: false,
        },
    });
    const [initialConfig, setInitialConfig] = useState<ConfigState | null>(null);
    const [playbookText, setPlaybookText] = useState('');
    const [playbookIdea, setPlaybookIdea] = useState('');
    const [playbookAiRefine, setPlaybookAiRefine] = useState(true);
    const [playbookLoading, setPlaybookLoading] = useState(false);
    const [playbookSaving, setPlaybookSaving] = useState(false);
    const [playbookStatus, setPlaybookStatus] = useState<{ type: 'idle' | 'success' | 'error'; message: string }>({
        type: 'idle',
        message: '',
    });
    const [confirmDialog, setConfirmDialog] = useState<ConfirmDialogState | null>(null);
    const confirmResolverRef = useRef<((confirmed: boolean) => void) | null>(null);
    const fetchConfigRef = useRef<() => Promise<void>>(async () => undefined);
    const fetchPlaybookRef = useRef<() => Promise<void>>(async () => undefined);
    const handleSaveRef = useRef<(closeAfterSave?: boolean) => Promise<void>>(async () => undefined);
    const handleRequestCloseRef = useRef<() => Promise<void>>(async () => undefined);
    const { isMobile } = useViewportProfile();

    const isBusy = loading || switchingPath || playbookSaving;
    const hasConfigChanges = React.useMemo(() => {
        if (!initialConfig) return true;
        const trim = (v: string) => String(v || '').trim();
        const baseChanged =
            trim(config.provider) !== trim(initialConfig.provider)
            || trim(config.base_url) !== trim(initialConfig.base_url)
            || trim(config.model) !== trim(initialConfig.model)
            || trim(config.gemini.model) !== trim(initialConfig.gemini.model)
            || config.gemini.alignment_source !== initialConfig.gemini.alignment_source
            || trim(config.azure.region) !== trim(initialConfig.azure.region);
        const keyChanged =
            trim(config.api_key).length > 0
            || trim(config.gemini.api_key).length > 0
            || trim(config.azure.api_key).length > 0;
        return baseChanged || keyChanged;
    }, [config, initialConfig]);
    const canSwitchTechPath = !switchingPath && !loading && !hasConfigChanges;
    const nextTechPathLabel = config.gemini.alignment_source === 'gop'
        ? 'Gemini + Whisper skeleton'
        : 'Gemini + GOP skeleton';

    useEffect(() => {
        if (isOpen) {
            setSaveStatus({ type: 'idle', message: '' });
            void fetchConfigRef.current();
            void fetchPlaybookRef.current();
        }
    }, [isOpen]);

    useEffect(() => {
        if (switchStatus.type !== 'success') return;
        const timer = window.setTimeout(() => {
            setSwitchStatus({ type: 'idle', message: '' });
        }, 4500);
        return () => window.clearTimeout(timer);
    }, [switchStatus.type, switchStatus.message]);

    useEffect(() => {
        if (saveStatus.type !== 'success') return;
        const timer = window.setTimeout(() => {
            setSaveStatus({ type: 'idle', message: '' });
        }, 4000);
        return () => window.clearTimeout(timer);
    }, [saveStatus.type, saveStatus.message]);

    useEffect(() => {
        if (!isOpen && confirmResolverRef.current) {
            const resolver = confirmResolverRef.current;
            confirmResolverRef.current = null;
            setConfirmDialog(null);
            resolver(false);
        }
    }, [isOpen]);

    const requestConfirmation = (dialog: ConfirmDialogState): Promise<boolean> => {
        if (confirmResolverRef.current) {
            confirmResolverRef.current(false);
            confirmResolverRef.current = null;
        }
        setConfirmDialog(dialog);
        return new Promise((resolve) => {
            confirmResolverRef.current = resolve;
        });
    };

    const resolveConfirmation = (confirmed: boolean) => {
        const resolver = confirmResolverRef.current;
        confirmResolverRef.current = null;
        setConfirmDialog(null);
        if (resolver) {
            resolver(confirmed);
        }
    };

    const fetchConfig = async () => {
        setLoading(true);
        try {
            const res = await fetch(`${API_HOST}/api/config`);
            if (!res.ok) return;
            const data = await res.json();
            const nextConfig: ConfigState = {
                provider: data.llm.provider,
                base_url: data.llm.base_url || '',
                model: data.llm.model,
                has_key: data.llm.has_key,
                api_key: '',
                gemini: {
                    api_key: '',
                    model: data.gemini.model,
                    alignment_source: data.gemini.alignment_source === 'gop' ? 'gop' : 'whisper',
                    has_key: data.gemini.has_key,
                },
                azure: {
                    api_key: '',
                    region: data.azure.region,
                    has_key: data.azure.has_key,
                },
            };
            setConfig(nextConfig);
            setInitialConfig(nextConfig);
        } catch (error) {
            console.error('Failed to fetch config', error);
        } finally {
            setLoading(false);
        }
    };

    const handleSave = async (closeAfterSave: boolean = false) => {
        if (!hasConfigChanges) {
            setSaveStatus({ type: 'success', message: 'No changes to save.' });
            if (closeAfterSave) {
                onClose();
            }
            return;
        }
        setLoading(true);
        setSaveStatus({ type: 'idle', message: '' });
        try {
            const payload = {
                llm: {
                    provider: config.provider,
                    base_url: config.base_url,
                    model: config.model,
                    api_key: config.api_key || undefined,
                },
                gemini: {
                    api_key: config.gemini.api_key || undefined,
                    model: config.gemini.model,
                    alignment_source: config.gemini.alignment_source,
                },
                azure: {
                    api_key: config.azure.api_key || undefined,
                    region: config.azure.region,
                },
            };

            const res = await fetch(`${API_HOST}/api/config`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });

            if (!res.ok) {
                let detail = '';
                try {
                    const contentType = res.headers.get('content-type') || '';
                    if (contentType.includes('application/json')) {
                        const data = await res.json();
                        detail = String(data?.detail || '').trim();
                    } else {
                        detail = String(await res.text()).trim();
                    }
                } catch {
                    detail = '';
                }
                setSaveStatus({
                    type: 'error',
                    message: detail ? `Failed to save configuration: ${detail}` : 'Failed to save configuration.',
                });
                return;
            }

            setSaveStatus({ type: 'success', message: 'Configuration saved.' });
            await fetchConfig();
            if (closeAfterSave) {
                onClose();
            }
        } catch (error) {
            console.error('Failed to save config', error);
            setSaveStatus({ type: 'error', message: formatActionError('Save configuration', error, 'Please retry') });
        } finally {
            setLoading(false);
        }
    };
    fetchConfigRef.current = fetchConfig;

    const handleRequestClose = async () => {
        if (isBusy) return;
        if (!hasConfigChanges) {
            onClose();
            return;
        }
        const confirmed = await requestConfirmation({
            title: 'Discard Changes',
            message: 'You have unsaved changes. Close without saving?',
            confirmLabel: 'Discard',
            tone: 'danger',
        });
        if (!confirmed) return;
        onClose();
    };
    handleSaveRef.current = handleSave;
    handleRequestCloseRef.current = handleRequestClose;

    const handleResetConfigEdits = () => {
        if (!initialConfig) return;
        setConfig({
            ...initialConfig,
            api_key: '',
            gemini: { ...initialConfig.gemini, api_key: '' },
            azure: { ...initialConfig.azure, api_key: '' },
        });
        setSaveStatus({ type: 'idle', message: '' });
    };

    useEffect(() => {
        if (!isOpen) return;
        const onKeyDown = (event: KeyboardEvent) => {
            const key = String(event.key || '').toLowerCase();
            if (confirmDialog) {
                if (key === 'escape') {
                    event.preventDefault();
                    resolveConfirmation(false);
                    return;
                }
                if (key === 'enter') {
                    event.preventDefault();
                    resolveConfirmation(true);
                }
                return;
            }
            const isSaveHotkey = (event.ctrlKey || event.metaKey) && key === 's';
            if (isSaveHotkey) {
                event.preventDefault();
                if (!loading && !switchingPath && hasConfigChanges) {
                    void handleSaveRef.current();
                }
                return;
            }
            if (key === 'escape') {
                event.preventDefault();
                if (!loading && !switchingPath && !playbookSaving) {
                    void handleRequestCloseRef.current();
                }
            }
        };
        window.addEventListener('keydown', onKeyDown);
        return () => window.removeEventListener('keydown', onKeyDown);
    }, [isOpen, loading, switchingPath, playbookSaving, hasConfigChanges, confirmDialog]);

    const toErrorMessage = (value: unknown, fallback: string): string => {
        if (value instanceof Error && value.message) return value.message;
        const text = String(value ?? '').trim();
        return text || fallback;
    };

    const formatActionError = (action: string, value: unknown, fallback: string): string => {
        return `${action} failed: ${toErrorMessage(value, fallback)}`;
    };

    const fetchPlaybook = async () => {
        setPlaybookLoading(true);
        try {
            const res = await fetch(`${API_HOST}/api/playbook`);
            if (!res.ok) {
                const txt = await res.text();
                throw new Error(txt || 'Failed to load playbook');
            }
            const data = await res.json();
            setPlaybookText(data.text || '');
            setPlaybookStatus({ type: 'idle', message: '' });
        } catch (error: unknown) {
            console.error('Failed to fetch playbook', error);
            setPlaybookStatus({ type: 'error', message: formatActionError('Load playbook', error, 'Unknown error') });
        } finally {
            setPlaybookLoading(false);
        }
    };
    fetchPlaybookRef.current = fetchPlaybook;

    const handleAppendIdea = async () => {
        const idea = playbookIdea.trim();
        if (!idea || playbookSaving) return;
        setPlaybookSaving(true);
        try {
            const res = await fetch(`${API_HOST}/api/playbook/ideas`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ idea, ai_refine: playbookAiRefine }),
            });
            if (!res.ok) {
                const txt = await res.text();
                throw new Error(txt || 'Failed to append idea');
            }
            const data = await res.json();
            setPlaybookIdea('');
            await fetchPlaybook();
            setPlaybookStatus({
                type: 'success',
                message: `Added: ${data?.entry?.key || 'NEW_RULE'} (${data?.source || 'rule'})${data?.warning ? ` | ${data.warning}` : ''}`,
            });
        } catch (error: unknown) {
            console.error('Failed to append playbook idea', error);
            setPlaybookStatus({ type: 'error', message: formatActionError('Append idea', error, 'Unknown error') });
        } finally {
            setPlaybookSaving(false);
        }
    };

    const handleSavePlaybook = async () => {
        const text = playbookText.trim();
        if (!text || playbookSaving) return;
        setPlaybookSaving(true);
        try {
            const res = await fetch(`${API_HOST}/api/playbook`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text }),
            });
            if (!res.ok) {
                const txt = await res.text();
                throw new Error(txt || 'Failed to save playbook');
            }
            setPlaybookStatus({ type: 'success', message: 'Playbook saved' });
        } catch (error: unknown) {
            console.error('Failed to save playbook', error);
            setPlaybookStatus({ type: 'error', message: formatActionError('Save playbook', error, 'Unknown error') });
        } finally {
            setPlaybookSaving(false);
        }
    };

    const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

    const extractErrorDetail = async (res: Response) => {
        try {
            const contentType = res.headers.get('content-type') || '';
            if (contentType.includes('application/json')) {
                const data = await res.json();
                return data?.detail || '';
            }
            return await res.text();
        } catch {
            return '';
        }
    };

    const restartBackendAndWait = async () => {
        const restartRes = await fetch(`${API_HOST}/api/restart`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
        });

        if (!restartRes.ok) {
            const detail = await extractErrorDetail(restartRes);
            const err: ErrorWithStatus = new Error(detail || 'Failed to trigger backend restart');
            err.status = restartRes.status;
            throw err;
        }

        await sleep(1600);
        const deadline = Date.now() + 70000;

        while (Date.now() < deadline) {
            try {
                const healthRes = await fetch(`${API_HOST}/api/health?ts=${Date.now()}`, { cache: 'no-store' });
                if (healthRes.ok) {
                    const data: { status?: string } = await healthRes.json().catch(() => ({}));
                    if (data?.status === 'ok') {
                        return;
                    }
                }
            } catch {
                // Backend may still be restarting.
            }
            await sleep(1200);
        }

        throw new Error('Backend restart timed out');
    };

    const handleToggleTechPath = async () => {
        if (switchingPath || loading) return;
        if (hasConfigChanges) {
            setSwitchStatus({ type: 'error', message: 'Please save or reset settings changes before switching tech path.' });
            return;
        }

        const target = config.gemini.alignment_source === 'gop' ? 'whisper' : 'gop';
        const targetLabel = target === 'gop'
            ? 'Gemini primary + GOP skeleton'
            : 'Gemini primary + Whisper skeleton';
        const confirmed = await requestConfirmation({
            title: 'Switch Tech Path',
            message: `Switch to "${targetLabel}" now? Backend will restart and scoring may be interrupted for about 10-60 seconds.`,
            confirmLabel: 'Switch & Restart',
            tone: 'primary',
        });
        if (!confirmed) return;

        setSwitchingPath(true);
        setSwitchStatus({ type: 'progress', message: 'Saving path config...' });

        try {
            const saveRes = await fetch(`${API_HOST}/api/config`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ gemini: { alignment_source: target } }),
            });

            if (!saveRes.ok) {
                throw new Error('Failed to save path config');
            }

            setConfig(prev => ({
                ...prev,
                gemini: { ...prev.gemini, alignment_source: target },
            }));

            setSwitchStatus({ type: 'progress', message: 'Restarting backend...' });
            await restartBackendAndWait();
            setSwitchStatus({ type: 'success', message: `Backend restart complete. Switched to ${targetLabel}.` });
            await fetchConfig();
        } catch (error: unknown) {
            console.error('Failed to switch tech path', error);
            const statusCode = typeof error === 'object' && error !== null && 'status' in error
                ? Number((error as { status?: unknown }).status)
                : NaN;
            if (statusCode === 409) {
                setSwitchStatus({
                    type: 'error',
                    message: 'Cannot restart now: a job is processing. Retry after current scoring finishes.',
                });
            } else {
                setSwitchStatus({ type: 'error', message: formatActionError('Switch tech path', error, 'Please retry') });
            }
        } finally {
            setSwitchingPath(false);
        }
    };

    if (!isOpen) return null;

    return (
        <div className={`fixed inset-0 z-[100] flex ${isMobile ? 'items-stretch justify-stretch p-0' : 'items-center justify-center p-4'} bg-black/50 backdrop-blur-sm overflow-y-auto`}>
            <div className={`bg-[#1e1e24] w-full ${isMobile ? 'h-[100dvh] rounded-none border-x-0 border-y-0 p-4 my-0' : 'max-w-[min(96vw,1100px)] my-8 p-6 rounded-2xl'} shadow-xl border border-white/10 text-white`}>
                <div className="flex justify-between items-center mb-6">
                    <div className="flex items-center gap-2">
                        <h2 className="text-xl font-bold">Settings</h2>
                        {hasConfigChanges ? (
                            <span className="text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded border border-amber-300/30 bg-amber-500/10 text-amber-200">
                                Unsaved changes
                            </span>
                        ) : null}
                    </div>
                    <button
                        onClick={() => void handleRequestClose()}
                        disabled={isBusy}
                        className="text-gray-400 hover:text-white disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                        x
                    </button>
                </div>

                <div className={`space-y-6 ${isMobile ? 'max-h-[calc(100dvh-120px)] pr-1' : 'max-h-[72vh] pr-2'} overflow-y-auto scrollbar-hide`}>
                    <div className="space-y-4">
                        <div className="flex items-center justify-between">
                            <h3 className="text-sm font-bold text-blue-400 uppercase tracking-wider">Advisor AI (Feedback)</h3>
                            <span className="text-[10px] bg-blue-500/10 text-blue-400 px-2 py-0.5 rounded border border-blue-500/20">OpenAI Compatible</span>
                        </div>

                        <div>
                            <label className="block text-xs font-medium text-gray-400 mb-1">Provider</label>
                            <select
                                value={config.provider}
                                onChange={e => setConfig({ ...config, provider: e.target.value })}
                                className="w-full bg-black/20 border border-white/10 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                            >
                                <option value="openai">OpenAI</option>
                                <option value="zhipu">Zhipu</option>
                                <option value="qwen">Qwen</option>
                                <option value="deepseek">DeepSeek</option>
                                <option value="moonshot">Moonshot</option>
                                <option value="custom">Custom</option>
                            </select>
                        </div>

                        <div className="grid grid-cols-2 gap-4">
                            <div>
                                <label className="block text-xs font-medium text-gray-400 mb-1">Model</label>
                                <input
                                    type="text"
                                    value={config.model}
                                    onChange={e => setConfig({ ...config, model: e.target.value })}
                                    className="w-full bg-black/20 border border-white/10 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                                />
                            </div>
                            <div>
                                <label className="block text-xs font-medium text-gray-400 mb-1">Base URL</label>
                                <input
                                    type="text"
                                    value={config.base_url}
                                    onChange={e => setConfig({ ...config, base_url: e.target.value })}
                                    className="w-full bg-black/20 border border-white/10 rounded-lg px-3 py-2 text-xs text-gray-300 focus:outline-none focus:border-blue-500"
                                />
                            </div>
                        </div>

                        <div>
                            <label className="block text-xs font-medium text-gray-400 mb-1">
                                API Key {config.has_key && <span className="text-green-500">(Configured)</span>}
                            </label>
                            <input
                                type="password"
                                value={config.api_key}
                                onChange={e => setConfig({ ...config, api_key: e.target.value })}
                                placeholder={config.has_key ? 'Keep existing key' : 'key1,key2,key3'}
                                className="w-full bg-black/20 border border-white/10 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                            />
                        </div>
                    </div>

                    <hr className="border-white/5" />

                    <div className="space-y-4">
                        <h3 className="text-sm font-bold text-purple-400 uppercase tracking-wider">Gemini (Scoring Core)</h3>

                        <div>
                            <label className="block text-xs font-medium text-gray-400 mb-1">
                                Gemini API Key {config.gemini.has_key && <span className="text-green-500">(Configured)</span>}
                            </label>
                            <input
                                type="password"
                                value={config.gemini.api_key}
                                onChange={e => setConfig({ ...config, gemini: { ...config.gemini, api_key: e.target.value } })}
                                placeholder={config.gemini.has_key ? 'Keep existing key' : 'AIza...,AIza...'}
                                className="w-full bg-black/20 border border-white/10 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                            />
                        </div>

                        <div>
                            <label className="block text-xs font-medium text-gray-400 mb-1">Model</label>
                            <select
                                value={config.gemini.model}
                                onChange={e => setConfig({ ...config, gemini: { ...config.gemini, model: e.target.value } })}
                                className="w-full bg-black/20 border border-white/10 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                            >
                                <option value="gemini-3-flash-preview">Gemini 3 Flash Preview</option>
                                <option value="gemini-3-pro-preview">Gemini 3 Pro Preview</option>
                                <option value="gemini-1.5-pro">Gemini 1.5 Pro</option>
                                <option value="gemini-1.5-flash">Gemini 1.5 Flash</option>
                            </select>
                        </div>

                        <div className="rounded-lg border border-white/10 bg-black/20 p-3 space-y-2">
                            <div className="text-xs text-gray-400">Tech Path</div>
                            <div className="text-sm text-white font-medium">
                                {config.gemini.alignment_source === 'gop' ? 'Gemini primary + GOP skeleton' : 'Gemini primary + Whisper skeleton'}
                            </div>
                            <button
                                type="button"
                                onClick={handleToggleTechPath}
                                disabled={!canSwitchTechPath}
                                className="w-full px-3 py-2 rounded-lg bg-purple-600 hover:bg-purple-500 text-white text-sm font-medium transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
                                title={switchingPath ? 'Switching tech path...' : `Switch to ${nextTechPathLabel} (backend restart)`}
                            >
                                {switchingPath ? 'Switching...' : `Switch to ${nextTechPathLabel}`}
                            </button>
                            {!canSwitchTechPath && !switchingPath && hasConfigChanges && (
                                <p className="text-[11px] text-amber-300">
                                    Save or reset current edits first, then switch tech path.
                                </p>
                            )}
                            {switchStatus.type !== 'idle' && (
                                <p className={`text-xs ${
                                    switchStatus.type === 'success'
                                        ? 'text-emerald-400'
                                        : switchStatus.type === 'error'
                                            ? 'text-red-400'
                                            : 'text-yellow-300'
                                }`}>
                                    {switchStatus.message}
                                </p>
                            )}
                        </div>
                    </div>

                    <hr className="border-white/5" />

                    <div className="space-y-4">
                        <h3 className="text-sm font-bold text-gray-400 uppercase tracking-wider">Azure (Optional)</h3>
                        <div>
                            <label className="block text-xs font-medium text-gray-400 mb-1">
                                Azure API Key {config.azure.has_key && <span className="text-green-500">(Configured)</span>}
                            </label>
                            <input
                                type="password"
                                value={config.azure.api_key}
                                onChange={e => setConfig({ ...config, azure: { ...config.azure, api_key: e.target.value } })}
                                placeholder={config.azure.has_key ? 'Keep existing key' : 'Key'}
                                className="w-full bg-black/20 border border-white/10 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                            />
                        </div>
                        <div>
                            <label className="block text-xs font-medium text-gray-400 mb-1">Azure Region</label>
                            <input
                                type="text"
                                value={config.azure.region}
                                onChange={e => setConfig({ ...config, azure: { ...config.azure, region: e.target.value } })}
                                placeholder="eastus"
                                className="w-full bg-black/20 border border-white/10 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                            />
                        </div>
                    </div>

                    <hr className="border-white/5" />

                    <div className="space-y-4">
                        <h3 className="text-sm font-bold text-amber-400 uppercase tracking-wider">Pronunciation Playbook</h3>
                        <p className="text-xs text-gray-400">
                            Add teacher ideas and save the markdown knowledge base. Optional Gemini refine can standardize idea text before append.
                        </p>
                        <label className="flex items-center gap-2 text-xs text-gray-300">
                            <input
                                type="checkbox"
                                checked={playbookAiRefine}
                                onChange={e => setPlaybookAiRefine(e.target.checked)}
                                className="accent-amber-500"
                            />
                            Use Gemini refine before append
                        </label>
                        <textarea
                            value={playbookIdea}
                            onChange={e => setPlaybookIdea(e.target.value)}
                            placeholder="Example: technique: final voiced ending lock; focus_words: sausages,lessons; drill: slow endings then short sentence chaining; mnemonic: final sound is a stamp."
                            className="w-full min-h-[84px] bg-black/20 border border-white/10 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-amber-500"
                        />
                        <div className="flex gap-2">
                            <button
                                type="button"
                                onClick={handleAppendIdea}
                                disabled={playbookSaving || !playbookIdea.trim()}
                                className="px-3 py-2 rounded-lg bg-amber-600 hover:bg-amber-500 text-white text-sm font-medium transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
                            >
                                {playbookSaving ? 'Processing...' : 'Append Idea'}
                            </button>
                            <button
                                type="button"
                                onClick={fetchPlaybook}
                                disabled={playbookLoading || playbookSaving}
                                className="px-3 py-2 rounded-lg bg-white/10 hover:bg-white/20 text-white text-sm transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
                            >
                                Refresh
                            </button>
                        </div>
                        <textarea
                            value={playbookText}
                            onChange={e => setPlaybookText(e.target.value)}
                            placeholder="Playbook Markdown"
                            className="w-full min-h-[180px] bg-black/20 border border-white/10 rounded-lg px-3 py-2 text-xs text-gray-200 font-mono focus:outline-none focus:border-amber-500"
                        />
                        <button
                            type="button"
                            onClick={handleSavePlaybook}
                            disabled={playbookSaving || !playbookText.trim()}
                            className="px-3 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-medium transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
                        >
                            {playbookSaving ? 'Saving...' : 'Save Playbook'}
                        </button>
                        {playbookStatus.type !== 'idle' && (
                            <p className={`text-xs ${playbookStatus.type === 'success' ? 'text-emerald-400' : 'text-red-400'}`}>
                                {playbookStatus.message}
                            </p>
                        )}
                    </div>

                    <hr className="border-white/5" />

                    <div className="space-y-4">
                        <h3 className="text-sm font-bold text-green-400 uppercase tracking-wider">Performance (Queue)</h3>
                        <div>
                            <label className="block text-xs font-medium text-gray-400 mb-1">
                                Max Concurrency: <span className="text-white font-bold">4 (Forced)</span>
                            </label>
                            <p className="text-[10px] text-gray-500 mt-1">
                                Upload queue uses fixed 4 parallel workers to keep throughput stable.
                            </p>
                        </div>
                    </div>
                </div>

                <div className="mt-8 flex items-center justify-between gap-3">
                    <div>
                        {saveStatus.type !== 'idle' && (
                            <p className={`text-xs ${saveStatus.type === 'success' ? 'text-emerald-400' : 'text-red-400'}`}>
                                {saveStatus.message}
                            </p>
                        )}
                        {saveStatus.type === 'idle' && !hasConfigChanges && (
                            <p className="text-xs text-gray-500">No unsaved changes.</p>
                        )}
                        <p className="text-[11px] text-gray-500 mt-1">Shortcut: Ctrl/Cmd + S to save, Esc to close.</p>
                    </div>
                    <div className="flex items-center gap-3">
                        <button
                            onClick={handleResetConfigEdits}
                            disabled={!hasConfigChanges || loading}
                            className="px-4 py-2 rounded-lg text-gray-300 hover:bg-white/5 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                            Reset Changes
                        </button>
                        <button
                            onClick={() => void handleRequestClose()}
                            disabled={isBusy}
                            className="px-4 py-2 rounded-lg text-gray-300 hover:bg-white/5 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                            Cancel
                        </button>
                        <button
                            onClick={() => void handleSave()}
                            disabled={loading || !hasConfigChanges}
                            className="px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 text-white font-medium transition-colors disabled:opacity-50"
                        >
                            {loading ? 'Saving...' : 'Save Changes'}
                        </button>
                        <button
                            onClick={() => void handleSave(true)}
                            disabled={loading || !hasConfigChanges}
                            className="px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-medium transition-colors disabled:opacity-50"
                        >
                            {loading ? 'Saving...' : 'Save & Close'}
                        </button>
                    </div>
                </div>

                {confirmDialog && (
                    <div className="fixed inset-0 z-[120] flex items-center justify-center bg-black/70 backdrop-blur-sm p-4">
                        <div className="w-full max-w-md rounded-2xl border border-white/10 bg-[#141419] shadow-2xl">
                            <div className="px-5 py-4 border-b border-white/10">
                                <h3 className="text-base font-bold text-white">{confirmDialog.title}</h3>
                                <p className="text-xs text-gray-300 mt-1">{confirmDialog.message}</p>
                            </div>
                            <div className="px-5 py-4 flex justify-end gap-2">
                                <button
                                    type="button"
                                    onClick={() => resolveConfirmation(false)}
                                    className="px-3 py-2 text-sm rounded-lg border border-white/20 text-gray-300 hover:text-white hover:border-white/40 transition-colors"
                                >
                                    Cancel
                                </button>
                                <button
                                    type="button"
                                    onClick={() => resolveConfirmation(true)}
                                    className={`px-4 py-2 text-sm rounded-lg text-white font-medium transition-colors ${
                                        confirmDialog.tone === 'danger'
                                            ? 'bg-red-600 hover:bg-red-500'
                                            : 'bg-blue-600 hover:bg-blue-500'
                                    }`}
                                >
                                    {confirmDialog.confirmLabel}
                                </button>
                            </div>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
};

export default SettingsModal;
