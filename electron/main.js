const path = require('path');
const { app, BrowserWindow, Menu, ipcMain, session } = require('electron');
const { startServer, PORT } = require('../server');

// The live site selects its desktop interface when the user agent contains
// neither "iPhone" nor "Android". Electron's own user agent already does
// that, so this window does not spoof a phone.
app.commandLine.appendSwitch(
  'enable-features',
  'WebBluetooth,WebBluetoothNewPermissionsBackend',
);
app.commandLine.appendSwitch('enable-experimental-web-platform-features');
app.commandLine.appendSwitch(
  'disable-features',
  'ThirdPartyStoragePartitioning,BlockThirdPartyCookies',
);

const PARTITION = 'persist:puffco';

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) app.quit();

function webPreferences() {
  return {
    partition: PARTITION,
    nodeIntegration: false,
    contextIsolation: true,
    sandbox: true,
    backgroundThrottling: false,
  };
}

function configureSession(ses) {
  ses.setPermissionRequestHandler((_contents, permission, callback) => {
    callback(permission !== 'serial' && permission !== 'hid' && permission !== 'usb');
  });
  ses.setPermissionCheckHandler((_contents, permission) => {
    return permission !== 'serial' && permission !== 'hid' && permission !== 'usb';
  });
  ses.setDevicePermissionHandler((details) => details.deviceType === 'bluetooth');
  ses.setBluetoothPairingHandler((details, callback) => {
    if (details.pairingKind === 'confirm' || details.pairingKind === 'confirmPin') {
      callback({ confirmed: true });
      return;
    }
    callback({ confirmed: false });
  });
}

const pickerOwners = new Map();
let pickerIpcReady = false;

function safeInvoke(callback, deviceId) {
  if (typeof callback !== 'function') return;
  try {
    callback(deviceId);
  } catch (error) {
    console.error('Bluetooth chooser callback failed', error);
  }
}

function ensurePickerIpc() {
  if (pickerIpcReady) return;
  pickerIpcReady = true;
  const finish = (senderId, deviceId) => {
    const state = pickerOwners.get(senderId);
    if (!state) return;
    const callback = state.callback;
    state.callback = null;
    state.suppressCancel = true;
    safeInvoke(callback, deviceId);
    const win = state.win;
    if (win && !win.isDestroyed()) win.close();
  };
  ipcMain.on('bluetooth-select', (event, deviceId) => {
    finish(event.sender.id, typeof deviceId === 'string' ? deviceId : '');
  });
  ipcMain.on('bluetooth-cancel', (event) => finish(event.sender.id, ''));
}

function attachBluetooth(contents) {
  ensurePickerIpc();
    const state = { callback: null, win: null, devices: [], opening: false, suppressCancel: false };
  const send = () => {
    const win = state.win;
    if (!win || win.isDestroyed()) return;
    let pickerContents;
    try {
      pickerContents = win.webContents;
    } catch {
      return;
    }
    if (!pickerContents || pickerContents.isDestroyed() || pickerContents.isLoading()) return;
    pickerContents.send(
      'bluetooth-devices',
      state.devices.map((device) => ({
        deviceId: device.deviceId,
        deviceName: device.deviceName || 'Unknown device',
      })),
    );
  };
  const openPicker = () => {
    if (contents.isDestroyed()) return;
    if (state.win && !state.win.isDestroyed()) {
      send();
      return;
    }
    // Not modal and not a child window. A modal chooser is destroyed while
    // the scan is still running, and touching it crashes the main process.
    const win = new BrowserWindow({
      width: 360,
      height: 480,
      resizable: false,
      minimizable: false,
      maximizable: false,
      title: 'Connect a device',
      backgroundColor: '#141414',
      autoHideMenuBar: true,
      show: false,
      alwaysOnTop: true,
      webPreferences: {
        preload: path.join(__dirname, 'picker-preload.js'),
        nodeIntegration: false,
        contextIsolation: true,
        sandbox: true,
      },
    });
    const pickerId = win.webContents.id;
    pickerOwners.set(pickerId, state);
    state.win = win;
    state.suppressCancel = false;
    win.once('ready-to-show', () => {
      if (!win.isDestroyed()) win.show();
    });
    win.webContents.on('did-finish-load', send);
    win.on('close', () => {
      if (state.suppressCancel) return;
      const callback = state.callback;
      state.callback = null;
      safeInvoke(callback, '');
    });
    win.on('closed', () => {
      pickerOwners.delete(pickerId);
      if (state.win === win) state.win = null;
    });
    win.loadFile(path.join(__dirname, 'picker.html'));
  };
  contents.on('select-bluetooth-device', (event, deviceList, callback) => {
    event.preventDefault();
    state.callback = callback;
    state.devices = deviceList || [];
    if (state.win && !state.win.isDestroyed()) {
      send();
      if (!state.win.isDestroyed()) state.win.focus();
      return;
    }
    if (state.opening) return;
    state.opening = true;
    setImmediate(() => {
      state.opening = false;
      openPicker();
    });
  });
}

function createWindow(port) {
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 960,
    minHeight: 640,
    show: false,
    title: 'Puffco',
    backgroundColor: '#121212',
    autoHideMenuBar: true,
    webPreferences: webPreferences(),
  });
  win.setMenuBarVisibility(false);
  win.once('ready-to-show', () => {
    if (win.isDestroyed()) return;
    win.maximize();
    win.show();
  });
  attachBluetooth(win.webContents);
  win.loadURL(`http://127.0.0.1:${port}/`);
  return win;
}

if (gotLock) {
  app.whenReady().then(async () => {
    Menu.setApplicationMenu(null);
    configureSession(session.fromPartition(PARTITION));
    const { port } = await startServer(PORT);
    const win = createWindow(port);
    app.on('second-instance', () => {
      if (win.isDestroyed()) return;
      if (win.isMinimized()) win.restore();
      win.focus();
    });
  });

  app.on('window-all-closed', () => {
    app.quit();
  });
}
