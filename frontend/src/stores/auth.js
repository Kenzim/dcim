import { writable } from 'svelte/store';
import { getCurrentUser, logout as apiLogout } from '../lib/api.js';

export const user = writable(null);
export const isAuthenticated = writable(false);

export async function checkAuth() {
  try {
    const userData = await getCurrentUser();
    if (userData) {
      user.set(userData);
      isAuthenticated.set(true);
      return true;
    } else {
      user.set(null);
      isAuthenticated.set(false);
      return false;
    }
  } catch (error) {
    user.set(null);
    isAuthenticated.set(false);
    return false;
  }
}

export async function logout() {
  try {
    await apiLogout();
  } catch (error) {
    console.error('Logout error:', error);
  } finally {
    user.set(null);
    isAuthenticated.set(false);
  }
}

