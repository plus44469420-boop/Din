const { ipcRenderer } = require('electron');

const STYLE = `
#puffco-bt-overlay {
  position: fixed;
  inset: 0;
  z-index: 2147483647;
  background: rgba(0, 0, 0, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: "Segoe UI", sans-serif;
}
#puffco-bt-overlay .panel {
  width: min(420px, calc(100vw - 32px));
  max-height: min(560px, calc(100vh - 32px));
  box-sizing: border-box;
  background: #141414;
  color: #f2efe9;
  border-radius: 16px;
  padding: 20px 18px 16px;
  display: flex;
  flex-direction: column;
}
#puffco-bt-overlay h1 { margin: 0 0 8px; font-size: 18px; font-weight: 600; }
#puffco-bt-overlay p { margin: 0 0 16px; color: #b9b3aa; font-size: 13px; }
#puffco-bt-overlay ul { list-style: none; margin: 0; padding: 0; overflow: auto; }
#puffco-bt-overlay button { font: inherit; cursor: pointer; border-radius: 10px; }
#puffco-bt-overlay button.device {
  display: block;
  width: 100%;
  text-align: left;
  background: #242424;
  color: inherit;
  border: 0;
  padding: 12px 14px;
  margin-bottom: 8px;
}
#puffco-bt-overlay button.cancel {
  margin-top: 12px;
  width: 100%;
  background: transparent;
  color: inherit;
  border: 1px solid #3a3a3a;
  padding: 10px 12px;
}
#puffco-bt-overlay .empty { color: #8e887f; padding: 8px 2px; }
`;

function render(devices) {
  let root = document.getElementById('puffco-bt-overlay');
  if (!root) {
    const style = document.createElement('style');
    style.id = 'puffco-bt-style';
    style.textContent = STYLE;
    document.documentElement.appendChild(style);
    root = document.createElement('div');
    root.id = 'puffco-bt-overlay';
    const panel = document.createElement('div');
    panel.className = 'panel';
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-label', 'Connect a device');
    const title = document.createElement('h1');
    title.textContent = 'Connect a device';
    const copy = document.createElement('p');
    copy.textContent = 'Select your Puffco. It needs to be powered on and nearby.';
    const list = document.createElement('ul');
    list.id = 'puffco-bt-list';
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'cancel';
    cancel.id = 'puffco-bt-cancel';
    cancel.textContent = 'Cancel';
    cancel.addEventListener('click', () => ipcRenderer.send('bluetooth-cancel'));
    panel.append(title, copy, list, cancel);
    root.appendChild(panel);
    document.documentElement.appendChild(root);
  }
  const list = root.querySelector('#puffco-bt-list');
  list.replaceChildren();
  if (!devices.length) {
    const empty = document.createElement('li');
    empty.className = 'empty';
    empty.textContent = 'Searching for nearby devices…';
    list.appendChild(empty);
    return;
  }
  for (const device of devices) {
    const item = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'device';
    button.textContent = device.deviceName || 'Unknown device';
    button.addEventListener('click', () => ipcRenderer.send('bluetooth-select', device.deviceId));
    item.appendChild(button);
    list.appendChild(item);
  }
}

ipcRenderer.on('bluetooth-devices', (_event, devices) => {
  render(Array.isArray(devices) ? devices : []);
});

ipcRenderer.on('bluetooth-close', () => {
  document.getElementById('puffco-bt-overlay')?.remove();
});
