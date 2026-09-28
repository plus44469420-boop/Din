# Puffco

Desktop website for the Puffco app. It serves the live site at [https://www.puffco.app/](https://www.puffco.app/) in the browser.

## Run the website

```bash
npm start
```

Open http://127.0.0.1:4173/ in Chrome or Edge.

Bluetooth on the computer has to be on before the page can pair with a device.

## Build a Windows app

```bash
npm install
npm run build:win
```

The portable exe is written to `dist/Puffco-1.0.1-portable.exe`. It opens the same desktop site in a full window, with the same scroll behavior and standard Web Bluetooth.

On Windows the command also writes an NSIS installer. On Linux the installer step needs Wine; the portable exe is still produced.
