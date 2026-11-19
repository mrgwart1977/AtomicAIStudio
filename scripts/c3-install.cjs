const { execFile } = require('child_process');
const installer = 'C:\\Projects\\atomic-ai-studio\\AtomicAIStudio\\apps\\desktop\\src-tauri\\target\\release\\bundle\\nsis\\Atomic Dream Engine_0.1.0_x64-setup.exe';
execFile(installer, ['/S'], (e) => {
  if (e) { console.error('FAIL', e.message); process.exit(1); }
  console.log('installer exited (NSIS /S)');
});
