import { writable } from 'svelte/store';

// Get initial theme from localStorage or default to light
function getInitialTheme() {
  if (typeof window !== 'undefined') {
    const stored = localStorage.getItem('theme');
    if (stored === 'dark' || stored === 'light') {
      return stored;
    }
    // Check system preference
    if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
      return 'dark';
    }
  }
  return 'light';
}

export const theme = writable(getInitialTheme());

// Apply theme to document
export function applyTheme(newTheme) {
  if (typeof document !== 'undefined') {
    document.documentElement.setAttribute('data-theme', newTheme);
    localStorage.setItem('theme', newTheme);
  }
}

// Initialize theme on load
if (typeof window !== 'undefined') {
  theme.subscribe(applyTheme);
  // Apply initial theme
  applyTheme(getInitialTheme());
}

export function toggleTheme() {
  theme.update(current => {
    const newTheme = current === 'light' ? 'dark' : 'light';
    return newTheme;
  });
}
