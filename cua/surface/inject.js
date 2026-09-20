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

    function squash(text) {
        return (text || '').replace(/\s+/g, ' ').trim().slice(0, 160);
    }

    function roleOf(el) {
        const explicit = el.getAttribute('role');
        if (explicit) return explicit.trim().toLowerCase();

        const tag = el.tagName.toLowerCase();
        switch (tag) {
            case 'a':
                return el.hasAttribute('href') ? 'link' : 'generic';
            case 'button':
                return 'button';
            case 'select':
                return el.multiple ? 'listbox' : 'combobox';
            case 'option':
                return 'option';
            case 'textarea':
                return 'textbox';
            case 'img':
                return 'img';
            case 'form':
                return 'form';
            case 'table':
                return 'table';
            case 'tr':
                return 'row';
            case 'td':
                return 'cell';
            case 'th':
                return 'columnheader';
            case 'iframe':
            case 'frame':
                return 'iframe';
            case 'h1': case 'h2': case 'h3':
            case 'h4': case 'h5': case 'h6':
                return 'heading';
            case 'ul': case 'ol':
                return 'list';
            case 'li':
                return 'listitem';
            case 'label':
                return 'label';
            case 'input': {
                const type = (el.getAttribute('type') || 'text').toLowerCase();
                if (type === 'submit' || type === 'button' ||
                    type === 'reset' || type === 'image') return 'button';
                if (type === 'checkbox') return 'checkbox';
                if (type === 'radio') return 'radio';
                if (type === 'hidden') return 'none';
                return 'textbox';
            }
            default:
                return 'generic';
        }
    }

    function labelTextFor(el) {
        if (el.id) {
            const bound = el.ownerDocument.querySelector(
                'label[for="' + CSS.escape(el.id) + '"]');
            if (bound) return squash(bound.textContent);
        }
        const ancestor = el.closest ? el.closest('label') : null;
        return ancestor ? squash(ancestor.textContent) : '';
    }

    function nameOf(el, role) {
        const labelledBy = el.getAttribute('aria-labelledby');
        if (labelledBy) {
            const parts = labelledBy.split(/\s+/)
                .map((id) => el.ownerDocument.getElementById(id))
                .filter(Boolean)
                .map((n) => squash(n.textContent));
            if (parts.length) return squash(parts.join(' '));
        }

        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel) return squash(ariaLabel);

        if (el.tagName.toLowerCase() === 'input') {
            const type = (el.getAttribute('type') || 'text').toLowerCase();

            // The rule that matters. For a push button, `value` IS the
            // accessible name -- <input type="submit" value="Search"> is the
            // Search button. For a text field it is emphatically not: value is
            // what the user typed, not what the control is called.
            //
            // Getting this wrong is subtly destructive. Our search field
            // re-renders carrying the previous query, so treating value as a
            // name would make an unnamed field acquire a name on the second
            // render, and role+name targeting would start matching it
            // sometimes -- which is worse than never matching at all.
            if (type === 'submit' || type === 'button' ||
                type === 'reset' || type === 'image') {
                const v = el.getAttribute('value');
                if (v) return squash(v);
                if (type === 'image') return squash(el.getAttribute('alt'));
            }
        }

        const bound = labelTextFor(el);
        if (bound) return bound;

        const alt = el.getAttribute('alt');
        if (alt) return squash(alt);

        const title = el.getAttribute('title');
        if (title) return squash(title);

        // Name from content, for roles where that is the convention.
        if (role === 'button' || role === 'link' || role === 'heading' ||
            role === 'cell' || role === 'columnheader' || role === 'option' ||
            role === 'listitem' || role === 'label' || role === 'alert' ||
            role === 'text') {
            return squash(el.textContent);
        }

        return '';
    }

    function valueOf(el) {
        const tag = el.tagName.toLowerCase();
        if (tag === 'select') {
            const opt = el.selectedOptions && el.selectedOptions[0];
            return opt ? squash(opt.textContent) : '';
        }
        if (tag === 'textarea') return squash(el.value);
        if (tag === 'input') {
            const type = (el.getAttribute('type') || 'text').toLowerCase();
            if (type === 'checkbox' || type === 'radio') {
                return el.checked ? 'checked' : 'unchecked';
            }
            if (type === 'password') return el.value ? '********' : '';
            if (type === 'submit' || type === 'button' || type === 'reset') {
                return '';
            }
            return squash(el.value);
        }
        return '';
    }

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
