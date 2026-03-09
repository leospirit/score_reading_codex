export type FeedbackStatusHint = {
    tone: 'info' | 'warning';
    text: string;
};

type FeedbackOptimizationState = {
    status?: 'pending' | 'optimizing' | 'frozen' | 'final';
    current_provider?: string;
    last_error?: string;
};

type FeedbackStatusHintInput = {
    feedbackSourceTag?: string;
    feedbackOptimization?: FeedbackOptimizationState | null;
};

export function getFeedbackStatusHint(input: FeedbackStatusHintInput): FeedbackStatusHint | null {
    const feedbackSourceTag = String(input.feedbackSourceTag || '').trim().toLowerCase();
    const state = input.feedbackOptimization || {};
    const status = String(state.status || '').trim().toLowerCase();
    const provider = String(state.current_provider || '').trim().toLowerCase();
    const hasError = String(state.last_error || '').trim().length > 0;

    const isAzureVisible = feedbackSourceTag === 'az' || provider === 'azure_fallback';
    if (!isAzureVisible) return null;

    if (status === 'optimizing') {
        return {
            tone: 'info',
            text: '正在优化点评，当前先显示 Azure 反馈。',
        };
    }

    if (status === 'pending' && hasError) {
        return {
            tone: 'warning',
            text: '优化失败，当前保留 Azure 反馈。',
        };
    }

    if (status === 'pending') {
        return {
            tone: 'info',
            text: '正在优化点评，当前先显示 Azure 反馈。',
        };
    }

    return null;
}
