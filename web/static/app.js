// API helper
async function api(url, method = 'GET', body = null) {
    const opts = {
        method,
        headers: { 'Content-Type': 'application/json' },
    };
    if (body) opts.body = JSON.stringify(body);

    const res = await fetch(url, opts);
    if (!res.ok) {
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
