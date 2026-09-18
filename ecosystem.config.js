const os = require('os');
const path = require('path');

module.exports = {
  apps: [{
    name: 'slop-token-refresh',
    script: process.env.SLOP_BIN || path.join(os.homedir(), '.local/bin/slop'),
    args: ['refresh-tokens'],
    interpreter: 'none',
    cwd: os.homedir(),
    instances: 1,
    exec_mode: 'fork',
    autorestart: true,
    restart_delay: 10000,
    kill_timeout: 120000,
    max_memory_restart: '256M',
    time: true,
    env: {
      PYTHONUNBUFFERED: '1',
      CODEX_HOME: process.env.CODEX_HOME || path.join(os.homedir(), '.codex'),
      PATH: process.env.PATH,
    },
  }],
};
