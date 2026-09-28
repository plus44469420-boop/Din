const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('picker', {
  onDevices(callback) {
    ipcRenderer.on('bluetooth-devices', (_event, devices) => {
      callback(Array.isArray(devices) ? devices : []);
    });
  },
  select(deviceId) {
    ipcRenderer.send('bluetooth-select', deviceId);
  },
  cancel() {
    ipcRenderer.send('bluetooth-cancel');
  },
});
