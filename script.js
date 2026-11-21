const authModal = document.querySelector('.auth-modal');
const loginLink = document.querySelector('.login-link');
const registerLink = document.querySelector('.register-link');
const loginBtnModal = document.querySelector('.login-btn-modal');
const closeBtnModal = document.querySelector('.close-btn-modal');
const loginForm = document.querySelector('.loginForm');
const registerForm = document.querySelector('.registerForm');

const feedbackOverlay = document.getElementById('authFeedback');
const feedbackCard = document.getElementById('feedbackCard');
const feedbackIcon = document.getElementById('feedbackIcon');
const feedbackTitle = document.getElementById('feedbackTitle');
const feedbackMessage = document.getElementById('feedbackMessage');
const feedbackOk = document.getElementById('feedbackOk');
let feedbackAction = null;

registerLink.addEventListener('click', (event) => {
    event.preventDefault();
    authModal.classList.add('slide');
});
loginLink.addEventListener('click', (event) => {
    event.preventDefault();
    authModal.classList.remove('slide');
});

loginBtnModal.addEventListener('click', () => authModal.classList.add('show'));
closeBtnModal.addEventListener('click', () => authModal.classList.remove('show', 'slide'));

// A pop-up that clearly reports whether authentication succeeded or failed.
function showAuthFeedback(type, title, message, action = null) {
    feedbackCard.className = `feedback-card ${type}`;
    feedbackIcon.className = type === 'success' ? 'bxf bx-check-circle' : 'bxf bx-x-circle';
    feedbackTitle.textContent = title;
    feedbackMessage.textContent = message;
    feedbackAction = action;
    feedbackOverlay.hidden = false;
    feedbackOk.focus();
}

function closeAuthFeedback() {
    if (feedbackOverlay.hidden) return;
    feedbackOverlay.hidden = true;
    const action = feedbackAction;
    feedbackAction = null;
    if (action) action();
}

feedbackOk.addEventListener('click', closeAuthFeedback);
feedbackOverlay.addEventListener('click', (event) => {
    if (event.target === feedbackOverlay) closeAuthFeedback();
});
document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !feedbackOverlay.hidden) closeAuthFeedback();
});

// Send form data to the Python server, then report the result in the pop-up.
async function submitAuth(form, endpoint, config) {
    const button = form.querySelector('button[type="submit"]');
    const data = Object.fromEntries(new FormData(form).entries());

    button.disabled = true;

    try {
        const response = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
            credentials: 'same-origin'
        });
        const result = await response.json();

        if (!response.ok) {
            showAuthFeedback('error', config.failureTitle,
                result.error || 'Something went wrong. Please try again.',
                () => form.querySelector('input[type="password"]')?.focus());
            return;
        }

        const successText = config.successMessage
            ? config.successMessage(result)
            : (result.message || 'Success!');
        showAuthFeedback('success', config.successTitle, successText,
            () => config.onSuccess(result));
    } catch (error) {
        showAuthFeedback('error', 'Connection error',
            'Could not connect to the server. Make sure server.py is running.');
    } finally {
        button.disabled = false;
    }
}

loginForm.addEventListener('submit', (event) => {
    event.preventDefault();
    submitAuth(loginForm, '/api/login', {
        successTitle: 'Login successful',
        failureTitle: 'Login failed',
        successMessage: (result) =>
            `Welcome${result.name ? ', ' + result.name : ''}! Opening your dashboard...`,
        onSuccess: () => { window.location.href = 'studyious.html'; }
    });
});

registerForm.addEventListener('submit', (event) => {
    event.preventDefault();
    submitAuth(registerForm, '/api/register', {
        successTitle: 'Registration successful',
        failureTitle: 'Registration failed',
        onSuccess: () => {
            registerForm.reset();
            authModal.classList.remove('slide');
        }
    });
});
