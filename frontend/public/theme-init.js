(function () {
  var t = localStorage.getItem('theme');
  if (t === 'dark' || t === 'light') {
    document.documentElement.setAttribute('data-theme', t);
  } else if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
    document.documentElement.setAttribute('data-theme', 'dark');
  } else {
    document.documentElement.setAttribute('data-theme', 'light');
  }
})();
