import { useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, Code } from 'lucide-react';
import UploadModal from './UploadModal';
import { useViewportProfile } from '../hooks/useViewportProfile';

export default function Hero() {
    const [isUploadOpen, setIsUploadOpen] = useState(false);
    const { isMobile } = useViewportProfile();

    return (
        <div className="relative min-h-[100svh] flex flex-col items-center justify-center overflow-hidden pt-20 pb-8 md:pt-24 md:pb-28">
            {/* Modals */}
            <UploadModal isOpen={isUploadOpen} onClose={() => setIsUploadOpen(false)} />

            {/* Background Effects */}
            <div className="absolute top-0 left-0 w-full h-full overflow-hidden -z-10">
                <div className="absolute top-1/4 left-1/4 w-96 h-96 bg-primary/20 rounded-full blur-3xl animate-pulse"></div>
                <div className="absolute bottom-1/4 right-1/4 w-96 h-96 bg-secondary/20 rounded-full blur-3xl animate-pulse delay-1000"></div>
                <div className="absolute inset-0 bg-[linear-gradient(rgba(5,5,16,0)_1px,transparent_1px),linear-gradient(90deg,rgba(5,5,16,0)_1px,transparent_1px)] bg-[size:40px_40px] [background-position:center] [mask-image:radial-gradient(ellipse_60%_50%_at_50%_50%,#000_70%,transparent_100%)] opacity-20"></div>
            </div>

            {/* Hero Content */}
            <div className={`text-center max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 z-10 space-y-7 md:space-y-8 ${isMobile ? '' : 'animate-float'}`}>
                <div className="inline-flex items-center gap-2 px-4 py-2 rounded-full bg-white/5 border border-white/10 text-sm text-primary mb-4 backdrop-blur-md">
                    <span className="relative flex h-2 w-2">
                        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-primary opacity-75"></span>
                        <span className="relative inline-flex rounded-full h-2 w-2 bg-primary"></span>
                    </span>
                    AI-Powered Assessment
                </div>

                <h1 className="text-4xl sm:text-5xl md:text-7xl lg:text-8xl font-black tracking-tight leading-tight text-transparent bg-clip-text bg-gradient-to-r from-white via-white to-gray-500">
                    MASTER YOUR <br />
                    <span className="text-transparent bg-clip-text bg-gradient-to-r from-primary to-secondary">SPOKEN ENGLISH</span>
                </h1>

                <p className="text-base md:text-xl text-gray-400 max-w-3xl mx-auto leading-relaxed">
                    Professional-grade pronunciation scoring, intonation analysis, and personalized AI coaching.
                    Powered by multi-engine speech analysis and LLM technology.
                </p>

                <div className="flex flex-col sm:flex-row gap-4 justify-center items-center mt-8">
                    <button
                        onClick={() => setIsUploadOpen(true)}
                        className="group relative px-8 py-4 bg-primary text-background font-bold text-lg rounded-none skew-x-[-10deg] hover:bg-white transition-colors duration-300"
                    >
                        <span className="block skew-x-[10deg] flex items-center gap-2">
                            UPLOAD AUDIO <ArrowRight className="w-5 h-5 group-hover:translate-x-1 transition-transform" />
                        </span>
                    </button>
                    <Link
                        to="/history"
                        className="group px-8 py-4 bg-transparent border border-white/20 text-white font-medium text-lg rounded-none skew-x-[-10deg] hover:border-primary/50 hover:bg-white/5 transition-all duration-300 flex items-center justify-center text-decoration-none"
                    >
                        <span className="block skew-x-[10deg] flex items-center gap-2">
                            <Code className="w-5 h-5 text-gray-400 group-hover:text-primary transition-colors" /> VIEW REPORT
                        </span>
                    </Link>
                    <Link
                        to="/word-clips"
                        className="group px-8 py-4 bg-transparent border border-cyan-400/30 text-cyan-200 font-medium text-lg rounded-none skew-x-[-10deg] hover:border-cyan-300 hover:bg-cyan-400/10 transition-all duration-300 flex items-center justify-center text-decoration-none"
                    >
                        <span className="block skew-x-[10deg] flex items-center gap-2">
                            <ArrowRight className="w-5 h-5" /> WORD CLIPS
                        </span>
                    </Link>
                </div>
            </div>

            {/* Stats/Social Proof */}
            <div className={`w-full border-t border-white/5 bg-black/20 backdrop-blur-sm ${isMobile ? 'mt-10' : 'absolute bottom-0'}`}>
                <div className="max-w-7xl mx-auto px-4 py-5 md:py-6 grid grid-cols-2 md:grid-cols-4 gap-4 md:gap-8 text-center">
                    <div className="rounded-lg border border-white/5 bg-white/[0.02] p-3 md:p-0 md:bg-transparent md:border-0">
                        <div className="text-2xl md:text-3xl font-bold text-white">GOP</div>
                        <div className="text-[10px] md:text-xs text-gray-500 uppercase tracking-[0.15em] mt-1">Standard Scoring</div>
                    </div>
                    <div className="rounded-lg border border-white/5 bg-white/[0.02] p-3 md:p-0 md:bg-transparent md:border-0">
                        <div className="text-2xl md:text-3xl font-bold text-white">AI</div>
                        <div className="text-[10px] md:text-xs text-gray-500 uppercase tracking-[0.15em] mt-1">Personalized Coach</div>
                    </div>
                    <div className="rounded-lg border border-white/5 bg-white/[0.02] p-3 md:p-0 md:bg-transparent md:border-0">
                        <div className="text-2xl md:text-3xl font-bold text-white">100%</div>
                        <div className="text-[10px] md:text-xs text-gray-500 uppercase tracking-[0.15em] mt-1">Privacy Focused</div>
                    </div>
                    <div className="rounded-lg border border-white/5 bg-white/[0.02] p-3 md:p-0 md:bg-transparent md:border-0">
                        <div className="text-2xl md:text-3xl font-bold text-white">24/7</div>
                        <div className="text-[10px] md:text-xs text-gray-500 uppercase tracking-[0.15em] mt-1">Availability</div>
                    </div>
                </div>
            </div>
        </div>
    );
}
