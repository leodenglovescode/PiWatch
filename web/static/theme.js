'use strict';
(function () {
  const stored = localStorage.getItem('piwatch-theme') || 'light';
  const dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme = stored === 'system' ? (dark ? 'dark' : 'light') : stored;
  document.documentElement.dataset.themePreference = stored;
}());
