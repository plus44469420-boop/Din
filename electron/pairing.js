const message = document.getElementById('message');
const pinInput = document.getElementById('pin');
const pair = document.getElementById('pair');
const cancel = document.getElementById('cancel');

let kind = 'confirm';

window.pairing.onDetails((details) => {
  kind = details.pairingKind || 'confirm';
  if (kind === 'confirmPin') {
    message.textContent = `Confirm this PIN matches the device: ${details.pin || ''}`;
  } else if (kind === 'providePin') {
    message.textContent = 'Enter the PIN shown on the device.';
    pinInput.hidden = false;
    pinInput.focus();
  } else {
    message.textContent = 'Confirm pairing with this device.';
  }
});

pair.addEventListener('click', () => {
  window.pairing.submit({
    confirmed: true,
    pin: kind === 'providePin' ? pinInput.value : '',
  });
});

cancel.addEventListener('click', () => {
  window.pairing.submit({ confirmed: false });
});
