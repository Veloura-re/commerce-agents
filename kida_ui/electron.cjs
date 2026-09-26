const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const { spawn } = require('child_process');
const fs = require('fs');

let mainWindow;
let botProcess = null;

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false
    },
    title: 'KIDA Citadel',
    backgroundColor: '#050505'
  });

  startBot();

  mainWindow.loadFile(path.join(__dirname, 'index.html'));

  mainWindow.on('closed', () => {
    mainWindow = null;
    if (botProcess) {
      botProcess.kill();
    }
  });
}

ipcMain.handle('get-state', () => {
  const rootPath = path.join(__dirname, '..');
  const auditPath = path.join(rootPath, 'aitrader', 'outputs', 'session_audit.json');
  try {
    if (fs.existsSync(auditPath)) {
      return JSON.parse(fs.readFileSync(auditPath, 'utf-8'));
    }
  } catch (e) {
    console.error("Failed to read audit:", e);
  }
  return null;
});

ipcMain.handle('get-logs', () => {
  const rootPath = path.join(__dirname, '..');
  const logPath = path.join(rootPath, 'aitrader', 'outputs', 'rotation_session_1h.log');
  try {
    if (fs.existsSync(logPath)) {
      const content = fs.readFileSync(logPath, 'utf-8');
      const lines = content.split('\n');
      return lines.slice(-100);
    }
  } catch (e) {
    console.error("Failed to read log:", e);
  }
  return [];
});

ipcMain.handle('get-weights', () => {
  const rootPath = path.join(__dirname, '..');
  const weightPath = path.join(rootPath, 'aitrader', 'outputs', 'ai_weights.json');
  try {
    if (fs.existsSync(weightPath)) {
      return JSON.parse(fs.readFileSync(weightPath, 'utf-8'));
    }
  } catch (e) {
    console.error("Failed to read weights:", e);
  }
  return null;
});

let apiProcess = null;

function startBot() {
  const rootPath = path.join(__dirname, '..');
  
  // Start the FastAPI backend
  const apiPath = path.join(rootPath, 'aitrader');
  apiProcess = spawn('bash', ['-c', 'uvicorn app:app --host 127.0.0.1 --port 8000'], {
    cwd: apiPath,
    stdio: 'ignore'
  });

  // Start the KIDA Bot engine
  botProcess = spawn('bash', ['-c', 'python3 -m kida_bot.main'], {
    cwd: rootPath,
    stdio: 'ignore'
  });
}

app.whenReady().then(createWindow);

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('quit', () => {
  if (botProcess) botProcess.kill();
  if (apiProcess) apiProcess.kill();
});

