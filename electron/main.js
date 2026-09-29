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
    preload: path.join(__dirname, 'shell-preload.js'),
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
    try {
      if (details.pairingKind === 'confirm' || details.pairingKind === 'confirmPin') {
        callback({ confirmed: true });
        return;
      }
      callback({ confirmed: false });
    } catch (error) {
      console.error('Bluetooth pairing handler failed', error);
    }
  });
}

const choosers = new Map();
let chooserIpcReady = false;

function safeInvoke(callback, deviceId) {
  if (typeof callback !== 'function') return;
  try {
    callback(deviceId);
  } catch (error) {
    console.error('Bluetooth chooser callback failed', error);
  }
}

function sendTo(contents, channel, payload) {
  if (!contents || contents.isDestroyed()) return;
  try {
    contents.send(channel, payload);
  } catch (error) {
    console.error(error);
  }
}

function devicePayload(devices) {
  return (devices || []).map((device) => ({
    deviceId: device.deviceId,
    deviceName: device.deviceName || 'Unknown device',
  }));
}

function finishChooser(contentsId, deviceId) {
  const state = choosers.get(contentsId);
  if (!state) return;
  const callback = state.callback;
  state.callback = null;
  state.devices = [];
  safeInvoke(callback, deviceId);
  sendTo(state.contents, 'bluetooth-close');
}

function ensureChooserIpc() {
  if (chooserIpcReady) return;
  chooserIpcReady = true;
  ipcMain.on('bluetooth-select', (event, deviceId) => {
    try {
      finishChooser(event.sender.id, typeof deviceId === 'string' ? deviceId : '');
    } catch (error) {
      console.error(error);
    }
  });
  ipcMain.on('bluetooth-cancel', (event) => {
    try {
      finishChooser(event.sender.id, '');
    } catch (error) {
      console.error(error);
    }
  });
}

function attachBluetooth(contents) {
  ensureChooserIpc();
  const state = { callback: null, devices: [], contents };
  choosers.set(contents.id, state);
  const publish = () => sendTo(contents, 'bluetooth-devices', devicePayload(state.devices));
  contents.on('destroyed', () => {
    choosers.delete(contents.id);
    state.callback = null;
  });
  contents.on('dom-ready', () => {
    if (state.callback) publish();
  });
  // The device list stays in this window. A second BrowserWindow is destroyed
  // while the scan is still running, and using it crashes the main process.
  contents.on('select-bluetooth-device', (event, deviceList, callback) => {
    event.preventDefault();
    state.callback = callback;
    state.devices = deviceList || [];
    publish();
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
