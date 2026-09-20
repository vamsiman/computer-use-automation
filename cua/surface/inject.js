// Build an accessibility tree we can actually act on.
//
// Playwright ships `page.accessibility.snapshot()`, and we deliberately do not
// use it: it is deprecated, it hands back no element handles so there is no way
// to act on what you found, and it cannot cross frame boundaries. Our target
// app puts its entire working area inside an iframe, so that last point alone
// rules it out.
//
// So we walk the DOM ourselves and, for every node we keep, stamp a
// `data-cua-ref` attribute. That attribute is the bridge: the tree we hand
// upwards is pure data, and a ref can be turned back into a real element with
// an ordinary attribute selector when it is time to click.
//
// Roles and names follow ARIA closely enough to be useful, not exhaustively.
// The one rule worth stating out loud is the one about `value`, below.

(function (framePrefix) {
    'use strict';

    const INTERACTIVE = new Set([
        'button', 'link', 'textbox', 'checkbox', 'radio', 'combobox',
        'listbox', 'option', 'menuitem', 'tab', 'slider', 'spinbutton',
    ]);

    // Roles worth a node of their own even with no name. Anything else that is
    // unnamed and uninteresting gets flattened away so the tree stays small
    // enough to put in a prompt.
    const STRUCTURAL = new Set([
        'table', 'row', 'cell', 'columnheader', 'rowheader', 'form',
        'dialog', 'alert', 'heading', 'list', 'listitem', 'iframe', 'img',
        'region', 'navigation', 'main',
    ]);

    let counter = 0;

    // @include describe.js

    function isVisible(el) {
        const style = el.ownerDocument.defaultView.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') {
            return false;
        }
        const rect = el.getBoundingClientRect();
        return rect.width > 0 || rect.height > 0;
    }

    function ownText(el) {
        // Text belonging to this element rather than to its descendants.
        let out = '';
        for (const node of el.childNodes) {
            if (node.nodeType === 3) out += node.nodeValue;
        }
        return squash(out);
    }

    function hasElementChildren(el) {
        return el.children && el.children.length > 0;
    }

    // Skip a <label> that names another control: its text already appears as
    // that control's accessible name, and emitting both just doubles the tree.
    function isRedundantLabel(el) {
        if (el.tagName.toLowerCase() !== 'label') return false;
        const target = el.getAttribute('for');
        return !!(target && el.ownerDocument.getElementById(target));
    }

    function describe(el) {
        const role = roleOf(el);
        if (role === 'none') return null;

        const name = nameOf(el, role);
        const text = ownText(el);

        const keep =
            INTERACTIVE.has(role) ||
            STRUCTURAL.has(role) ||
            (text && !hasElementChildren(el));

        if (!keep) return null;
        if (isRedundantLabel(el)) return null;

        const rect = el.getBoundingClientRect();
        const ref = framePrefix + 'n' + (counter++);
        el.setAttribute('data-cua-ref', ref);

        let effectiveRole = role;
        if (role === 'generic' || role === 'label') effectiveRole = 'text';

        return {
            role: effectiveRole,
            name: name || (effectiveRole === 'text' ? text : ''),
            value: valueOf(el) || null,
            ref: ref,
            box: [
                Math.round(rect.left), Math.round(rect.top),
                Math.round(rect.width), Math.round(rect.height),
            ],
            disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
            // Set for iframe nodes so the caller can splice the child frame's
            // tree in at the right place rather than appending it at the root.
            frameHost: effectiveRole === 'iframe',
            children: [],
        };
    }

    function walk(el, out) {
        if (!isVisible(el)) return;

        const node = describe(el);
        const sink = node ? node.children : out;
        if (node) out.push(node);

        for (const child of el.children) {
            walk(child, sink);
        }
    }

    for (const stale of document.querySelectorAll('[data-cua-ref]')) {
        stale.removeAttribute('data-cua-ref');
    }

    const top = [];
    if (document.body) walk(document.body, top);

    return {
        location: document.location.href,
        title: document.title,
        children: top,
    };
});
