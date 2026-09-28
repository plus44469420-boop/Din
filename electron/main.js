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

function ensurePickerIpc() {
  if (pickerIpcReady) return;
  pickerIpcReady = true;
  const finish = (senderId, deviceId) => {
    const state = pickerOwners.get(senderId);
    if (!state) return;
    const callback = state.callback;
    state.callback = null;
    if (state.win && !state.win.isDestroyed()) state.win.close();
    if (callback) callback(deviceId);
  };
  ipcMain.on('bluetooth-select', (event, deviceId) => {
    finish(event.sender.id, typeof deviceId === 'string' ? deviceId : '');
  });
  ipcMain.on('bluetooth-cancel', (event) => finish(event.sender.id, ''));
}

function attachBluetooth(contents) {
  ensurePickerIpc();
  const state = { callback: null, win: null, devices: [] };
  const send = () => {
    if (!state.win || state.win.isDestroyed() || state.win.webContents.isLoading()) return;
    state.win.webContents.send(
      'bluetooth-devices',
      state.devices.map((device) => ({
        deviceId: device.deviceId,
        deviceName: device.deviceName || 'Unknown device',
      })),
    );
  };
  contents.on('select-bluetooth-device', (event, deviceList, callback) => {
    event.preventDefault();
    state.callback = callback;
    state.devices = deviceList || [];
    if (state.win && !state.win.isDestroyed()) {
      send();
      state.win.focus();
      return;
    }
    const parent = BrowserWindow.fromWebContents(contents);
    const win = new BrowserWindow({
      parent: parent || undefined,
      modal: Boolean(parent),
      width: 360,
      height: 480,
      resizable: false,
      title: 'Connect a device',
      backgroundColor: '#141414',
      autoHideMenuBar: true,
      show: false,
      webPreferences: {
        preload: path.join(__dirname, 'picker-preload.js'),
        nodeIntegration: false,
        contextIsolation: true,
        sandbox: true,
      },
    });
    pickerOwners.set(win.webContents.id, state);
    state.win = win;
    win.once('ready-to-show', () => win.show());
    win.webContents.on('did-finish-load', send);
    win.on('closed', () => {
      pickerOwners.delete(win.webContents.id);
      if (state.callback) {
        const callback = state.callback;
        state.callback = null;
        callback('');
      }
      state.win = null;
    });
    win.loadFile(path.join(__dirname, 'picker.html'));
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
      if (win.isMinimized()) win.restore();
      win.focus();
    });
  });

  app.on('window-all-closed', () => {
    app.quit();
  });
}
