// Global state
let shelfData = [];
let progressData = {};
let player;
let currentActiveItem = null;
let progressSyncInterval = null;

// DOM Elements
const shelfGrid = document.getElementById('shelfGrid');
const searchInput = document.getElementById('searchInput');
const typeFilter = document.getElementById('typeFilter');
const statusFilter = document.getElementById('statusFilter');
const genreFilter = document.getElementById('genreFilter');

const ytModal = document.getElementById('ytModal');
const closeYtModal = document.getElementById('closeYtModal');
const modalTitle = document.getElementById('modalTitle');
const modalCreator = document.getElementById('modalCreator');
const markFinishedBtn = document.getElementById('markFinishedBtn');
const openOriginalBtn = document.getElementById('openOriginalBtn');

// Notes Panel Elements
const notesPanel = document.getElementById('notesPanel');
const newNoteInput = document.getElementById('newNoteInput');
const addNoteBtn = document.getElementById('addNoteBtn');
const notesList = document.getElementById('notesList');
const generateSummaryBtn = document.getElementById('generateSummaryBtn');
const aiSummaryOutput = document.getElementById('aiSummaryOutput');
const aiSummaryContent = document.getElementById('aiSummaryContent');


// YouTube IFrame API Ready Callback
function onYouTubeIframeAPIReady() {
    console.log("YouTube API Ready");
}

// Fetch initial data
async function loadData() {
    try {
        const res = await fetch('/api/shelf');
        const json = await res.json();
        if (json.status === 'success') {
            shelfData = json.data;
            progressData = json.progress;
            renderGrid();
        }
    } catch (e) {
        shelfGrid.innerHTML = '<div class="loading-spinner"><i class="fas fa-exclamation-triangle"></i> Failed to load shelf data.</div>';
        console.error(e);
    }
}

function renderGrid() {
    const searchTerm = searchInput.value.toLowerCase();
    const typeVal = typeFilter.value;
    const statusVal = statusFilter.value;

    shelfGrid.innerHTML = '';

    const filtered = shelfData.filter(item => {
        // Text Match
        const textMatch = !searchTerm || 
            (item.title && item.title.toLowerCase().includes(searchTerm)) ||
            (item.creator && item.creator.toLowerCase().includes(searchTerm)) ||
            (item.ai_summary && item.ai_summary.toLowerCase().includes(searchTerm));
        
        // Type Match
        const typeMatch = typeVal === 'ALL' || item.content_type === typeVal;

        // Status Match
        const prog = progressData[item.content_hash] || { is_completed: false };
        const isCompleted = prog.is_completed;
        let statusMatch = true;
        if (statusVal === 'UNREAD' && isCompleted) statusMatch = false;
        if (statusVal === 'COMPLETED' && !isCompleted) statusMatch = false;

        // Genre Match
        let genreMatch = true;
        if (genreFilter.value !== 'ALL') {
            const g = genreFilter.value;
            const t = item.tags ? String(item.tags).toLowerCase() : '';
            const s = item.ai_summary ? String(item.ai_summary).toLowerCase() : '';
            genreMatch = t.includes(g) || s.includes(g);
        }

        return textMatch && typeMatch && statusMatch && genreMatch;
    });

    if (filtered.length === 0) {
        shelfGrid.innerHTML = '<div class="loading-spinner">No items found matching your filters.</div>';
        return;
    }

    filtered.forEach(item => {
        const prog = progressData[item.content_hash] || { progress_seconds: 0, is_completed: false };
        
        const card = document.createElement('div');
        card.className = 'shelf-card glass-panel';
        
        // Thumbnail handling
        let thumbUrl = item.thumbnail_url;
        if (!thumbUrl) {
            // Placeholder based on type
            if (item.content_type === 'YOUTUBE') thumbUrl = 'https://images.unsplash.com/photo-1611162617213-7d7a39e9b1d7?auto=format&fit=crop&w=600&q=80';
            else if (item.content_type === 'BOOK') thumbUrl = 'https://images.unsplash.com/photo-1544947950-fa07a98d237f?auto=format&fit=crop&w=600&q=80';
            else thumbUrl = 'https://images.unsplash.com/photo-1451187580459-43490279c0fa?auto=format&fit=crop&w=600&q=80';
        }

        // Check if youtube to extract ID for better thumbnail
        if (item.content_type === 'YOUTUBE' && !item.thumbnail_url && item.url) {
            const ytMatch = item.url.match(/(?:v=|youtu\.be\/)([^&]+)/);
            if (ytMatch && ytMatch[1]) {
                thumbUrl = `https://img.youtube.com/vi/${ytMatch[1]}/maxresdefault.jpg`;
            }
        }

        const isCompletedBadge = prog.is_completed ? `<div class="status-badge status-COMPLETED"><i class="fas fa-check-circle"></i> Completed</div>` : '';

        // Estimate progress bar width (just visually, say 1 hour max for videos to show *some* progress)
        // If it's completed, 100%. Otherwise show something.
        let width = prog.is_completed ? 100 : (prog.progress_seconds > 0 ? Math.min(100, (prog.progress_seconds / 600) * 100) : 0);
        if(item.content_type !== 'YOUTUBE' && !prog.is_completed) width = 0;

        card.innerHTML = `
            <div class="card-thumbnail-container">
                <img src="${thumbUrl}" alt="Thumbnail" class="card-thumbnail" onerror="this.src='https://images.unsplash.com/photo-1451187580459-43490279c0fa?auto=format&fit=crop&w=600&q=80'">
                <div class="type-badge type-${item.content_type}">${item.content_type}</div>
                ${isCompletedBadge}
            </div>
            <div class="card-content">
                <h3 class="card-title">${item.title}</h3>
                <p class="card-creator">${item.creator ? '<i class="fas fa-user-edit"></i> ' + item.creator : ''}</p>
                <div class="card-progress-bg">
                    <div class="card-progress-fill" style="width: ${width}%"></div>
                </div>
            </div>
        `;

        card.addEventListener('click', () => openItemModal(item));
        shelfGrid.appendChild(card);
    });
}

function extractYouTubeId(url) {
    if (!url) return null;
    const match = url.match(/(?:v=|youtu\.be\/)([^&]+)/);
    return match ? match[1] : null;
}

function openItemModal(item) {
    currentActiveItem = item;
    const prog = progressData[item.content_hash] || { progress_seconds: 0, is_completed: false };
    
    modalTitle.textContent = item.title;
    modalCreator.textContent = item.creator || 'Unknown Creator';
    
    // Fix legacy tachiyomi links that were already saved in the database
    let itemUrl = item.url;
    if (itemUrl && itemUrl.includes('github.com/tachiyomiorg')) {
        itemUrl = `https://asurascans.com/?s=${encodeURIComponent(item.title)}`;
    }
    openOriginalBtn.href = itemUrl;
    
    // Automatically copy the title to clipboard for Manga/Manhwa if they need to manually search AsuraScans
    openOriginalBtn.onclick = () => {
        if (item.content_type === 'MANGA') {
            navigator.clipboard.writeText(item.title).catch(err => console.error('Clipboard copy failed:', err));
        }
    };
    
    updateFinishedBtnState(prog.is_completed);

    const playerContainer = document.getElementById('playerContainer');
    const moviePlayer = document.getElementById('moviePlayer');
    const ytId = item.content_type === 'YOUTUBE' ? extractYouTubeId(item.url) : null;
    const adblockWarning = document.getElementById('adblockWarning');

    if (item.content_type === 'MOVIE_TV' || item.content_type === 'ANIME' || item.content_type === 'MANGA') {
        if (adblockWarning) adblockWarning.classList.remove('hidden');
    } else {
        if (adblockWarning) adblockWarning.classList.add('hidden');
    }

    // Reset displays
    document.getElementById('ytPlayer').style.display = 'none';
    if (moviePlayer) {
        moviePlayer.style.display = 'none';
        moviePlayer.src = '';
    }

    if (ytId) {
        playerContainer.style.display = 'block';
        notesPanel.style.display = 'flex';
        ytModal.querySelector('.modal-content').classList.add('modal-split');
        
        document.getElementById('ytPlayer').style.display = 'block';
        if (player) {
            player.loadVideoById({
                videoId: ytId,
                startSeconds: prog.progress_seconds || 0
            });
        } else {
            player = new YT.Player('ytPlayer', {
                height: '100%',
                width: '100%',
                videoId: ytId,
                playerVars: {
                    'start': prog.progress_seconds || 0,
                    'autoplay': 1
                },
                events: {
                    'onStateChange': onPlayerStateChange
                }
            });
        }
        startProgressSync();
        loadNotes(item.content_hash);
    } else {
        playerContainer.style.display = 'none';
        notesPanel.style.display = 'none';
        ytModal.querySelector('.modal-content').classList.remove('modal-split');
        if (player && typeof player.stopVideo === 'function') {
            player.stopVideo();
        }
    }

    ytModal.classList.remove('hidden');
}

function closeItemModal() {
    ytModal.classList.add('hidden');
    if (player && typeof player.stopVideo === 'function') {
        player.stopVideo();
    }
    const moviePlayer = document.getElementById('moviePlayer');
    if (moviePlayer) moviePlayer.src = '';
    stopProgressSync();
    currentActiveItem = null;
}

function updateFinishedBtnState(isCompleted) {
    if (isCompleted) {
        markFinishedBtn.innerHTML = '<i class="fas fa-undo"></i> Mark as Unread';
        markFinishedBtn.classList.add('completed');
    } else {
        markFinishedBtn.innerHTML = '<i class="fas fa-check"></i> Mark as Finished';
        markFinishedBtn.classList.remove('completed');
    }
}

async function toggleFinishedStatus() {
    if (!currentActiveItem) return;
    const hash = currentActiveItem.content_hash;
    const prog = progressData[hash] || { progress_seconds: 0, is_completed: false };
    
    prog.is_completed = !prog.is_completed;
    
    // Update local state immediately
    progressData[hash] = prog;
    updateFinishedBtnState(prog.is_completed);
    
    // If it's a YouTube video and we mark it as finished, pause the video if playing?
    // User preference, leave it for now.

    // Sync to server
    await syncProgressToServer(hash, prog.progress_seconds, prog.is_completed);
    
    // Re-render grid to show/hide based on filters
    renderGrid();
}

function onPlayerStateChange(event) {
    if (event.data == YT.PlayerState.PLAYING) {
        startProgressSync();
    } else if (event.data == YT.PlayerState.PAUSED || event.data == YT.PlayerState.ENDED) {
        stopProgressSync();
        syncCurrentPlayerProgress();
        if(event.data == YT.PlayerState.ENDED) {
             // Auto-mark as finished when video ends
             const hash = currentActiveItem.content_hash;
             const prog = progressData[hash] || { progress_seconds: 0, is_completed: false };
             if(!prog.is_completed) {
                 toggleFinishedStatus();
             }
        }
    }
}

function startProgressSync() {
    if (progressSyncInterval) clearInterval(progressSyncInterval);
    progressSyncInterval = setInterval(() => {
        syncCurrentPlayerProgress();
    }, 5000); // Sync every 5 seconds
}

function stopProgressSync() {
    if (progressSyncInterval) {
        clearInterval(progressSyncInterval);
        progressSyncInterval = null;
    }
}

function syncCurrentPlayerProgress() {
    if (!currentActiveItem || !player || typeof player.getCurrentTime !== 'function') return;
    
    const hash = currentActiveItem.content_hash;
    const currentTime = Math.floor(player.getCurrentTime());
    
    if (currentTime > 0) {
        const prog = progressData[hash] || { is_completed: false };
        prog.progress_seconds = currentTime;
        progressData[hash] = prog;
        syncProgressToServer(hash, currentTime, prog.is_completed);
    }
}

async function syncProgressToServer(hash, seconds, isCompleted) {
    try {
        await fetch('/api/progress', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                content_hash: hash,
                progress_seconds: seconds,
                is_completed: isCompleted
            })
        });
    } catch (e) {
        console.error("Failed to sync progress", e);
    }
}

// Notes Logic
function formatTime(seconds) {
    const m = Math.floor(seconds / 60).toString().padStart(2, '0');
    const s = (seconds % 60).toString().padStart(2, '0');
    return `${m}:${s}`;
}

async function loadNotes(hash) {
    notesList.innerHTML = '<div class="loading-spinner">Loading notes...</div>';
    aiSummaryOutput.classList.add('hidden');
    try {
        const res = await fetch(`/api/notes/${hash}`);
        const json = await res.json();
        notesList.innerHTML = '';
        if (json.status === 'success' && json.notes.length > 0) {
            json.notes.forEach(note => renderNote(note));
        } else {
            notesList.innerHTML = '<p style="color:var(--text-muted); text-align:center; margin-top:20px;">No notes yet. Take your first note!</p>';
        }
    } catch (e) {
        notesList.innerHTML = '<p>Error loading notes.</p>';
    }
}

function renderNote(note) {
    const el = document.createElement('div');
    el.className = 'note-item';
    el.innerHTML = `
        <span class="note-timestamp" onclick="seekPlayer(${note.timestamp_seconds})">[${formatTime(note.timestamp_seconds)}]</span>
        <div class="note-text">${note.note_text}</div>
    `;
    notesList.appendChild(el);
}

function seekPlayer(seconds) {
    if (player && typeof player.seekTo === 'function') {
        player.seekTo(seconds, true);
    }
}

async function handleAddNote() {
    const text = newNoteInput.value.trim();
    if (!text || !currentActiveItem) return;
    
    let currentSeconds = 0;
    if (player && typeof player.getCurrentTime === 'function') {
        currentSeconds = Math.floor(player.getCurrentTime());
    }

    newNoteInput.value = '';
    
    // Remove "no notes yet" msg if it's there
    if(notesList.innerHTML.includes('No notes yet')) {
        notesList.innerHTML = '';
    }

    try {
        const res = await fetch('/api/notes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                content_hash: currentActiveItem.content_hash,
                timestamp_seconds: currentSeconds,
                note_text: text
            })
        });
        const json = await res.json();
        if (json.status === 'success') {
            renderNote(json.note);
            notesList.scrollTop = notesList.scrollHeight; // auto scroll
        }
    } catch (e) {
        console.error("Failed to add note", e);
    }
}

async function handleGenerateSummary() {
    if (!currentActiveItem) return;
    generateSummaryBtn.innerHTML = '<i class="fas fa-circle-notch fa-spin"></i> Generating...';
    generateSummaryBtn.disabled = true;
    
    try {
        const res = await fetch(`/api/notes/${currentActiveItem.content_hash}/generate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: currentActiveItem.title })
        });
        const json = await res.json();
        
        aiSummaryOutput.classList.remove('hidden');
        if (json.status === 'success' && typeof marked !== 'undefined') {
            aiSummaryContent.innerHTML = marked.parse(json.summary);
        } else {
            aiSummaryContent.innerHTML = '<p>Failed to generate summary or no notes available.</p>';
        }
    } catch (e) {
        console.error("Failed to generate summary", e);
        aiSummaryOutput.classList.remove('hidden');
        aiSummaryContent.innerHTML = '<p>Error generating summary.</p>';
    } finally {
        generateSummaryBtn.innerHTML = '<i class="fas fa-magic"></i> Generate AI Master Note';
        generateSummaryBtn.disabled = false;
    }
}

// Genre Mappings
const genreMapping = {
    'ALL': [
        { value: 'ALL', label: 'All Genres' },
        { value: 'motivational', label: 'Motivational' },
        { value: 'finance', label: 'Finance' },
        { value: 'tech', label: 'Tech' },
        { value: 'action', label: 'Action' },
        { value: 'romance', label: 'Romance' },
        { value: 'comedy', label: 'Comedy' },
        { value: 'sci-fi', label: 'Sci-Fi' },
        { value: 'fantasy', label: 'Fantasy' }
    ],
    'YOUTUBE': [
        { value: 'ALL', label: 'All Genres' },
        { value: 'motivational', label: 'Motivational' },
        { value: 'sales', label: 'Sales' },
        { value: 'marketing', label: 'Marketing' },
        { value: 'discipline', label: 'Discipline' },
        { value: 'finance', label: 'Finance' },
        { value: 'money', label: 'Money' },
        { value: 'mind', label: 'Mind' },
        { value: 'peace', label: 'Peace' },
        { value: 'tech', label: 'Tech' },
        { value: 'entertainment', label: 'Entertainment' },
        { value: 'education', label: 'Education' }
    ],
    'MOVIE_TV': [
        { value: 'ALL', label: 'All Genres' },
        { value: 'action', label: 'Action' },
        { value: 'comedy', label: 'Comedy' },
        { value: 'drama', label: 'Drama' },
        { value: 'sci-fi', label: 'Sci-Fi' },
        { value: 'fantasy', label: 'Fantasy' },
        { value: 'horror', label: 'Horror' },
        { value: 'thriller', label: 'Thriller' },
        { value: 'romance', label: 'Romance' },
        { value: 'documentary', label: 'Documentary' }
    ],
    'ANIME': [
        { value: 'ALL', label: 'All Genres' },
        { value: 'shounen', label: 'Shounen' },
        { value: 'shoujo', label: 'Shoujo' },
        { value: 'isekai', label: 'Isekai' },
        { value: 'slice of life', label: 'Slice of Life' },
        { value: 'mecha', label: 'Mecha' },
        { value: 'action', label: 'Action' },
        { value: 'romance', label: 'Romance' },
        { value: 'fantasy', label: 'Fantasy' }
    ],
    'MANGA': [
        { value: 'ALL', label: 'All Genres' },
        { value: 'action', label: 'Action' },
        { value: 'romance', label: 'Romance' },
        { value: 'fantasy', label: 'Fantasy' },
        { value: 'isekai', label: 'Isekai' },
        { value: 'martial arts', label: 'Martial Arts' },
        { value: 'comedy', label: 'Comedy' },
        { value: 'drama', label: 'Drama' },
        { value: 'slice of life', label: 'Slice of Life' }
    ],
    'BOOK': [
        { value: 'ALL', label: 'All Genres' },
        { value: 'fiction', label: 'Fiction' },
        { value: 'non-fiction', label: 'Non-Fiction' },
        { value: 'self-help', label: 'Self-Help' },
        { value: 'biography', label: 'Biography' },
        { value: 'fantasy', label: 'Fantasy' },
        { value: 'sci-fi', label: 'Sci-Fi' },
        { value: 'mystery', label: 'Mystery' },
        { value: 'romance', label: 'Romance' },
        { value: 'history', label: 'History' }
    ]
};

function updateGenreFilter() {
    const selectedType = typeFilter.value;
    const genres = genreMapping[selectedType] || genreMapping['ALL'];
    const prevSelected = genreFilter.value;
    
    genreFilter.innerHTML = '';
    genres.forEach(g => {
        const option = document.createElement('option');
        option.value = g.value;
        option.textContent = g.label;
        genreFilter.appendChild(option);
    });
    
    if (genres.some(g => g.value === prevSelected)) {
        genreFilter.value = prevSelected;
    } else {
        genreFilter.value = 'ALL';
    }
}

// Event Listeners
searchInput.addEventListener('input', renderGrid);
typeFilter.addEventListener('change', () => {
    updateGenreFilter();
    renderGrid();
});
statusFilter.addEventListener('change', renderGrid);
genreFilter.addEventListener('change', renderGrid);
closeYtModal.addEventListener('click', closeItemModal);
markFinishedBtn.addEventListener('click', toggleFinishedStatus);

addNoteBtn.addEventListener('click', handleAddNote);
newNoteInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') handleAddNote();
});
generateSummaryBtn.addEventListener('click', handleGenerateSummary);

// Close modal on click outside
ytModal.addEventListener('click', (e) => {
    if (e.target === ytModal) closeItemModal();
});

// Init
document.addEventListener('DOMContentLoaded', () => {
    updateGenreFilter();
    loadData();
});
