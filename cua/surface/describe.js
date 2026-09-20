// How a control is described: its role, its accessible name, its value.
//
// Shared on purpose. `inject.js` uses these when the automation looks at the
// page; `watch.js` uses the same ones when it watches a person work. If the
// two disagreed, a handoff would be recorded in a different vocabulary from
// the run it interrupted, and the log of what the human did could not be read
// against the steps the automation took.
//
// Included textually rather than imported: these run inside `frame.evaluate`,
// where there is no module system and nothing to import from.

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
