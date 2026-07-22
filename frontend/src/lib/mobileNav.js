// Shared state for the mobile navigation drawer.
// Desktop keeps the sidebar always visible; on small screens the sidebar
// becomes an off-canvas drawer toggled via this store (hamburger in PageHeader).
import { writable } from 'svelte/store';

export const sidebarOpen = writable(false);

export function openSidebar() {
  sidebarOpen.set(true);
}

export function closeSidebar() {
  sidebarOpen.set(false);
}

export function toggleSidebar() {
  sidebarOpen.update((v) => !v);
}
