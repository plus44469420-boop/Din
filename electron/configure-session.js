// Deny only permissions that are not part of the Puffco page.
// Bluetooth, notifications, storage, and login-related checks stay allowed.
const DENIED_PERMISSIONS = new Set([
  'serial',
  'hid',
  'usb',
  'fileSystem',
  'display-capture',
  'midiSysex',
  'mediaKeySystem',
]);

function permissionAllowed(permission) {
  return !DENIED_PERMISSIONS.has(permission);
}

function configureSession(ses, userAgent) {
  ses.setUserAgent(userAgent);
  ses.setPermissionRequestHandler((_webContents, permission, callback) => {
    callback(permissionAllowed(permission));
  });
  ses.setPermissionCheckHandler((_webContents, permission) => {
    return permissionAllowed(permission);
  });
  ses.setDevicePermissionHandler((details) => details.deviceType === 'bluetooth');
  if (typeof ses.setSpellCheckerEnabled === 'function') {
    ses.setSpellCheckerEnabled(false);
  }
}

module.exports = { configureSession, permissionAllowed };
