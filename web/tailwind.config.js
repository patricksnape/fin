/** Colors are CSS variables (src/styles.css) so light/dark swap in one place. */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        page: "var(--page)",
        surface: "var(--surface)",
        ink: "var(--ink)",
        "ink-2": "var(--ink-2)",
        muted: "var(--muted)",
        hairline: "var(--hairline)",
        accent: "var(--accent)",
        good: "var(--good-text)",
        bad: "var(--bad-text)",
      },
      fontFamily: { sans: ['system-ui', '-apple-system', '"Segoe UI"', "sans-serif"] },
    },
  },
};
