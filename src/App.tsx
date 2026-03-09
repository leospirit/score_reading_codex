import { Suspense, lazy } from 'react';
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';
import Navigation from './components/Navigation';

const Dashboard = lazy(() => import('./pages/Dashboard'));
const DashboardPro = lazy(() => import('./pages/DashboardPro'));
const HistoryPage = lazy(() => import('./pages/History'));
const ReportBuilder = lazy(() => import('./pages/ReportBuilder'));
const WordClipStudio = lazy(() => import('./pages/WordClipStudio'));
const DiagnosticsPage = lazy(() => import('./pages/Diagnostics'));

function App() {
  return (
    <Router>
      <div className="bg-background min-h-screen text-text font-body selection:bg-primary selection:text-background">
        <Navigation />

        <Suspense fallback={(
          <div className="mx-auto max-w-7xl px-4 py-16 text-center text-sm text-gray-400">
            Loading...
          </div>
        )}
        >
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/dashboard-pro" element={<DashboardPro />} />
            <Route path="/history" element={<HistoryPage />} />
            <Route path="/report-builder" element={<ReportBuilder />} />
            <Route path="/word-clips" element={<WordClipStudio />} />
            <Route path="/diagnostics" element={<DiagnosticsPage />} />
          </Routes>
        </Suspense>

        {/* Simple Footer */}
        <footer className="border-t border-white/10 py-12 bg-black/40 mt-auto">
          <div className="max-w-7xl mx-auto px-4 text-center text-gray-500 text-sm">
            <p>&copy; 2026 SpeechMaster. All rights reserved.</p>
          </div>
        </footer>
      </div>
    </Router>
  );
}

export default App;
