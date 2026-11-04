import { createRoot } from 'react-dom/client';

import { App } from './App';
import './styles.css';

const host = document.getElementById('root');
if (host === null) throw new Error('index.html has no #root to mount the console on');

// No StrictMode. Its deliberate mount-unmount-remount would open a second socket against the same
// lounge, and a demo whose participant count flickers on a dev-only double render teaches the
// reader to distrust the one number §6.1 says must agree everywhere. The socket hook's own cleanup
// is still exercised — every reconnect path runs it — just not twice in a row on boot.
createRoot(host).render(<App />);
