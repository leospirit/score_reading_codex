import { useEffect, useState } from 'react';

type ViewportProfile = 'mobile' | 'desktop';

interface ViewportInfo {
    profile: ViewportProfile;
    isMobile: boolean;
    width: number;
    height: number;
    aspectRatio: number;
}

const detectProfile = (): ViewportInfo => {
    if (typeof window === 'undefined') {
        return {
            profile: 'desktop',
            isMobile: false,
            width: 1366,
            height: 768,
            aspectRatio: 768 / 1366,
        };
    }

    const width = window.innerWidth || 1366;
    const height = window.innerHeight || 768;
    const aspectRatio = height / Math.max(1, width);
    const coarsePointer = window.matchMedia?.('(pointer: coarse)').matches ?? false;
    const touchCapable = navigator.maxTouchPoints > 0;

    const isMobileByWidth = width <= 900;
    const isMobileByDevice = (coarsePointer || touchCapable) && (width <= 1100 || aspectRatio > 1.0);
    const isMobile = isMobileByWidth || isMobileByDevice;

    return {
        profile: isMobile ? 'mobile' : 'desktop',
        isMobile,
        width,
        height,
        aspectRatio,
    };
};

export const useViewportProfile = (): ViewportInfo => {
    const [viewport, setViewport] = useState<ViewportInfo>(detectProfile);

    useEffect(() => {
        const update = () => setViewport(detectProfile());
        update();
        window.addEventListener('resize', update);
        window.addEventListener('orientationchange', update);
        return () => {
            window.removeEventListener('resize', update);
            window.removeEventListener('orientationchange', update);
        };
    }, []);

    return viewport;
};

