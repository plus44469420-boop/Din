const { spawnSync } = require('child_process');
const path = require('path');

const builder = path.join(
  __dirname,
  '..',
  'node_modules',
  '.bin',
  process.platform === 'win32' ? 'electron-builder.cmd' : 'electron-builder',
);

function run(args) {
  const result = spawnSync(builder, args, {
    stdio: 'inherit',
    env: { ...process.env, CSC_IDENTITY_AUTO_DISCOVERY: 'false' },
  });
  return result.status == null ? 1 : result.status;
}

const portableStatus = run(['--win', 'portable']);
if (portableStatus !== 0) process.exit(portableStatus);

const nsisStatus = run(['--win', 'nsis']);
if (nsisStatus !== 0) {
  console.error(
    'NSIS installer was not built. On Linux that step needs Wine. The portable exe is in dist/.',
  );
}
