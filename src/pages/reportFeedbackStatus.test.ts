import test from 'node:test';
import assert from 'node:assert/strict';

import { getFeedbackStatusHint } from './reportFeedbackStatus.ts';

test('shows azure-visible hint while optimization is still running', () => {
    const hint = getFeedbackStatusHint({
        feedbackSourceTag: 'az',
        feedbackOptimization: {
            status: 'pending',
            current_provider: 'azure_fallback',
            last_error: '',
        },
    });

    assert.deepEqual(hint, {
        tone: 'info',
        text: '正在优化点评，当前先显示 Azure 反馈。',
    });
});

test('shows azure fallback hint after optimization failure', () => {
    const hint = getFeedbackStatusHint({
        feedbackSourceTag: 'az',
        feedbackOptimization: {
            status: 'pending',
            current_provider: 'azure_fallback',
            last_error: 'volcengine timeout',
        },
    });

    assert.deepEqual(hint, {
        tone: 'warning',
        text: '优化失败，当前保留 Azure 反馈。',
    });
});

test('returns null when feedback is already finalized by non-azure provider', () => {
    const hint = getFeedbackStatusHint({
        feedbackSourceTag: 'db',
        feedbackOptimization: {
            status: 'final',
            current_provider: 'volcengine',
            last_error: '',
        },
    });

    assert.equal(hint, null);
});
