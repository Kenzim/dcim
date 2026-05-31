// Simple history-based router wrapper
import { writable } from 'svelte/store';

export const currentRoute = writable('/');

// Initialize router
export function initRouter(routes) {
  // Handle initial route
  const path = window.location.pathname || '/';
  currentRoute.set(path);
  
  // Handle browser back/forward
  window.addEventListener('popstate', () => {
    currentRoute.set(window.location.pathname || '/');
  });
  
  // Intercept link clicks
  document.addEventListener('click', (e) => {
    const link = e.target.closest('a[href^="/"]');
    if (link && !link.hasAttribute('data-router-ignore')) {
      e.preventDefault();
      const href = link.getAttribute('href');
      navigate(href);
    }
  });
  
  return {
    navigate,
    getCurrentRoute: () => window.location.pathname || '/',
    routes
  };
}

export function navigate(path) {
  // Ensure path starts with /
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  
  window.history.pushState({}, '', normalizedPath);
  currentRoute.set(normalizedPath);
  
  // Force update by dispatching popstate event
  window.dispatchEvent(new PopStateEvent('popstate'));
  
  // Also trigger a custom event to ensure reactivity
  window.dispatchEvent(new CustomEvent('routechange', { detail: { path: normalizedPath } }));
}

export function getRouteComponent(routes, path) {
  // Normalize path
  const normalizedPath = path || '/';
  
  // Try exact match first
  if (routes[normalizedPath]) {
    return routes[normalizedPath];
  }
  
  // Try catch-all routes (check these before defaulting)
  for (const [route, component] of Object.entries(routes)) {
    if (route.endsWith('/*')) {
      const baseRoute = route.slice(0, -2);
      if (normalizedPath.startsWith(baseRoute)) {
        return component;
      }
    }
  }
  
  // Default to home if no match
  return routes['/'] || null;
}
