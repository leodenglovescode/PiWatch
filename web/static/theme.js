'use strict';

function setThemeAttributes(preference) {
  const systemTheme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  const root = document.documentElement;
  root.dataset.theme = preference === 'system' || preference === 'glass' ? systemTheme : preference;
  root.dataset.themePreference = preference;
}

setThemeAttributes(localStorage.getItem('piwatch-theme') || 'light');
