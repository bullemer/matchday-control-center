module.exports = {
  testDir: './tests', testMatch: '**/*.spec.js',
  use: {baseURL: 'http://127.0.0.1:25000', httpCredentials: {username:'operator',password:'browser-test'}},
  webServer: {command:'venv/bin/python tests/browser_server.py', url:'http://127.0.0.1:25000', reuseExistingServer:false},
};
