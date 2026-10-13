// Theme/mode selection + localStorage persistence. See
// PI_CONTROL_PLAN.md "UI Design - Themes" / "Theme switcher".
//
// The actual flash-of-wrong-theme guard lives in index.html as a tiny
// inline <script> in <head> that runs before this file loads - this
// module just owns the switcher UI and keeps everything in sync after
// first paint.

const STORAGE_KEY = "acom.theme";
const DEFAULT_THEME = { style: "acom", mode: "dark" };
const VALID_STYLES = ["acom", "classic", "modern"];
const VALID_MODES = ["dark", "light"];

export function loadTheme() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULT_THEME };
    const parsed = JSON.parse(raw);
    if (!VALID_STYLES.includes(parsed.style) || !VALID_MODES.includes(parsed.mode)) {
      return { ...DEFAULT_THEME };
    }
    return parsed;
  } catch {
    return { ...DEFAULT_THEME };
  }
}

export function saveTheme(theme) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(theme));
}

export function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme.style);
  document.documentElement.setAttribute("data-mode", theme.mode);
  document.dispatchEvent(new CustomEvent("acom-theme-change", { detail: theme }));
}

export function initThemeSwitcher() {
  const theme = loadTheme();
  applyTheme(theme);

  const styleSelect = document.getElementById("theme-style-select");
  const modeToggle = document.getElementById("theme-mode-toggle");

  if (styleSelect) {
    styleSelect.value = theme.style;
    styleSelect.addEventListener("change", () => {
      const next = { ...loadTheme(), style: styleSelect.value };
      saveTheme(next);
      applyTheme(next);
    });
  }

  if (modeToggle) {
    updateModeToggleLabel(modeToggle, theme.mode);
    modeToggle.addEventListener("click", () => {
      const current = loadTheme();
      const next = { ...current, mode: current.mode === "dark" ? "light" : "dark" };
      saveTheme(next);
      applyTheme(next);
      updateModeToggleLabel(modeToggle, next.mode);
    });
  }
}

function updateModeToggleLabel(button, mode) {
  button.textContent = mode === "dark" ? "☀️" : "🌙"; // sun / moon
  button.setAttribute("aria-label", mode === "dark" ? "Switch to light mode" : "Switch to dark mode");
}
