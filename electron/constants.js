const PARTITION = 'persist:puffco';
const START_URL = 'https://www.puffco.app/';

// Current iPhone WKWebView user agent (iOS 26). The live app sets
// IS_IOS_WEB when navigator.userAgent contains "iPhone", and
// IS_USING_DESKTOP_WEB when it contains neither "iPhone" nor "Android".
const IPHONE_WEBVIEW_UA =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 26_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148';

// CSS viewport of a current large iPhone (iPhone 16 Pro class).
const DEFAULT_CONTENT_SIZE = { width: 402, height: 874 };

module.exports = {
  PARTITION,
  START_URL,
  IPHONE_WEBVIEW_UA,
  DEFAULT_CONTENT_SIZE,
};
