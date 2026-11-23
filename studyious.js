const body = document.querySelector("body");
const sidebar = document.getElementById("sidebar");
const toggle = body.querySelector(".toggle");
const mobileMenuBtn = document.getElementById("mobileMenuBtn");
const sidebarOverlay = document.getElementById("sidebarOverlay");
const modeSwitch = body.querySelector(".toggle-switch");
const uploadInput = document.querySelector(".uploadFile");
const uploadStatus = document.getElementById("uploadStatus");
const logoutLink = document.getElementById("logoutLink");
const profileAvatar = document.getElementById("profileAvatar");
const avatarInitial = document.getElementById("avatarInitial");
const avatarImage = document.getElementById("avatarImage");
const avatarModal = document.getElementById("avatarModal");
const avatarCanvas = document.getElementById("avatarCanvas");
const avatarFileInput = document.getElementById("avatarFileInput");
const avatarZoom = document.getElementById("avatarZoom");
const avatarStatusEl = document.getElementById("avatarStatus");
const cropPlaceholder = document.getElementById("cropPlaceholder");
const removeAvatarBtn = document.getElementById("removeAvatarBtn");
const logoutModal = document.getElementById("logoutModal");
const logoutIcon = document.getElementById("logoutIcon");
const logoutTitle = document.getElementById("logoutTitle");
const logoutMessage = document.getElementById("logoutMessage");
const logoutConfirmBtn = document.getElementById("logoutConfirmBtn");
const logoutCancelBtn = document.getElementById("logoutCancelBtn");
const confirmModal = document.getElementById("confirmModal");
const confirmIcon = document.getElementById("confirmIcon");
const confirmTitle = document.getElementById("confirmTitle");
const confirmMessage = document.getElementById("confirmMessage");
const confirmDeleteBtn = document.getElementById("confirmDeleteBtn");
const confirmCancelBtn = document.getElementById("confirmCancelBtn");


const QUESTIONS_KEY = "studyiousQuestions";
const THEME_KEY = "studyiousTheme";
const CROP_SIZE = 320;
const MAX_QUESTIONS = 50;
// Dynamic Array and Queue is implemented for the question-storing
let questionArray = JSON.parse(localStorage.getItem(QUESTIONS_KEY) || "[]");

if (questionArray.length > MAX_QUESTIONS) {
    questionArray = questionArray.slice(0, MAX_QUESTIONS);
    saveQuestions();
    uploadStatus.textContent = `Only the first ${MAX_QUESTIONS} saved questions were kept.`;
}
// Profile Avatar
let currentUser = {};
let quizState = null;
let cropImage = null;
let cropBaseScale = 1;
let cropOffsetX = 0;
let cropOffsetY = 0;
let dragStart = null;

// Used for the creation of quizzes and files in the system
function makeElement(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
}

async function api(path, options = {}) {
    const response = await fetch(path, { credentials: "same-origin", ...options });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Request failed.");
    return result;
}

function formatTimestamp(value) {
    if (!value) return "";
    const raw = String(value).trim();
    const source = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(raw)
        ? raw + "Z"
        : raw;
    const date = new Date(source);
    if (Number.isNaN(date.getTime())) return raw;
    return new Intl.DateTimeFormat("en-PH", {
        timeZone: "Asia/Manila",
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        hour12: true,
    }).format(date);
}

function setStatus(element, message, isError = false) {
    element.textContent = message;
    element.className = (element.id === "uploadStatus" ? "upload-status" : "finalize-status") +
        (isError ? " error" : " success");
}


// User Authentication
fetch("/api/me", { credentials: "same-origin" })
    .then((response) => {
        if (!response.ok) throw new Error("Not logged in");
        return response.json();
    })
    .then((user) => {
        currentUser = user;
        renderSettings();
        renderAvatar();
    })
    .catch(() => { window.location.href = "login.html"; });

if (localStorage.getItem(THEME_KEY) === "dark") body.classList.add("dark");

function setSidebarOpen(open) {
    sidebar.classList.toggle("close", !open);
    if (mobileMenuBtn) mobileMenuBtn.setAttribute("aria-expanded", String(open));
}

toggle.addEventListener("click", (event) => {
    event.stopPropagation();
    if (window.innerWidth <= 700) setSidebarOpen(sidebar.classList.contains("close"));
    else sidebar.classList.toggle("close");
});

if (mobileMenuBtn) {
    mobileMenuBtn.addEventListener("click", (event) => {
        event.stopPropagation();
        setSidebarOpen(true);
    });
}

if (sidebarOverlay) sidebarOverlay.addEventListener("click", () => setSidebarOpen(false));

// On phones the sidebar is a drawer, so tapping the page closes it
document.querySelector(".dashboard").addEventListener("click", () => {
    if (window.innerWidth <= 700 && !sidebar.classList.contains("close")) {
        setSidebarOpen(false);
    }
});

modeSwitch.addEventListener("click", () => {
    body.classList.toggle("dark");
    localStorage.setItem(THEME_KEY, body.classList.contains("dark") ? "dark" : "light");
    renderSettings();
});


// Sidebar Navigation
const navItems = document.querySelectorAll(".nav-item");
const views = document.querySelectorAll(".view");

function showView(name) {
    views.forEach((view) => view.classList.toggle("active", view.id === `${name}-view`));
    navItems.forEach((item) => item.classList.toggle("active", item.dataset.view === name));
    if (window.innerWidth <= 700) setSidebarOpen(false);

    if (name === "files") loadFiles();
    if (name === "quizzes") loadQuizzes();
    if (name === "settings") renderSettings();
}

navItems.forEach((item) => {
    item.addEventListener("click", (event) => {
        event.preventDefault();
        showView(item.dataset.view);
    });
});


// Home Tab
function saveQuestions() {
    localStorage.setItem(QUESTIONS_KEY, JSON.stringify(questionArray));
}

// Items are either strings from uploaded files or [question, answer] pairs
function questionText(item) {
    return typeof item === "string" ? item : (item?.question || "");
}

function answerText(item) {
    return typeof item === "string" ? "" : (item?.answer || "");
}

function formatQuestion(item, index) {
    const question = questionText(item).trim();
    if (!question) return "";
    const answer = answerText(item).trim();
    return answer
        ? `${index + 1}. Q: ${question}\n   A: ${answer}`
        : `${index + 1}. ${question}`;
}

function renderQuestions() {
    document.getElementById("counter").textContent = `${questionArray.length} / ${MAX_QUESTIONS}`;
    document.getElementById("questions").textContent =
        questionArray.map(formatQuestion).filter(Boolean).join("\n") ||
        "Any input will be displayed here";
}

function storeQuestions() {
    const questionInput = document.getElementById("inputItems");
    const answerInput = document.getElementById("answerItems");
    const question = questionInput.value.trim();
    const answer = answerInput.value.trim();
    const status = document.getElementById("manualStatus");

    if (!question || !answer) {
        setStatus(status, "Enter both a question and an answer.", true);
        return;
    }
    if (questionArray.length >= MAX_QUESTIONS) {
        setStatus(status, `Question limit reached (${MAX_QUESTIONS}). Delete or finalize questions first.`, true);
        return;
    }

    // Queue applicaiton for the questionnaire
    questionArray.push({ question, answer });
    questionInput.value = "";
    answerInput.value = "";
    questionInput.focus();
    setStatus(status, "Question and answer added.");
    saveQuestions();
    renderQuestions();
}

document.getElementById("insertBtn").addEventListener("click", storeQuestions);
["inputItems", "answerItems"].forEach((id) => {
    document.getElementById(id).addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            storeQuestions();
        }
    });
});


// File uploads in Home tab
uploadInput.addEventListener("change", async () => {
    const file = uploadInput.files[0];
    if (!file) return;

    const extension = file.name.split(".").pop().toLowerCase();
    if (extension !== "docx" && extension !== "pdf") {
        setStatus(uploadStatus, "Only .docx and .pdf files are accepted.", true);
        uploadInput.value = "";
        return;
    }

    setStatus(uploadStatus, "Reading file...");
    const formData = new FormData();
    formData.append("file", file);

    try {
        const result = await api("/api/upload", { method: "POST", body: formData });
        let added = 0;
        let skipped = 0;
        for (const question of result.questions) {
            if (questionArray.length >= MAX_QUESTIONS) {
                skipped += 1;
                continue;
            }
            // Linear search checks the array if a question currently exists in the array
            if (questionArray.includes(question)) continue;
            // Queue - enqueue [Inserts a question at the end of the questionnaire]
            questionArray.push(question);
            added += 1;
        }
        saveQuestions();
        renderQuestions();
        const base = `Generated ${result.extracted_count} question(s) from ${result.filename}.`;
        setStatus(uploadStatus, skipped
            ? `${base} Added ${added}; question limit is ${MAX_QUESTIONS}.`
            : base);
    } catch (error) {
        setStatus(uploadStatus, error.message, true);
    }
    uploadInput.value = "";
});


// Finalizing the quiz in Home tab
document.getElementById("finalizeBtn").addEventListener("click", async () => {
    const finalizeStatus = document.getElementById("finalizeStatus");
    if (!questionArray.length) {
        setStatus(finalizeStatus, "Add at least one question first.", true);
        return;
    }

    const titleInput = document.getElementById("quizTitle");
    const title = titleInput.value.trim() || `Quiz - ${new Date().toLocaleDateString()}`;

    try {
        const quiz = await api("/api/quizzes", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title, questions: questionArray })
        });
        questionArray = [];
        titleInput.value = "";
        saveQuestions();
        renderQuestions();
        setStatus(finalizeStatus, `Saved "${quiz.title}" with ${quiz.question_count} question(s). Find it under Quizzes.`);
    } catch (error) {
        setStatus(finalizeStatus, error.message, true);
    }
});

document.getElementById("deleteQuestionsBtn").addEventListener("click", () => {
    const finalizeStatus = document.getElementById("finalizeStatus");
    if (!questionArray.length) {
        setStatus(finalizeStatus, "There are no questions to delete.", true);
        return;
    }

    const count = questionArray.length;
    showConfirmDialog({
        title: "Delete questions?",
        message: `Delete all ${count} question(s) from your working list? This cannot be undone.`,
        confirmLabel: "Delete questions",
        onConfirm: () => {
            questionArray = [];
            saveQuestions();
            renderQuestions();
            setStatus(finalizeStatus, `Deleted ${count} question(s).`);
        }
    });
});


// Confirmation pop-up when deleting a quiz and a file
let confirmAction = null;

function showConfirmDialog({ title, message, confirmLabel = "Delete", onConfirm }) {
    confirmIcon.className = "bxf bx-trash";
    confirmTitle.textContent = title;
    confirmMessage.textContent = message;
    confirmDeleteBtn.textContent = confirmLabel;
    confirmCancelBtn.hidden = false;
    confirmAction = onConfirm;
    confirmModal.hidden = false;
    confirmDeleteBtn.focus();
}

function showConfirmError(message, title = "Couldn't delete file") {
    confirmIcon.className = "bxf bx-x-circle";
    confirmTitle.textContent = title;
    confirmMessage.textContent = message;
    confirmDeleteBtn.textContent = "OK";
    confirmCancelBtn.hidden = true;
    confirmAction = closeConfirmDialog;
    confirmModal.hidden = false;
    confirmDeleteBtn.focus();
}

function closeConfirmDialog() {
    if (confirmModal.hidden) return;
    confirmModal.hidden = true;
    confirmAction = null;
}

confirmDeleteBtn.addEventListener("click", () => {
    const action = confirmAction;
    closeConfirmDialog();
    if (action) action();
});
confirmCancelBtn.addEventListener("click", closeConfirmDialog);
confirmModal.addEventListener("click", (event) => {
    if (event.target === confirmModal) closeConfirmDialog();
});


// Files Tab
async function loadFiles() {
    const list = document.getElementById("filesList");
    list.replaceChildren(makeElement("p", "empty", "Loading files..."));

    try {
        const result = await api("/api/files");
        list.replaceChildren();

        if (!result.files.length) {
            list.appendChild(makeElement("p", "empty", "No uploaded files yet. Add one from the Home tab."));
            return;
        }

        result.files.forEach((file) => {
            const card = makeElement("div", "item-card");
            const info = makeElement("div");
            info.appendChild(makeElement("h3", "", file.filename));
            info.appendChild(makeElement("p", "",
                `${file.question_count} question(s) - uploaded ${formatTimestamp(file.uploaded_at)}`));

            const actions = makeElement("div", "item-actions");
            const loadButton = makeElement("button", "small-btn", "Load questions");
            loadButton.addEventListener("click", () => loadFileQuestions(file.id));
            const deleteButton = makeElement("button", "small-btn", "Delete");
            deleteButton.addEventListener("click", () => {
                showConfirmDialog({
                    title: "Delete file?",
                    message: `Delete ${file.filename}? Its extracted questions will also be removed from your working list.`,
                    onConfirm: async () => {
                        try {
                            await removeFileQuestions(file.id);
                            await api(`/api/files/${file.id}`, { method: "DELETE" });
                            loadFiles();
                        } catch (error) {
                            showConfirmError(error.message);
                        }
                    }
                });
            });
            actions.append(loadButton, deleteButton);
            card.append(info, actions);
            list.appendChild(card);
        });
    } catch (error) {
        list.replaceChildren(makeElement("p", "empty", error.message));
    }
}

// Remove a deleted file's questions from the Home counter and questionnaire.
async function removeFileQuestions(fileId) {
    const result = await api(`/api/files/${fileId}`);
    const fileQuestionKeys = new Set(result.file.questions.map(questionText));
    // Linear search is used to scan for any matching file entries
    questionArray = questionArray.filter((item) => !fileQuestionKeys.has(questionText(item)));
    saveQuestions();
    renderQuestions();
    setStatus(uploadStatus, `Removed ${fileQuestionKeys.size} question(s) from ${result.file.filename}.`);
}

async function loadFileQuestions(fileId) {
    try {
        const result = await api(`/api/files/${fileId}`);
        let added = 0;
        let skipped = 0;
        for (const question of result.file.questions) {
            const exists = questionArray.some((item) => questionText(item) === questionText(question));
            if (exists) continue;
            if (questionArray.length >= MAX_QUESTIONS) {
                skipped += 1;
                continue;
            }
            // Queue is used for inserting questions at the rear of the questionnaire
            questionArray.push(question);
            added += 1;
        }
        saveQuestions();
        renderQuestions();
        const base = `Loaded ${result.file.questions.length} question(s) from ${result.file.filename}.`;
        setStatus(uploadStatus, skipped
            ? `${base} Added ${added}; question limit is ${MAX_QUESTIONS}.`
            : base);
        showView("home");
    } catch (error) {
        showConfirmError(error.message, "Couldn't load file");
    }
}


// Quizzes Tab
async function loadQuizzes() {
    const list = document.getElementById("quizzesList");
    list.replaceChildren(makeElement("p", "empty", "Loading quizzes..."));

    try {
        const result = await api("/api/quizzes");
        list.replaceChildren();

        if (!result.quizzes.length) {
            list.appendChild(makeElement("p", "empty", "No quizzes yet. Finalize your questions on the Home tab."));
            return;
        }

        result.quizzes.forEach((quiz) => {
            const card = makeElement("div", "item-card");
            const info = makeElement("div");
            info.appendChild(makeElement("h3", "", quiz.title));
            info.appendChild(makeElement("p", "",
                `${quiz.question_count} question(s) - created ${formatTimestamp(quiz.created_at)}`));

            const actions = makeElement("div", "item-actions");
            const startButton = makeElement("button", "action-btn", "Start quiz");
            startButton.addEventListener("click", () => startQuiz(quiz.id));
            const deleteButton = makeElement("button", "small-btn", "Delete");
            deleteButton.addEventListener("click", () => {
                showConfirmDialog({
                    title: "Delete quiz?",
                    message: `Delete quiz "${quiz.title}"? This cannot be undone.`,
                    confirmLabel: "Delete quiz",
                    onConfirm: async () => {
                        try {
                            await api(`/api/quizzes/${quiz.id}`, { method: "DELETE" });
                            loadQuizzes();
                        } catch (error) {
                            showConfirmError(error.message, "Couldn't delete quiz");
                        }
                    }
                });
            });
            actions.append(startButton, deleteButton);
            card.append(info, actions);
            list.appendChild(card);
        });
    } catch (error) {
        list.replaceChildren(makeElement("p", "empty", error.message));
    }
}

const quizSession = document.getElementById("quizSession");
const quizQuestion = document.getElementById("quizQuestion");
const quizProgress = document.getElementById("quizProgress");
const answerEntry = document.getElementById("answerEntry");
const quizAnswerInput = document.getElementById("quizAnswerInput");
const checkAnswerBtn = document.getElementById("checkAnswerBtn");
const quizFeedback = document.getElementById("quizFeedback");
const gotItBtn = document.getElementById("gotItBtn");
const reviewBtn = document.getElementById("reviewBtn");
const nextQuestionBtn = document.getElementById("nextQuestionBtn");


// Priority queue enables for any missed / incorrect questions to return to the question order
class MinHeap {
    constructor() {
        this.items = [];
    }

    get size() {
        return this.items.length;
    }

    _before(a, b) {
        return a.priority < b.priority || (a.priority === b.priority && a.seq < b.seq);
    }

    push(item) {
        this.items.push(item);
        let index = this.items.length - 1;
        while (index > 0) {
            const parent = Math.floor((index - 1) / 2);
            if (!this._before(this.items[index], this.items[parent])) break;
            [this.items[index], this.items[parent]] = [this.items[parent], this.items[index]];
            index = parent;
        }
    }

    _siftDown(index) {
        while (true) {
            const left = index * 2 + 1;
            const right = left + 1;
            let smallest = index;
            if (left < this.items.length && this._before(this.items[left], this.items[smallest])) {
                smallest = left;
            }
            if (right < this.items.length && this._before(this.items[right], this.items[smallest])) {
                smallest = right;
            }
            if (smallest === index) break;
            [this.items[index], this.items[smallest]] = [this.items[smallest], this.items[index]];
            index = smallest;
        }
    }

    pop() {
        if (!this.items.length) return null;
        const top = this.items[0];
        const last = this.items.pop();
        if (this.items.length) {
            this.items[0] = last;
            this._siftDown(0);
        }
        return top;
    }

    // Heapify is used in order to correct the priority order of the questions
    reheapify() {
        for (let index = Math.floor(this.items.length / 2) - 1; index >= 0; index -= 1) {
            this._siftDown(index);
        }
    }
}

const REVIEW_DELAY = 2; // Missed questions return to the order after 2 questions


function buildQuestionQueue(questions) {
    const queue = new MinHeap();
    questions.forEach((question, index) => {
        queue.push({ question, priority: index, seq: index });
    });
    return queue;
}

const GRAPH_SEARCH_DEPTH = 2; 
const RELATED_BOOST = 0.75;   

// BFS is utilized to check for any related topics within a quiz
function relatedTopics(topics, graph) {
    const related = new Set();
    const visited = new Set(topics);
    const queue = [...topics];
    let head = 0;
    let depth = 0;

    while (head < queue.length && depth < GRAPH_SEARCH_DEPTH) {
        const levelEnd = queue.length;
        while (head < levelEnd) {
            const topic = queue[head++];
            for (const neighbor of graph[topic] || []) {
                if (visited.has(neighbor)) continue;
                visited.add(neighbor);
                related.add(neighbor);
                queue.push(neighbor);
            }
        }
        depth += 1;
    }
    return related;
}

function boostRelatedQuestions(related) {
    if (!related.size) return 0;
    let boosted = 0;
    for (const card of quizState.queue.items) {
        const overlap = (card.question.topics || []).filter((topic) => related.has(topic)).length;
        if (!overlap) continue;
        card.priority -= RELATED_BOOST * overlap;
        boosted += 1;
    }
    if (boosted) quizState.queue.reheapify();
    return boosted;
}

async function startQuiz(quizId) {
    try {
        const result = await api(`/api/quizzes/${quizId}`);
        const questions = result.quiz.questions || [];
        quizState = {
            ...result.quiz,
            // Adjacency list is used to map out related topics in missed questions in order for the quiz to adapt
            graph: result.quiz.concept_graph || {},
            queue: buildQuestionQueue(questions),
            current: null,
            asked: 0,
            correct: 0,
            seq: questions.length,
            answered: false
        };
        document.getElementById("quizSessionTitle").textContent = quizState.title;
        quizSession.hidden = false;
        renderQuizQuestion();
    } catch (error) {
        showConfirmError(error.message, "Couldn't start quiz");
    }
}

function normalizeAnswer(value) {
    return String(value || "")
        .toLowerCase()
        .replace(/[^a-z0-9\s]/g, " ")
        .replace(/\s+/g, " ")
        .trim();
}

function showNextButton() {
    nextQuestionBtn.hidden = false;
    nextQuestionBtn.textContent = quizState.queue.size > 0 ? "Next question" : "Finish quiz";
    quizProgress.textContent = `Question ${quizState.asked} of ${quizState.asked + quizState.queue.size}`;
}

function requeueCurrent() {
    if (!quizState.current) return 0;
    const topics = quizState.current.question.topics || [];
    const boosted = boostRelatedQuestions(relatedTopics(topics, quizState.graph || {}));
    quizState.queue.push({
        ...quizState.current,
        priority: quizState.asked + REVIEW_DELAY - 0.5,
        seq: quizState.seq++
    });
    return boosted;
}

function renderQuizQuestion() {
    answerEntry.hidden = true;
    gotItBtn.hidden = true;
    reviewBtn.hidden = true;
    nextQuestionBtn.hidden = true;
    quizFeedback.hidden = true;
    quizFeedback.className = "quiz-feedback";
    quizFeedback.textContent = "";
    quizAnswerInput.value = "";
    quizState.current = null;

    if (!quizState.queue.size) {
        const reviews = quizState.asked - quizState.questions.length;
        quizProgress.textContent = "Quiz complete";
        quizQuestion.textContent =
            `You got ${quizState.correct} out of ${quizState.asked} answer(s) correct.` +
            (reviews > 0 ? ` ${reviews} review(s) completed.` : "") +
            " Keep studying!";
        return;
    }

    const card = quizState.queue.pop();
    quizState.current = card;
    quizState.asked += 1;
    const item = card.question;
    quizProgress.textContent = `Question ${quizState.asked} of ${quizState.asked + quizState.queue.size}`;
    quizQuestion.textContent = questionText(item) || "(No question text)";

    if (answerText(item)) {
        answerEntry.hidden = false;
        quizAnswerInput.focus();
    } else {
        quizFeedback.hidden = false;
        quizFeedback.textContent = "No answer is stored for this point - self-check it.";
        gotItBtn.hidden = false;
        reviewBtn.hidden = false;
    }
}

function checkAnswer() {
    const answer = answerText(quizState.current?.question);
    const userAnswer = normalizeAnswer(quizAnswerInput.value);
    const expected = normalizeAnswer(answer);
    const correct = userAnswer === expected || (userAnswer.length >= 4 && expected.includes(userAnswer));

    if (correct) quizState.correct += 1;
    else requeueCurrent();
    quizFeedback.hidden = false;
    quizFeedback.className = `quiz-feedback ${correct ? "correct" : "incorrect"}`;
    quizFeedback.textContent = correct
        ? "Correct!"
        : `Incorrect. The answer is ${answer}`;
    answerEntry.hidden = true;
    showNextButton();
}

function markQuestion(knewIt) {
    let boosted = 0;
    if (knewIt) quizState.correct += 1;
    else boosted = requeueCurrent();
    gotItBtn.hidden = true;
    reviewBtn.hidden = true;
    quizFeedback.hidden = !knewIt;
    if (!knewIt) {
        quizFeedback.className = "quiz-feedback";
        quizFeedback.textContent = "Marked for review - this question will return." +
            (boosted ? ` ${boosted} related question(s) moved up.` : "");
    }
    showNextButton();
}

checkAnswerBtn.addEventListener("click", checkAnswer);
quizAnswerInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
        event.preventDefault();
        checkAnswer();
    }
});
gotItBtn.addEventListener("click", () => markQuestion(true));
reviewBtn.addEventListener("click", () => markQuestion(false));
nextQuestionBtn.addEventListener("click", () => {
    renderQuizQuestion();
});
document.getElementById("closeQuizBtn").addEventListener("click", () => {
    quizSession.hidden = true;
    quizState = null;
});


// Settings Tab
function renderSettings() {
    document.getElementById("settingsName").textContent = currentUser.name || "...";
    document.getElementById("settingsEmail").textContent = currentUser.email || "...";
    document.getElementById("settingsTheme").textContent =
        body.classList.contains("dark") ? "Dark" : "Light";
}

document.getElementById("clearWorkingBtn").addEventListener("click", () => {
    questionArray = [];
    saveQuestions();
    renderQuestions();
    setStatus(document.getElementById("settingsStatus"), "Working questions cleared.");
});


// Profile Avatar
function renderAvatar() {
    const name = (currentUser.name || "").trim();
    avatarInitial.textContent = name ? name[0].toUpperCase() : "?";
    if (currentUser.avatar) {
        avatarImage.src = currentUser.avatar;
        avatarImage.hidden = false;
        avatarInitial.hidden = true;
    } else {
        avatarImage.removeAttribute("src");
        avatarImage.hidden = true;
        avatarInitial.hidden = false;
    }
}

function clampCrop() {
    if (!cropImage) return;
    const zoom = Number(avatarZoom.value) / 100;
    const width = cropImage.width * cropBaseScale * zoom;
    const height = cropImage.height * cropBaseScale * zoom;
    const maxX = Math.max(0, (width - CROP_SIZE) / 2);
    const maxY = Math.max(0, (height - CROP_SIZE) / 2);
    cropOffsetX = Math.max(-maxX, Math.min(maxX, cropOffsetX));
    cropOffsetY = Math.max(-maxY, Math.min(maxY, cropOffsetY));
}

function drawCrop() {
    const context = avatarCanvas.getContext("2d");
    context.clearRect(0, 0, CROP_SIZE, CROP_SIZE);
    if (!cropImage) return;
    const zoom = Number(avatarZoom.value) / 100;
    const width = cropImage.width * cropBaseScale * zoom;
    const height = cropImage.height * cropBaseScale * zoom;
    const left = (CROP_SIZE - width) / 2 + cropOffsetX;
    const top = (CROP_SIZE - height) / 2 + cropOffsetY;
    context.drawImage(cropImage, left, top, width, height);
}

function refreshCrop() {
    clampCrop();
    drawCrop();
}

function openAvatarModal() {
    avatarModal.hidden = false;
    cropImage = null;
    cropOffsetX = 0;
    cropOffsetY = 0;
    cropPlaceholder.hidden = false;
    avatarZoom.value = "100";
    avatarFileInput.value = "";
    removeAvatarBtn.hidden = !currentUser.avatar;
    avatarCanvas.getContext("2d").clearRect(0, 0, CROP_SIZE, CROP_SIZE);
    setStatus(avatarStatusEl, "");
}

function closeAvatarModal() {
    avatarModal.hidden = true;
}

avatarFileInput.addEventListener("change", (event) => {
    const file = event.target.files[0];
    if (!file) return;
    if (!file.type.startsWith("image/")) {
        setStatus(avatarStatusEl, "Please choose an image file.", true);
        return;
    }
    if (file.size > 10 * 1024 * 1024) {
        setStatus(avatarStatusEl, "Please choose an image smaller than 10 MB.", true);
        return;
    }

    const objectUrl = URL.createObjectURL(file);
    const image = new Image();
    image.onload = () => {
        URL.revokeObjectURL(objectUrl);
        cropImage = image;
        cropBaseScale = Math.max(CROP_SIZE / image.width, CROP_SIZE / image.height);
        cropOffsetX = 0;
        cropOffsetY = 0;
        avatarZoom.value = "100";
        cropPlaceholder.hidden = true;
        refreshCrop();
        setStatus(avatarStatusEl, "");
    };
    image.onerror = () => {
        URL.revokeObjectURL(objectUrl);
        setStatus(avatarStatusEl, "That image could not be opened.", true);
    };
    image.src = objectUrl;
});

avatarZoom.addEventListener("input", refreshCrop);

avatarCanvas.addEventListener("pointerdown", (event) => {
    if (!cropImage) return;
    dragStart = {
        x: event.clientX,
        y: event.clientY,
        offsetX: cropOffsetX,
        offsetY: cropOffsetY,
    };
    avatarCanvas.setPointerCapture(event.pointerId);
    avatarCanvas.classList.add("dragging");
});

avatarCanvas.addEventListener("pointermove", (event) => {
    if (!dragStart) return;
    const scale = CROP_SIZE / avatarCanvas.getBoundingClientRect().width;
    cropOffsetX = dragStart.offsetX + (event.clientX - dragStart.x) * scale;
    cropOffsetY = dragStart.offsetY + (event.clientY - dragStart.y) * scale;
    refreshCrop();
});

["pointerup", "pointercancel"].forEach((type) => {
    avatarCanvas.addEventListener(type, () => {
        dragStart = null;
        avatarCanvas.classList.remove("dragging");
    });
});

document.getElementById("saveAvatarBtn").addEventListener("click", async () => {
    if (!cropImage) {
        setStatus(avatarStatusEl, "Choose an image first.", true);
        return;
    }
    try {
        const result = await api("/api/avatar", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ image: avatarCanvas.toDataURL("image/png") }),
        });
        currentUser.avatar = result.avatar;
        renderAvatar();
        closeAvatarModal();
        setStatus(document.getElementById("settingsStatus"), "Profile picture updated.");
    } catch (error) {
        setStatus(avatarStatusEl, error.message, true);
    }
});

removeAvatarBtn.addEventListener("click", async () => {
    try {
        await api("/api/avatar", { method: "DELETE" });
        currentUser.avatar = null;
        renderAvatar();
        closeAvatarModal();
        setStatus(document.getElementById("settingsStatus"), "Profile picture removed.");
    } catch (error) {
        setStatus(avatarStatusEl, error.message, true);
    }
});

profileAvatar.addEventListener("click", openAvatarModal);
document.getElementById("changeAvatarBtn").addEventListener("click", openAvatarModal);
document.getElementById("closeAvatarBtn").addEventListener("click", closeAvatarModal);
avatarModal.addEventListener("click", (event) => {
    if (event.target === avatarModal) closeAvatarModal();
});
document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (window.innerWidth <= 700 && !sidebar.classList.contains("close")) setSidebarOpen(false);
    if (!avatarModal.hidden) closeAvatarModal();
    if (!logoutModal.hidden) closeLogoutModal();
    if (!confirmModal.hidden) closeConfirmDialog();
});


// Log out
let logoutAction = null;

function showLogoutModal(mode) {
    if (mode === "done") {
        logoutIcon.className = "bxf bx-check-circle";
        logoutTitle.textContent = "Logged out";
        logoutMessage.textContent = "You have been logged out successfully.";
        logoutConfirmBtn.textContent = "OK";
        logoutCancelBtn.hidden = true;
        logoutAction = () => { window.location.href = "login.html"; };
    } else {
        logoutIcon.className = "bxf bx-arrow-out-left-square-half";
        logoutTitle.textContent = "Log out?";
        logoutMessage.textContent = "You'll need to log in again to access your quizzes.";
        logoutConfirmBtn.textContent = "Log out";
        logoutCancelBtn.hidden = false;
        logoutAction = performLogout;
    }
    logoutModal.hidden = false;
    logoutConfirmBtn.focus();
}

function closeLogoutModal() {
    if (logoutModal.hidden) return;
    logoutModal.hidden = true;
    logoutAction = null;
}

function performLogout() {
    logoutConfirmBtn.disabled = true;
    logoutConfirmBtn.textContent = "Logging out...";
    api("/api/logout", { method: "POST" })
        .catch(() => { /* Confirm the log out even if the request fails. */ })
        .finally(() => {
            logoutConfirmBtn.disabled = false;
            showLogoutModal("done");
        });
}

logoutLink.addEventListener("click", (event) => {
    event.preventDefault();
    showLogoutModal("confirm");
});

logoutConfirmBtn.addEventListener("click", () => {
    const action = logoutAction;
    closeLogoutModal();
    if (action) action();
});
logoutCancelBtn.addEventListener("click", closeLogoutModal);
logoutModal.addEventListener("click", (event) => {
    if (event.target === logoutModal) closeLogoutModal();
});

renderQuestions();
