const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('pairing', {
  onDetails(callback) {
    ipcRenderer.on('pairing-details', (_event, details) => callback(details || {}));
  },
  submit(response) {
    ipcRenderer.send('pairing-submit', response);
  },
});
