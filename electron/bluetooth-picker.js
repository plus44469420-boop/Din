const path = require('path');
const { BrowserWindow, ipcMain } = require('electron');

const PICKER_HTML = path.join(__dirname, 'picker.html');

const pickerOwners = new Map();
const stateByContents = new Map();
let ipcReady = false;

function settle(state, deviceId) {
  const callback = state.callback;
  const win = state.win;
  state.callback = null;
  state.win = null;
  state.devices = [];
  if (win && !win.isDestroyed()) win.close();
  if (!callback) return;
  try {
    callback(deviceId);
  } catch (error) {
    console.error('Bluetooth chooser callback failed', error);
  }
}

function sendDevices(state) {
  if (!state.win || state.win.isDestroyed()) return;
  if (state.win.webContents.isLoading()) return;
  const devices = state.devices
    .filter((device) => device && device.deviceId)
    .map((device) => ({
      deviceId: device.deviceId,
      deviceName: device.deviceName || 'Unknown device',
    }));
  state.win.webContents.send('bluetooth-devices', devices);
}

function ownerState(senderId) {
  const ownerId = pickerOwners.get(senderId);
  if (ownerId == null) return null;
  return stateByContents.get(ownerId) || null;
}

function ensureIpc() {
  if (ipcReady) return;
  ipcReady = true;
  ipcMain.on('bluetooth-select', (event, deviceId) => {
    const state = ownerState(event.sender.id);
    if (!state) return;
    settle(state, typeof deviceId === 'string' ? deviceId : '');
  });
  ipcMain.on('bluetooth-cancel', (event) => {
    const state = ownerState(event.sender.id);
    if (!state) return;
    settle(state, '');
  });
}

function openPicker(parentContents, state) {
  if (state.win && !state.win.isDestroyed()) {
    sendDevices(state);
    state.win.focus();
    return;
  }

  const parent = BrowserWindow.fromWebContents(parentContents);
  const win = new BrowserWindow({
    parent: parent || undefined,
    modal: Boolean(parent),
    width: 360,
    height: 480,
    resizable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    title: 'Connect a device',
    backgroundColor: '#141414',
    autoHideMenuBar: true,
    show: false,
    icon: path.join(__dirname, '..', 'assets', 'icon.png'),
    webPreferences: {
      preload: path.join(__dirname, 'picker-preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
    },
  });

  const pickerContentsId = win.webContents.id;
  pickerOwners.set(pickerContentsId, parentContents.id);
  state.win = win;
  win.setMenuBarVisibility(false);
  win.once('ready-to-show', () => {
    if (!win.isDestroyed()) win.show();
  });
  win.webContents.on('did-finish-load', () => sendDevices(state));
  win.on('closed', () => {
    pickerOwners.delete(pickerContentsId);
    if (state.win && state.win !== win) return;
    state.win = null;
    if (state.callback) settle(state, '');
  });
  win.loadFile(PICKER_HTML);
}

function attachBluetoothPicker(contents) {
  ensureIpc();
  contents.on('select-bluetooth-device', (event, deviceList, callback) => {
    event.preventDefault();
    let state = stateByContents.get(contents.id);
    if (!state) {
      state = { callback: null, devices: [], win: null };
      stateByContents.set(contents.id, state);
    }
    state.callback = callback;
    state.devices = Array.isArray(deviceList) ? deviceList : [];
    openPicker(contents, state);
  });

  contents.once('destroyed', () => {
    const state = stateByContents.get(contents.id);
    stateByContents.delete(contents.id);
    if (!state) return;
    state.callback = null;
    if (state.win && !state.win.isDestroyed()) state.win.close();
  });
}

const PAIRING_HTML = path.join(__dirname, 'pairing.html');
const pendingPairing = new Map();
let pairingIpcReady = false;

function ensurePairingIpc() {
  if (pairingIpcReady) return;
  pairingIpcReady = true;
  ipcMain.on('pairing-submit', (event, response) => {
    const resolve = pendingPairing.get(event.sender.id);
    if (!resolve) return;
    pendingPairing.delete(event.sender.id);
    const confirmed = Boolean(response && response.confirmed);
    const pin = response && typeof response.pin === 'string' ? response.pin : '';
    resolve(confirmed ? { confirmed: true, pin } : { confirmed: false });
    const win = BrowserWindow.fromWebContents(event.sender);
    if (win && !win.isDestroyed()) win.close();
  });
}

function promptBluetoothPairing(details) {
  ensurePairingIpc();
  return new Promise((resolve) => {
    const parent = BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0];
    const win = new BrowserWindow({
      parent: parent || undefined,
      modal: Boolean(parent),
      width: 360,
      height: details.pairingKind === 'providePin' ? 280 : 230,
      resizable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      title: 'Bluetooth pairing',
      backgroundColor: '#141414',
      autoHideMenuBar: true,
      show: false,
      icon: path.join(__dirname, '..', 'assets', 'icon.png'),
      webPreferences: {
        preload: path.join(__dirname, 'pairing-preload.js'),
        nodeIntegration: false,
        contextIsolation: true,
        sandbox: true,
      },
    });
    const contentsId = win.webContents.id;
    let settled = false;
    const finish = (response) => {
      if (settled) return;
      settled = true;
      pendingPairing.delete(contentsId);
      resolve(response);
    };
    pendingPairing.set(contentsId, (response) => finish(response));
    win.setMenuBarVisibility(false);
    win.once('ready-to-show', () => {
      if (!win.isDestroyed()) win.show();
    });
    win.webContents.on('did-finish-load', () => {
      if (win.isDestroyed()) return;
      win.webContents.send('pairing-details', {
        pairingKind: details.pairingKind,
        pin: details.pin || '',
      });
    });
    win.on('closed', () => finish({ confirmed: false }));
    win.loadFile(PAIRING_HTML);
  });
}

function attachBluetoothPairing(ses) {
  ses.setBluetoothPairingHandler((details, callback) => {
    promptBluetoothPairing(details)
      .then((response) => callback(response))
      .catch((error) => {
        console.error('Bluetooth pairing prompt failed', error);
        callback({ confirmed: false });
      });
  });
}

module.exports = { attachBluetoothPicker, attachBluetoothPairing };
