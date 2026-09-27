# Puffco

Desktop window for the Puffco app. It loads [https://www.puffco.app/](https://www.puffco.app/).

## Run

```bash
npm install
npm start
```

`npm run dev` does the same and opens developer tools.

Bluetooth on the computer has to be on before the page can pair with a device.

## Build a Windows installer

```bash
npm install
npm run build:win
```

The NSIS installer and portable exe are written to `dist/`.
