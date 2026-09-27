const list = document.getElementById('list');
const cancel = document.getElementById('cancel');

function render(devices) {
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
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = device.deviceName;
    const id = document.createElement('span');
    id.className = 'id';
    id.textContent = device.deviceId;
    button.append(name, id);
    button.addEventListener('click', () => window.picker.select(device.deviceId));
    item.appendChild(button);
    list.appendChild(item);
  }
}

window.picker.onDevices(render);
cancel.addEventListener('click', () => window.picker.cancel());
