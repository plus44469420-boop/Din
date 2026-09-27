const path = require('path');
const { app, BrowserWindow, Menu, session } = require('electron');
const { attachBluetoothPairing, attachBluetoothPicker } = require('./bluetooth-picker');
const { configureSession } = require('./configure-session');
const {
  DEFAULT_CONTENT_SIZE,
  IPHONE_WEBVIEW_UA,
  PARTITION,
  START_URL,
} = require('./constants');
const { attachLinkPolicy } = require('./navigation');
const { loadWindowState, trackWindowState } = require('./window-state');

app.setName('Puffco');
app.userAgentFallback = IPHONE_WEBVIEW_UA;

// Web Bluetooth is how the page talks to Puffco hardware.
// The new permissions backend exposes getDevices() so a device the user
// already allowed can reconnect without another chooser.
// Third-party cookie partitioning is left off so Google and Apple sign-in
// can keep their session inside the window.
app.commandLine.appendSwitch(
  'enable-features',
  'WebBluetooth,WebBluetoothNewPermissionsBackend',
);
app.commandLine.appendSwitch('enable-experimental-web-platform-features');
app.commandLine.appendSwitch(
  'disable-features',
  'ThirdPartyStoragePartitioning,BlockThirdPartyCookies',
);
app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required');

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
}

const ICON = path.join(__dirname, '..', 'assets', 'icon.png');
let mainWindow = null;

function sharedWebPreferences() {
  return {
    partition: PARTITION,
    nodeIntegration: false,
    contextIsolation: true,
    sandbox: true,
    backgroundThrottling: false,
    spellcheck: false,
  };
}

function showOffline(contents) {
  const offline = path.join(__dirname, 'offline.html');
  if (contents.getURL().startsWith('file://') && contents.getURL().includes('offline.html')) {
    return;
  }
  contents.loadFile(offline);
}

function createMainWindow() {
  const saved = loadWindowState();
  const win = new BrowserWindow({
    width: saved?.width ?? DEFAULT_CONTENT_SIZE.width,
    height: saved?.height ?? DEFAULT_CONTENT_SIZE.height,
    x: Number.isFinite(saved?.x) ? saved.x : undefined,
    y: Number.isFinite(saved?.y) ? saved.y : undefined,
    useContentSize: !saved,
    minWidth: 320,
    minHeight: 480,
    title: 'Puffco',
    backgroundColor: '#141414',
    autoHideMenuBar: true,
    icon: ICON,
    show: false,
    resizable: true,
    webPreferences: sharedWebPreferences(),
  });

  win.setMenuBarVisibility(false);
  if (!saved) win.center();
  trackWindowState(win);
  win.once('ready-to-show', () => {
    win.show();
    if (saved?.maximized) win.maximize();
  });
  win.on('closed', () => {
    if (mainWindow === win) mainWindow = null;
  });

  if (process.argv.includes('--dev')) {
    win.webContents.openDevTools({ mode: 'detach' });
  }

  win.loadURL(START_URL);
  return win;
}

function attachContents(contents) {
  if (contents.session !== session.fromPartition(PARTITION)) return;

  contents.setUserAgent(IPHONE_WEBVIEW_UA);
  try {
    const zoom = contents.setVisualZoomLevelLimits(1, 1);
    if (zoom && typeof zoom.catch === 'function') zoom.catch(() => {});
  } catch {
    // Older Electron builds expose this on the renderer webFrame instead.
  }
  attachLinkPolicy(contents);
  attachBluetoothPicker(contents);
  contents.on('did-fail-load', (_event, errorCode, _description, validatedURL, isMainFrame) => {
    if (!isMainFrame || errorCode === -3) return;
    if (!validatedURL || validatedURL.startsWith('file://')) return;
    if (validatedURL.startsWith('devtools://') || validatedURL.startsWith('chrome-devtools://')) {
      return;
    }
    showOffline(contents);
  });
}

if (gotLock) {
  app.whenReady().then(() => {
    Menu.setApplicationMenu(null);
    const appSession = session.fromPartition(PARTITION);
    configureSession(appSession, IPHONE_WEBVIEW_UA);
    attachBluetoothPairing(appSession);
    app.on('browser-window-created', (_event, win) => {
      win.on('page-title-updated', (event) => {
        event.preventDefault();
      });
    });
    app.on('web-contents-created', (_event, contents) => {
      attachContents(contents);
    });

    mainWindow = createMainWindow();

    app.on('second-instance', () => {
      if (!mainWindow) {
        mainWindow = createMainWindow();
        return;
      }
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    });

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) {
        mainWindow = createMainWindow();
      }
    });
  });

  app.on('window-all-closed', () => {
    app.quit();
  });
}
