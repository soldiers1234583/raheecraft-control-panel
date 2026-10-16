/**
 * Executes the real `backend/static/js/dashboard.js` against a stub DOM and
 * checks what `checkLogin()` does.
 *
 * Why this exists: the dashboard used to decide "am I logged in?" from
 * `document.cookie`. The session cookie is HttpOnly, so that check can never
 * succeed — a freshly signed-in user was bounced straight back to /login
 * ("logged in, then kicked"). A source-level assertion would not prove the
 * behaviour, so this loads the actual file and calls the actual function.
 *
 * Prints one JSON object and exits non-zero if any expectation fails.
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const DASHBOARD_JS = path.join(__dirname, '..', '..', 'static', 'js', 'dashboard.js');
const source = fs.readFileSync(DASHBOARD_JS, 'utf8');

/**
 * Run checkLogin() with a given server-rendered flag and an empty
 * (script-invisible) cookie jar.
 */
function runCheckLogin(flag) {
    const usernameEl = { textContent: 'User' };
    const document = {
        body: { getAttribute: (name) => (name === 'data-logged-in' ? flag : null) },
        getElementById: (id) => (id === 'username' ? usernameEl : null),
        querySelectorAll: () => [],
        addEventListener: () => {},
        // This is what an HttpOnly session cookie looks like to JavaScript.
        cookie: '',
    };
    const window = { location: { href: 'https://panel.invalid/' } };
    const sandbox = {
        document,
        window,
        console,
        fetch: () => Promise.reject(new Error('no network in this harness')),
        setTimeout: () => 0,
        setInterval: () => 0,
        clearTimeout: () => 0,
        clearInterval: () => 0,
        localStorage: { getItem: () => null, setItem: () => {} },
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox);
    vm.runInContext(
        'globalThis.__result = (function () {' +
        '  Dashboard.checkLogin();' +
        '  return { href: window.location.href,' +
        '           username: (document.getElementById("username") || {}).textContent };' +
        '})();',
        sandbox);
    const result = sandbox.__result;
    return {
        flag: flag === null ? null : flag,
        href: result.href,
        redirected: result.href === '/login',
        username: result.username,
    };
}

const cases = {
    // A valid session: flag says logged in, cookie jar is empty. Must NOT bounce.
    logged_in_flag_1: runCheckLogin('1'),
    // No session: the server says so, so the page sends the visitor to the form.
    anonymous_flag_0: runCheckLogin('0'),
    // Cached HTML without the attribute: fail open and let the API's 401 redirect.
    missing_flag: runCheckLogin(null),
};

const failures = [];
if (cases.logged_in_flag_1.redirected) {
    failures.push('logged_in_flag_1 redirected to /login (the reported bug)');
}
if (!cases.anonymous_flag_0.redirected) {
    failures.push('anonymous_flag_0 did not redirect to /login');
}
if (cases.missing_flag.redirected) {
    failures.push('missing_flag redirected on its own instead of letting the API decide');
}

console.log(JSON.stringify(cases));
if (failures.length) {
    console.error('FAILED: ' + failures.join('; '));
    process.exit(1);
}
