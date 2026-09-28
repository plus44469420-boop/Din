const list = document.getElementById('list');
document.getElementById('cancel').addEventListener('click', () => window.picker.cancel());
window.picker.onDevices((devices) => {
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
    button.addEventListener('click', () => window.picker.select(device.deviceId));
    item.appendChild(button);
    list.appendChild(item);
  }
});
