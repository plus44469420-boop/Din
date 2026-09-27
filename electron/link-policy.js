const { START_URL } = require('./constants');

const APP_SUFFIX = 'puffco.app';

// Hosts the page uses for sign-in. These stay in the app window.
const AUTH_SUFFIXES = [
  'accounts.google.com',
  'accounts.youtube.com',
  'appleid.apple.com',
  'appleid.cdn-apple.com',
  'googleapis.com',
  'gstatic.com',
  'googleusercontent.com',
  'firebaseapp.com',
  'web.app',
];

function hostIs(hostname, suffix) {
  return hostname === suffix || hostname.endsWith(`.${suffix}`);
}

function isPuffcoMediaHost(hostname) {
  return hostname.includes('puffco') && hostname.endsWith('.amazonaws.com');
}

function isInAppFlow(urlString) {
  let url;
  try {
    url = new URL(urlString);
  } catch {
    return false;
  }

  if (url.protocol === 'about:') return true;
  if (url.protocol !== 'https:' && url.protocol !== 'http:') return false;

  const host = url.hostname;
  if (hostIs(host, APP_SUFFIX)) return true;
  if (isPuffcoMediaHost(host)) return true;
  if (AUTH_SUFFIXES.some((suffix) => hostIs(host, suffix))) return true;

  // reCAPTCHA and Google Identity iframes that navigate the frame.
  if (
    (host === 'www.google.com' || host === 'google.com') &&
    (url.pathname.startsWith('/recaptcha') || url.pathname.startsWith('/a/'))
  ) {
    return true;
  }

  return false;
}

module.exports = { isInAppFlow, START_URL };
