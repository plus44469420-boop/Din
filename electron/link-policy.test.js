const test = require('node:test');
const assert = require('node:assert/strict');
const { isInAppFlow } = require('./link-policy');

test('keeps the Puffco app and its auth flow inside the window', () => {
  const inside = [
    'https://www.puffco.app/',
    'https://www.puffco.app/home',
    'https://puffco.app/',
    'https://api.puffco.app/api/users',
    'https://assets.puffco.app/cleaning/PeakPro_Cleaning_Instructions.pdf',
    'https://puffco-prod.s3-us-west-2.amazonaws.com/welcome/opal.mp4',
    'https://accounts.google.com/gsi/client',
    'https://accounts.google.com/o/oauth2/v2/auth',
    'https://appleid.apple.com/auth/authorize?client_id=x',
    'https://appleid.cdn-apple.com/appleauth/static/jsapi/appleid/1/en_US/appleid.auth.js',
    'https://securetoken.googleapis.com/v1/token',
    'https://www.googleapis.com/identitytoolkit/v3/relyingparty',
    'https://www.google.com/recaptcha/api2/anchor',
    'about:blank',
  ];
  for (const url of inside) {
    assert.equal(isInAppFlow(url), true, url);
  }
});

test('sends links that leave the app to the system browser', () => {
  const outside = [
    'https://www.puffco.com/',
    'https://www.puffco.com/pages/path-browser-ios-instructions',
    'https://puffcorp.formstack.com/forms/customer_support_intake',
    'https://apps.apple.com/app/id1519521388',
    'https://play.google.com/store/apps/details?id=com.puffco.android',
    'https://www.google.com/maps/dir/?api=1&destination=1,2',
    'https://maps.google.com/',
    'mailto:support@example.com',
    'https://evilpuffco.app.attacker.com/',
    'https://www.puffco.app.evil.com/',
    'https://notpuffco.app/',
    'https://accounts.google.com.evil.com/',
    'javascript:alert(1)',
  ];
  for (const url of outside) {
    assert.equal(isInAppFlow(url), false, url);
  }
});
