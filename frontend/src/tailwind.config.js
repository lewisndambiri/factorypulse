/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#17202a",
        panel: "#f4f7f9",
        line: "#d6dde4",
        steel: "#516171",
        signal: "#00897b",
        warning: "#d9822b",
        fault: "#c62828",
      },
    },
  },
  plugins: [],
};
