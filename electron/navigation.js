const path = require('path');
const { BrowserWindow, shell } = require('electron');
const { PARTITION } = require('./constants');
const { isInAppFlow } = require('./link-policy');

const ICON = path.join(__dirname, '..', 'assets', 'icon.png');

function popupOptions(contents) {
  const parent = BrowserWindow.fromWebContents(contents);
  return {
    parent: parent || undefined,
    width: 420,
    height: 760,
    minWidth: 320,
    minHeight: 480,
    title: 'Puffco',
    backgroundColor: '#141414',
    autoHideMenuBar: true,
    icon: ICON,
    webPreferences: {
      partition: PARTITION,
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      backgroundThrottling: false,
      spellcheck: false,
    },
  };
}

function openExternal(url) {
  shell.openExternal(url).catch((error) => {
    console.error('Failed to open external link', url, error);
  });
}

function isDevtools(url) {
  return url.startsWith('devtools://') || url.startsWith('chrome-devtools://');
}

function attachLinkPolicy(contents) {
  contents.setWindowOpenHandler(({ url }) => {
    if (isDevtools(url)) return { action: 'allow' };
    if (isInAppFlow(url)) {
      return {
        action: 'allow',
        overrideBrowserWindowOptions: popupOptions(contents),
      };
    }
    openExternal(url);
    return { action: 'deny' };
  });

  const guardNavigation = (event, url) => {
    if (!url || isDevtools(url)) return;
    if (isInAppFlow(url)) return;
    event.preventDefault();
    openExternal(url);
  };

  contents.on('will-navigate', guardNavigation);
  contents.on('will-redirect', guardNavigation);
}

module.exports = { attachLinkPolicy };
