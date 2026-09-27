const fs = require('fs');
const path = require('path');
const { app, screen } = require('electron');
const { DEFAULT_CONTENT_SIZE } = require('./constants');

function stateFile() {
  return path.join(app.getPath('userData'), 'window-bounds.json');
}

function onScreen(bounds) {
  if (!Number.isFinite(bounds.x) || !Number.isFinite(bounds.y)) return false;
  const area = screen.getDisplayMatching(bounds).workArea;
  return (
    bounds.x < area.x + area.width &&
    bounds.x + bounds.width > area.x &&
    bounds.y < area.y + area.height &&
    bounds.y + Math.min(bounds.height, 80) > area.y
  );
}

function loadWindowState() {
  try {
    const data = JSON.parse(fs.readFileSync(stateFile(), 'utf8'));
    if (!Number.isFinite(data.width) || !Number.isFinite(data.height)) return null;
    if (data.width < 320 || data.height < 480) return null;
    if (!onScreen(data)) {
      return { width: data.width, height: data.height, maximized: false };
    }
    return data;
  } catch {
    return null;
  }
}

function writeState(state) {
  try {
    fs.mkdirSync(path.dirname(stateFile()), { recursive: true });
    fs.writeFileSync(stateFile(), JSON.stringify(state));
  } catch (error) {
    console.error('Failed to save window bounds', error);
  }
}

function snapshot(win) {
  const bounds = win.isMaximized() ? win.getNormalBounds() : win.getBounds();
  return {
    x: bounds.x,
    y: bounds.y,
    width: bounds.width,
    height: bounds.height,
    maximized: win.isMaximized(),
  };
}

function trackWindowState(win) {
  let timer = null;
  const save = () => writeState(snapshot(win));
  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(save, 250);
  };
  win.on('resize', schedule);
  win.on('move', schedule);
  win.on('close', () => {
    clearTimeout(timer);
    if (!win.isDestroyed()) save();
  });
}

module.exports = { loadWindowState, trackWindowState, DEFAULT_CONTENT_SIZE };
