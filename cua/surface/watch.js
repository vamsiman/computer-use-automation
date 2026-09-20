// What the person did while they held the session.
//
// A handoff is the one moment the automation is not the thing acting, and it
// would be the one gap in the evidence if nobody wrote it down. So for the
// duration of the handoff every click, change and edit in the live window is
// recorded in the same vocabulary the automation uses -- role, accessible
// name, value -- by literally the same code, included below from
// `describe.js`.
//
// Two details are load-bearing:
//
// 1. The log lives in `sessionStorage`, not in a variable. The first thing a
//    person usually does is press the button that navigates, which would
//    throw away anything held in the page. Storage survives navigation within
//    the tab, so the record of the click outlives the page it happened on.
//
// 2. Listeners are installed at document start and are inert unless the
//    watch flag is set. Playwright cannot un-inject an init script, so the
//    script is always there and the *flag* is what turns on. Nothing is
//    recorded while the automation is driving.

(function () {
    'use strict';

    const FLAG = '__cua_watch';
    const LOG = '__cua_watch_log';

    // A person cannot generate an unbounded number of meaningful actions in
    // one handoff, so a cap here costs nothing and stops a stuck key from
    // filling the tab's storage quota.
    const MAX_RECORDS = 200;

    // @include describe.js

    function store() {
        try {
            return window.sessionStorage;
        } catch (err) {
            // Storage can be blocked outright. Losing the log is bad; taking
            // the live session down during a human handoff is worse.
            return null;
        }
    }

    function active() {
        const s = store();
        return !!s && s.getItem(FLAG) === '1';
    }

    function read() {
        const s = store();
        if (!s) return [];
        try {
            return JSON.parse(s.getItem(LOG) || '[]');
        } catch (err) {
            return [];
        }
    }

    function write(records) {
        const s = store();
        if (!s) return;
        try {
            s.setItem(LOG, JSON.stringify(records.slice(-MAX_RECORDS)));
        } catch (err) {
            /* quota, private mode: drop rather than break the handoff */
        }
    }

    function describeTarget(el, kind) {
        const role = roleOf(el);
        const name = nameOf(el, role);
        return {
            id: Date.now().toString(36) + '-' +
                Math.random().toString(36).slice(2, 8),
            at: new Date().toISOString(),
            kind: kind,
            role: role === 'generic' || role === 'label' ? 'text' : role,
            name: name,
            // `valueOf` is where password masking already lives, so a human
            // typing into a password field is masked by the same rule that
            // masks it when the automation reads the page.
            value: valueOf(el) || null,
            // Null once the page has re-rendered since the last observation.
            // Recorded when present because it ties the human's click to a
            // node in the tree the automation last saw.
            ref: el.getAttribute ? el.getAttribute('data-cua-ref') : null,
            location: window.location.href,
        };
    }

    function note(event, kind) {
        if (!active()) return;
        const el = event.target;
        if (!el || el.nodeType !== 1) return;

        const record = describeTarget(el, kind);
        const records = read();

        // Typing fires an `input` event per keystroke. Recording twelve of
        // them says nothing that the last one does not, so consecutive edits
        // to the same control collapse into their final value.
        const last = records[records.length - 1];
        if (last && last.kind === 'input' && kind === 'input' &&
            last.ref === record.ref && last.ref !== null) {
            records[records.length - 1] = record;
        } else {
            records.push(record);
        }
        write(records);
    }

    if (!window.__cuaWatchInstalled) {
        window.__cuaWatchInstalled = true;
        // Capture phase, so a control that stops propagation is still seen.
        document.addEventListener('click', (e) => note(e, 'click'), true);
        document.addEventListener('change', (e) => note(e, 'change'), true);
        document.addEventListener('input', (e) => note(e, 'input'), true);
    }

    window.__cuaWatch = {
        start: function () {
            const s = store();
            if (s) s.setItem(FLAG, '1');
            return true;
        },
        stop: function () {
            const s = store();
            if (s) s.removeItem(FLAG);
            return true;
        },
        drain: function () {
            const records = read();
            const s = store();
            if (s) s.removeItem(LOG);
            return records;
        },
        active: active,
    };
})();
