import React, { useEffect, useState } from 'react';

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

const SettingsModal: React.FC<SettingsModalProps> = ({ isOpen, onClose }) => {
    const [loading, setLoading] = useState(false);
    const [switchingPath, setSwitchingPath] = useState(false);
    const [switchStatus, setSwitchStatus] = useState<{ type: 'idle' | 'progress' | 'success' | 'error'; message: string }>({
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

    const API_HOST = '';

    useEffect(() => {
        if (isOpen) {
            void fetchConfig();
        }
    }, [isOpen]);

    const fetchConfig = async () => {
        setLoading(true);
        try {
            const res = await fetch(`${API_HOST}/api/config`);
            if (!res.ok) return;
            const data = await res.json();
            setConfig({
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
            });
        } catch (error) {
            console.error('Failed to fetch config', error);
        } finally {
            setLoading(false);
        }
    };

    const handleSave = async () => {
        setLoading(true);
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
                alert('Failed to save configuration.');
                return;
            }

            alert('Configuration saved.');
            onClose();
        } catch (error) {
            console.error('Failed to save config', error);
            alert('Error saving configuration.');
        } finally {
            setLoading(false);
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
            const err: any = new Error(detail || 'Failed to trigger backend restart');
            err.status = restartRes.status;
            throw err;
        }

        await sleep(1600);
        const deadline = Date.now() + 70000;

        while (Date.now() < deadline) {
            try {
                const healthRes = await fetch(`${API_HOST}/api/health?ts=${Date.now()}`, { cache: 'no-store' });
                if (healthRes.ok) {
                    const data = await healthRes.json().catch(() => ({} as any));
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

        const target = config.gemini.alignment_source === 'gop' ? 'whisper' : 'gop';
        const targetLabel = target === 'gop'
            ? 'Gemini primary + GOP skeleton'
            : 'Gemini primary + Whisper skeleton';

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
        } catch (error: any) {
            console.error('Failed to switch tech path', error);
            if (Number(error?.status) === 409) {
                setSwitchStatus({
                    type: 'error',
                    message: 'Cannot restart now: a job is processing. Retry after current scoring finishes.',
                });
            } else {
                setSwitchStatus({ type: 'error', message: 'Switch failed. Please retry.' });
            }
        } finally {
            setSwitchingPath(false);
        }
    };

    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/50 backdrop-blur-sm p-4 overflow-y-auto">
            <div className="bg-[#1e1e24] w-full max-w-md my-8 p-6 rounded-2xl shadow-xl border border-white/10 text-white">
                <div className="flex justify-between items-center mb-6">
                    <h2 className="text-xl font-bold">Settings</h2>
                    <button onClick={onClose} className="text-gray-400 hover:text-white">x</button>
                </div>

                <div className="space-y-6 max-h-[70vh] overflow-y-auto pr-2 scrollbar-hide">
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
                                disabled={switchingPath || loading}
                                className="w-full px-3 py-2 rounded-lg bg-purple-600 hover:bg-purple-500 text-white text-sm font-medium transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
                            >
                                {switchingPath ? 'Switching...' : 'Switch Tech Path (with backend restart)'}
                            </button>
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

                <div className="mt-8 flex justify-end gap-3">
                    <button
                        onClick={onClose}
                        className="px-4 py-2 rounded-lg text-gray-300 hover:bg-white/5 transition-colors"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={handleSave}
                        disabled={loading}
                        className="px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 text-white font-medium transition-colors disabled:opacity-50"
                    >
                        {loading ? 'Saving...' : 'Save Changes'}
                    </button>
                </div>
            </div>
        </div>
    );
};

export default SettingsModal;
