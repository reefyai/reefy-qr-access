// API helper
async function api(url, method = 'GET', body = null, showErrors = true) {
    const opts = {
        method,
        headers: { 'Content-Type': 'application/json' },
    };
    if (body) opts.body = JSON.stringify(body);

    const res = await fetch(url, opts);
    if (!res.ok && showErrors) {
        try {
            const err = await res.json();
            showError(err.error || 'Request failed');
        } catch {
            showError('Request failed');
        }
    }
    return res;
}

function showError(msg) {
    openModal(`
        <h2>Error</h2>
        <p>${msg}</p>
        <button onclick="closeModal()" style="margin-top:1rem">OK</button>
    `);
}

// Confirm modal (replaces browser confirm())
function confirmAction(message, onConfirm) {
    openModal(`
        <h2>Confirm</h2>
        <p>${message}</p>
        <div class="confirm-buttons">
            <button class="btn-cancel" onclick="closeModal()">Cancel</button>
            <button class="btn-danger" id="confirm-yes">Yes</button>
        </div>
    `);
    document.getElementById('confirm-yes').onclick = () => {
        closeModal();
        onConfirm();
    };
}

// Modal
function openModal(html) {
    document.getElementById('modal-content').innerHTML = html;
    document.getElementById('modal-overlay').style.display = 'flex';
    const input = document.querySelector('#modal-content input[autofocus]');
    if (input) setTimeout(() => input.focus(), 50);
}

function closeModal() {
    document.getElementById('modal-overlay').style.display = 'none';
}

document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeModal();
});

// Help-popover system: any element with class="help" + data-help opens
// a single anchored popover on click. Click again (or anywhere outside)
// to dismiss. Keep markup minimal - just sprinkle <button class="help"
// data-help="..."> next to a section title and you're done.
(function() {
    let _helpPop = null;
    function close() {
        if (_helpPop) { _helpPop.remove(); _helpPop = null; }
    }
    document.addEventListener('click', function(e) {
        const trigger = e.target.closest('.help');
        if (trigger) {
            e.stopPropagation();
            const wasOpen = _helpPop && _helpPop.dataset.anchor === trigger.dataset.help;
            close();
            if (wasOpen) return;
            const pop = document.createElement('div');
            pop.className = 'help-popover';
            pop.textContent = trigger.dataset.help || '';
            pop.dataset.anchor = trigger.dataset.help || '';
            document.body.appendChild(pop);
            const rect = trigger.getBoundingClientRect();
            const top = rect.bottom + window.scrollY + 6;
            // Clamp horizontally so the popover stays in the viewport.
            const maxLeft = window.innerWidth - pop.offsetWidth - 12;
            pop.style.left = Math.min(rect.left + window.scrollX, maxLeft) + 'px';
            pop.style.top = top + 'px';
            _helpPop = pop;
        } else if (_helpPop && !e.target.closest('.help-popover')) {
            close();
        }
    });
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape') close();
    });
})();
