import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./App.css";

// Apply stored preferences before first paint (no flash)
const html = document.documentElement;

const storedFontSize = localStorage.getItem("muse-font-size");
if (storedFontSize && storedFontSize !== "comfortable") {
  html.setAttribute("data-font-size", storedFontSize);
}

const storedFontFamily = localStorage.getItem("muse-font-family");
if (storedFontFamily && storedFontFamily !== "inter") {
  html.setAttribute("data-font-family", storedFontFamily);
}

const storedPalette = localStorage.getItem("muse-palette");
if (storedPalette && storedPalette !== "midnight") {
  html.setAttribute("data-palette", storedPalette);
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
