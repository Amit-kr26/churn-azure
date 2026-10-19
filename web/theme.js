(() => {
  const KEY = "churn-theme";
  const META = { light: "#e9ede8", dark: "#0d1210" };
  let theme = null;

  try {
    theme = localStorage.getItem(KEY);
  } catch {
    theme = null;
  }

  if (theme !== "light" && theme !== "dark") {
    theme = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  document.documentElement.dataset.theme = theme;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", META[theme]);
})();
